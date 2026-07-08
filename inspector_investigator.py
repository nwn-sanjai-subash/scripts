#!/usr/bin/env python3
"""
inspector_investigator.py

Interactive, read-only investigation tool for Amazon Inspector vulnerability
findings on a single EC2 instance. Designed to be run directly in AWS
CloudShell (or any shell with boto3 + an AWS role/profile already
configured) with NO command-line arguments -- it walks you through the
investigation step by step.

FLOW
----
  1. Run the script:            python3 inspector_investigator.py
  2. It asks you for an EC2 Instance ID.
  3. It shows the instance metadata, then a QUICK, SERVER-FREE summary of
     Inspector findings (title, CVE, package, Inspector-reported versions).
     Nothing is sent to the instance at this stage.
  4. It then shows a menu. You decide what happens next:
       - View full details of a finding (description / remediation / links)
       - Verify a specific finding ON THE SERVER (this is the step that
         actually connects to the instance via SSM, using only read-only
         commands, to confirm whether the vulnerability really exists)
       - Verify ALL findings on the server
       - Export everything gathered so far to a JSON file
       - Investigate a different instance
       - Exit

SAFETY
------
  This tool is strictly read-only. It will never install, update, remove,
  or otherwise modify software; never modify AWS resources; never reboot,
  restart, or stop anything; never trigger Inspector scans or SSM inventory
  refreshes. Every command it is capable of sending to an instance comes
  from a small fixed set of read-only templates (see ALLOWED_* constants
  below), and every command is additionally checked against a deny-list
  before being sent, as defense in depth.

REQUIRED AWS PERMISSIONS (READ-ONLY)
-------------------------------------
  ec2:DescribeInstances
  inspector2:ListFindings
  ssm:SendCommand
  ssm:GetCommandInvocation
  ssm:ListCommandInvocations

RUNNING IN AWS CLOUDSHELL
--------------------------
  CloudShell already has boto3-compatible credentials (your CloudShell
  session role) and Python 3 pre-installed. Just:

      pip3 install --user boto3          # if boto3 isn't already available
      python3 inspector_investigator.py

  Non-interactive / scripted use is also supported via flags, see --help.
"""

from __future__ import annotations

import argparse
import json
import logging
import re as _re
import shlex
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError, BotoCoreError
except ImportError:  # pragma: no cover
    print("This tool requires boto3. In CloudShell run: pip3 install --user boto3")
    sys.exit(1)


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

# The ONLY commands this tool is capable of sending to an instance via SSM.
# Every one of these is a read-only / informational command. Package names
# are substituted in via shlex.quote() -- never via raw string formatting of
# untrusted input into a shell, to prevent command injection or command
# smuggling.

OS_DETECT_COMMANDS = ["cat /etc/os-release"]
OS_DETECT_FALLBACKS = [
    "cat /etc/redhat-release",
    "uname -a",
]

DEBIAN_VERIFY_COMMANDS = {
    "dpkg_status": "dpkg -l {pkg}",
    "apt_policy": "apt-cache policy {pkg}",
    "apt_upgradable": "apt list --upgradable",
    "reboot_required": "test -f /var/run/reboot-required && echo REBOOT_REQUIRED || echo NO_REBOOT_REQUIRED",
    "running_kernel": "uname -r",
}

RPM_VERIFY_COMMANDS = {
    "rpm_q": "rpm -q {pkg}",
    "rpm_qi": "rpm -qi {pkg}",
    "dnf_info": "dnf info {pkg}",
    "yum_info": "yum info {pkg}",
    "needs_restarting": "needs-restarting -r; echo NEEDS_RESTARTING_EXIT_$?",
    "running_kernel": "uname -r",
}

DEBIAN_FAMILIES = {"ubuntu", "debian"}
RPM_FAMILIES = {
    "amazon linux", "amazon linux 2", "amazon linux 2023",
    "rhel", "centos", "rocky", "almalinux", "oracle linux",
}

KERNEL_PACKAGE_HINTS = (
    "linux-image", "linux-image-aws", "linux-image-generic",
    "kernel", "kernel-core", "kernel-uek", "kernel-default",
)

DEFAULT_SEVERITY = "CRITICAL"
DEFAULT_STATUS = "ACTIVE"

SSM_POLL_INTERVAL_SECONDS = 2
SSM_POLL_TIMEOUT_SECONDS = 60
SSM_MAX_RETRIES = 3

# Explicit deny-list. This is a defense-in-depth safety net: even though this
# tool only ever *constructs* commands from the read-only templates above,
# we additionally refuse to send anything that matches known
# mutating/destructive command signatures, in case of future code changes.
FORBIDDEN_COMMAND_SUBSTRINGS = [
    "apt install", "apt-get install", "apt upgrade", "apt-get upgrade",
    "apt update", "apt-get update", "apt remove", "apt-get remove",
    "apt autoremove", "dpkg -i", "dpkg --install",
    "yum install", "yum update", "yum upgrade", "yum remove",
    "dnf install", "dnf update", "dnf upgrade", "dnf remove",
    "rpm -U", "rpm -i", "rpm --install", "rpm --upgrade",
    "reboot", "shutdown", "systemctl restart", "systemctl stop",
    "systemctl start", "systemctl enable", "systemctl disable",
    "amazon-linux-extras", "snap install", "pro attach", "pro enable",
    "subscription-manager register", "subscription-manager attach",
    "subscription-manager repos", "rm", "mkfs", "> /", "dd if=",
]


class SafetyViolation(Exception):
    """Raised if a command that looks mutating is ever about to be sent."""


# Pre-compile deny-list patterns once. For "word-like" entries (letters,
# digits, hyphens, spaces only) we use lookaround word-boundary matching so
# that e.g. the forbidden word "reboot" does NOT false-positive on the
# legitimate, read-only filename "/var/run/reboot-required" -- only on
# "reboot" actually being invoked as a standalone command/word. Entries
# containing symbols (">", "/", etc.) fall back to plain substring matching
# since word-boundary semantics don't apply to them.
_WORD_LIKE_RE = _re.compile(r"^[a-z0-9\-\s]+$")
_COMPILED_FORBIDDEN_PATTERNS = []
for _entry in FORBIDDEN_COMMAND_SUBSTRINGS:
    _stripped = _entry.strip().lower()
    if _WORD_LIKE_RE.match(_stripped):
        _pattern = _re.compile(r"(?<![\w-])" + _re.escape(_stripped) + r"(?![\w-])")
    else:
        _pattern = _re.compile(_re.escape(_stripped))
    _COMPILED_FORBIDDEN_PATTERNS.append((_stripped, _pattern))


def assert_command_is_safe(command: str) -> None:
    """Defense-in-depth guard: refuse to send anything resembling a
    mutating command, regardless of where it came from in the code."""
    lowered = command.lower()
    for original, pattern in _COMPILED_FORBIDDEN_PATTERNS:
        if pattern.search(lowered):
            raise SafetyViolation(
                f"Refusing to execute command that matches forbidden pattern "
                f"'{original}': {command}"
            )


# --------------------------------------------------------------------------
# Data classes
# --------------------------------------------------------------------------

@dataclass
class InstanceMetadata:
    instance_id: str
    name: str
    region: str
    availability_zone: str
    platform: str
    platform_details: str
    ami_id: str
    state: str
    instance_type: str


@dataclass
class Finding:
    arn: str
    title: str
    description: str
    severity: str
    status: str
    cve: str
    package_name: str
    installed_version: str
    fixed_version: str
    vendor_severity: str
    cvss: Optional[float]
    last_observed: Optional[str]
    first_observed: Optional[str]
    reference_urls: List[str] = field(default_factory=list)
    remediation: str = ""


@dataclass
class VerificationResult:
    package_found: bool = False
    installed_version_verified: Optional[str] = None
    candidate_version: Optional[str] = None
    upgradable: Optional[bool] = None
    is_kernel_package: bool = False
    running_kernel: Optional[str] = None
    reboot_required: Optional[bool] = None
    esm_suspected: bool = False
    raw_output: Dict[str, str] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)


@dataclass
class Analysis:
    rule: str
    assessment: str
    recommended_action: str
    client_ticket_likely: bool


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

def setup_logging(verbose: bool) -> logging.Logger:
    logger = logging.getLogger("inspector_investigator")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.handlers = [handler]
    return logger


# --------------------------------------------------------------------------
# EC2 validation + metadata
# --------------------------------------------------------------------------

def validate_and_get_instance(ec2_client, instance_id: str, logger: logging.Logger) -> Optional[dict]:
    """Returns the raw EC2 instance dict, or None if it doesn't exist."""
    try:
        resp = ec2_client.describe_instances(InstanceIds=[instance_id])
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("InvalidInstanceID.NotFound", "InvalidInstanceID.Malformed"):
            logger.error(f"Instance {instance_id} not found: {e}")
            return None
        logger.error(f"Error describing instance {instance_id}: {e}")
        raise

    reservations = resp.get("Reservations", [])
    if not reservations or not reservations[0].get("Instances"):
        logger.error(f"Instance {instance_id} not found.")
        return None
    return reservations[0]["Instances"][0]


def extract_metadata(instance: dict, region: str) -> InstanceMetadata:
    name = ""
    for tag in instance.get("Tags", []):
        if tag.get("Key") == "Name":
            name = tag.get("Value", "")
            break

    return InstanceMetadata(
        instance_id=instance.get("InstanceId", ""),
        name=name or "(no Name tag)",
        region=region,
        availability_zone=instance.get("Placement", {}).get("AvailabilityZone", ""),
        platform=instance.get("PlatformDetails", instance.get("Platform", "linux")),
        platform_details=instance.get("PlatformDetails", ""),
        ami_id=instance.get("ImageId", ""),
        state=instance.get("State", {}).get("Name", ""),
        instance_type=instance.get("InstanceType", ""),
    )


# --------------------------------------------------------------------------
# Inspector findings
# --------------------------------------------------------------------------

def get_inspector_findings(
    inspector_client, instance_id: str, severity: str, status: str, logger: logging.Logger
) -> List[Finding]:
    filter_criteria = {
        "resourceId": [{"comparison": "EQUALS", "value": instance_id}],
        "severity": [{"comparison": "EQUALS", "value": severity}],
        "findingStatus": [{"comparison": "EQUALS", "value": status}],
    }

    findings: List[Finding] = []
    next_token = None

    while True:
        kwargs = {"filterCriteria": filter_criteria, "maxResults": 100}
        if next_token:
            kwargs["nextToken"] = next_token
        try:
            resp = inspector_client.list_findings(**kwargs)
        except ClientError as e:
            logger.error(f"Error listing Inspector findings: {e}")
            raise

        for f in resp.get("findings", []):
            findings.append(_parse_finding(f))

        next_token = resp.get("nextToken")
        if not next_token:
            break

    return findings


def _parse_finding(f: dict) -> Finding:
    package_name = ""
    installed_version = ""
    fixed_version = ""
    cve = f.get("findingArn", "").split("/")[-1] if not f.get("packageVulnerabilityDetails") else ""

    pvd = f.get("packageVulnerabilityDetails") or {}
    if pvd.get("vulnerabilityId"):
        cve = pvd["vulnerabilityId"]

    vulnerable_packages = pvd.get("vulnerablePackages") or []
    if vulnerable_packages:
        pkg = vulnerable_packages[0]
        package_name = pkg.get("name", "")
        installed_version = pkg.get("version", "")
        fixed_version = pkg.get("fixedInVersion", "") or pkg.get("remediation", "")

    reference_urls = pvd.get("referenceUrls", []) or []

    vendor_severity = ""
    cvss_score: Optional[float] = None
    cvss_list = pvd.get("cvss") or []
    if cvss_list:
        cvss_score = cvss_list[0].get("baseScore")
        vendor_severity = cvss_list[0].get("source", "")

    remediation = ""
    remediation_obj = f.get("remediation") or {}
    if remediation_obj.get("recommendation"):
        remediation = remediation_obj["recommendation"].get("text", "")

    return Finding(
        arn=f.get("findingArn", ""),
        title=f.get("title", ""),
        description=f.get("description", ""),
        severity=f.get("severity", ""),
        status=f.get("status", ""),
        cve=cve,
        package_name=package_name,
        installed_version=installed_version,
        fixed_version=fixed_version,
        vendor_severity=vendor_severity,
        cvss=cvss_score,
        last_observed=str(f.get("lastObservedAt", "")) or None,
        first_observed=str(f.get("firstObservedAt", "")) or None,
        reference_urls=reference_urls,
        remediation=remediation,
    )


# --------------------------------------------------------------------------
# SSM helper: generic safe command runner with retries + timeout
# --------------------------------------------------------------------------

def run_ssm_commands(
    ssm_client,
    instance_id: str,
    commands: List[str],
    logger: logging.Logger,
    timeout_seconds: int = SSM_POLL_TIMEOUT_SECONDS,
) -> Tuple[bool, str, str]:
    """
    Sends a list of shell commands to the instance via AWS-RunShellScript.
    Every command is checked against the safety deny-list before being
    sent. Returns (success, stdout, stderr).
    """
    for cmd in commands:
        assert_command_is_safe(cmd)

    last_error = None
    for attempt in range(1, SSM_MAX_RETRIES + 1):
        try:
            send_resp = ssm_client.send_command(
                InstanceIds=[instance_id],
                DocumentName="AWS-RunShellScript",
                Parameters={"commands": commands},
                TimeoutSeconds=max(timeout_seconds, 30),
            )
            command_id = send_resp["Command"]["CommandId"]
            return _poll_ssm_command(ssm_client, command_id, instance_id, logger, timeout_seconds)
        except (ClientError, BotoCoreError) as e:
            last_error = e
            logger.debug(f"SSM send_command attempt {attempt} failed: {e}")
            time.sleep(1.5 * attempt)

    logger.warning(f"SSM command failed after {SSM_MAX_RETRIES} attempts: {last_error}")
    return False, "", str(last_error)


def _poll_ssm_command(
    ssm_client, command_id: str, instance_id: str, logger: logging.Logger, timeout_seconds: int
) -> Tuple[bool, str, str]:
    start = time.time()
    while time.time() - start < timeout_seconds:
        try:
            inv = ssm_client.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "InvocationDoesNotExist":
                time.sleep(SSM_POLL_INTERVAL_SECONDS)
                continue
            logger.debug(f"get_command_invocation error: {e}")
            time.sleep(SSM_POLL_INTERVAL_SECONDS)
            continue

        status = inv.get("Status")
        if status in ("Success", "Failed", "Cancelled", "TimedOut"):
            stdout = inv.get("StandardOutputContent", "")
            stderr = inv.get("StandardErrorContent", "")
            return status == "Success", stdout, stderr

        time.sleep(SSM_POLL_INTERVAL_SECONDS)

    logger.warning(f"Timed out waiting for SSM command {command_id} on {instance_id}")
    return False, "", "timeout"


# --------------------------------------------------------------------------
# OS detection
# --------------------------------------------------------------------------

def detect_os(ssm_client, instance_id: str, logger: logging.Logger) -> str:
    ok, stdout, _ = run_ssm_commands(ssm_client, instance_id, OS_DETECT_COMMANDS, logger)
    os_family = _parse_os_release(stdout) if ok else ""

    if not os_family:
        ok, stdout, _ = run_ssm_commands(ssm_client, instance_id, [OS_DETECT_FALLBACKS[0]], logger)
        os_family = _parse_redhat_release(stdout) if ok else ""

    if not os_family:
        ok, stdout, _ = run_ssm_commands(ssm_client, instance_id, [OS_DETECT_FALLBACKS[1]], logger)
        os_family = _parse_uname(stdout) if ok else ""

    if not os_family:
        logger.warning(f"Could not determine OS for {instance_id}; verification steps will be skipped.")
        os_family = "unknown"

    return os_family


def _parse_os_release(text: str) -> str:
    fields = {}
    for line in text.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            fields[k.strip()] = v.strip().strip('"')

    os_id = fields.get("ID", "").lower()
    version_id = fields.get("VERSION_ID", "")
    pretty = fields.get("PRETTY_NAME", "").lower()

    if os_id == "ubuntu":
        return "ubuntu"
    if os_id == "debian":
        return "debian"
    if os_id in ("amzn", "amazon"):
        if version_id == "2":
            return "amazon linux 2"
        if version_id == "2023":
            return "amazon linux 2023"
        return "amazon linux"
    if os_id == "rhel":
        return "rhel"
    if os_id == "centos":
        return "centos"
    if os_id == "rocky":
        return "rocky"
    if os_id in ("almalinux", "alma"):
        return "almalinux"
    if "oracle" in os_id or "oracle" in pretty:
        return "oracle linux"

    return ""


def _parse_redhat_release(text: str) -> str:
    lowered = text.lower()
    if "centos" in lowered:
        return "centos"
    if "red hat" in lowered or "rhel" in lowered:
        return "rhel"
    if "rocky" in lowered:
        return "rocky"
    if "alma" in lowered:
        return "almalinux"
    if "oracle" in lowered:
        return "oracle linux"
    return ""


def _parse_uname(text: str) -> str:
    lowered = text.lower()
    if "ubuntu" in lowered:
        return "ubuntu"
    if "debian" in lowered:
        return "debian"
    if "amzn" in lowered or "amazon" in lowered:
        return "amazon linux"
    return ""


# --------------------------------------------------------------------------
# Package verification (this is the part that actually touches the server)
# --------------------------------------------------------------------------

def is_kernel_package(package_name: str) -> bool:
    name = (package_name or "").lower()
    return any(hint in name for hint in KERNEL_PACKAGE_HINTS)


def verify_package(
    ssm_client, instance_id: str, os_family: str, package_name: str, logger: logging.Logger
) -> VerificationResult:
    result = VerificationResult(is_kernel_package=is_kernel_package(package_name))

    if os_family == "unknown" or not package_name:
        result.notes.append("OS unknown or package name missing; skipping live verification.")
        return result

    quoted_pkg = shlex.quote(package_name)

    if os_family in DEBIAN_FAMILIES:
        _verify_debian(ssm_client, instance_id, quoted_pkg, result, logger)
    elif os_family in RPM_FAMILIES:
        _verify_rpm(ssm_client, instance_id, quoted_pkg, result, logger)
    else:
        result.notes.append(f"Unsupported OS family '{os_family}' for live verification.")

    return result


def _verify_debian(ssm_client, instance_id: str, quoted_pkg: str, result: VerificationResult, logger) -> None:
    dpkg_cmd = DEBIAN_VERIFY_COMMANDS["dpkg_status"].format(pkg=quoted_pkg)
    ok, out, _ = run_ssm_commands(ssm_client, instance_id, [dpkg_cmd], logger)
    result.raw_output["dpkg -l"] = out
    if ok and "no packages found" not in out.lower():
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0] in ("ii", "hi"):
                result.package_found = True
                result.installed_version_verified = parts[2]
                break

    policy_cmd = DEBIAN_VERIFY_COMMANDS["apt_policy"].format(pkg=quoted_pkg)
    ok, out, _ = run_ssm_commands(ssm_client, instance_id, [policy_cmd], logger)
    result.raw_output["apt-cache policy"] = out
    if ok:
        for line in out.splitlines():
            line_stripped = line.strip()
            if line_stripped.startswith("Candidate:"):
                candidate = line_stripped.split(":", 1)[1].strip()
                if candidate and candidate != "(none)":
                    result.candidate_version = candidate
                if "esm" in candidate.lower():
                    result.esm_suspected = True

    ok, out, _ = run_ssm_commands(ssm_client, instance_id, [DEBIAN_VERIFY_COMMANDS["apt_upgradable"]], logger)
    result.raw_output["apt list --upgradable"] = out
    if ok:
        result.upgradable = any(
            result.installed_version_verified and result.installed_version_verified not in line
            for line in out.splitlines()
            if line.strip() and not line.startswith("Listing")
        )

    ok, out, _ = run_ssm_commands(ssm_client, instance_id, [DEBIAN_VERIFY_COMMANDS["running_kernel"]], logger)
    if ok:
        result.running_kernel = out.strip()

    if result.is_kernel_package:
        ok, out, _ = run_ssm_commands(ssm_client, instance_id, [DEBIAN_VERIFY_COMMANDS["reboot_required"]], logger)
        if ok:
            result.reboot_required = "REBOOT_REQUIRED" in out


def _verify_rpm(ssm_client, instance_id: str, quoted_pkg: str, result: VerificationResult, logger) -> None:
    rpm_q_cmd = RPM_VERIFY_COMMANDS["rpm_q"].format(pkg=quoted_pkg)
    ok, out, _ = run_ssm_commands(ssm_client, instance_id, [rpm_q_cmd], logger)
    result.raw_output["rpm -q"] = out
    if ok and "not installed" not in out.lower() and "error" not in out.lower():
        result.package_found = True
        tokens = out.strip().split("-")
        if len(tokens) >= 2:
            result.installed_version_verified = "-".join(tokens[-2:])

    rpm_qi_cmd = RPM_VERIFY_COMMANDS["rpm_qi"].format(pkg=quoted_pkg)
    ok, out, _ = run_ssm_commands(ssm_client, instance_id, [rpm_qi_cmd], logger)
    result.raw_output["rpm -qi"] = out

    dnf_cmd = RPM_VERIFY_COMMANDS["dnf_info"].format(pkg=quoted_pkg)
    ok, out, _ = run_ssm_commands(ssm_client, instance_id, [dnf_cmd], logger)
    if not ok or not out.strip():
        yum_cmd = RPM_VERIFY_COMMANDS["yum_info"].format(pkg=quoted_pkg)
        ok, out, _ = run_ssm_commands(ssm_client, instance_id, [yum_cmd], logger)
        result.raw_output["yum info"] = out
    else:
        result.raw_output["dnf info"] = out

    if ok:
        in_available = False
        for line in out.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("available packages"):
                in_available = True
            if in_available and stripped.lower().startswith("version"):
                result.candidate_version = stripped.split(":", 1)[-1].strip()

    ok, out, _ = run_ssm_commands(ssm_client, instance_id, [RPM_VERIFY_COMMANDS["running_kernel"]], logger)
    if ok:
        result.running_kernel = out.strip()

    if result.is_kernel_package:
        ok, out, _ = run_ssm_commands(ssm_client, instance_id, [RPM_VERIFY_COMMANDS["needs_restarting"]], logger)
        if ok:
            result.reboot_required = "NEEDS_RESTARTING_EXIT_1" in out


# --------------------------------------------------------------------------
# Analysis engine
# --------------------------------------------------------------------------

def analyze_finding(finding: Finding, verification: VerificationResult, os_family: str) -> Analysis:
    installed = finding.installed_version
    fixed = finding.fixed_version
    verified_installed = verification.installed_version_verified
    candidate = verification.candidate_version
    running_kernel = verification.running_kernel

    if not verification.package_found and os_family != "unknown":
        return Analysis(
            rule="Rule 1: Package missing",
            assessment="Package not found on the instance. Possible stale finding or package removal.",
            recommended_action="Confirm the package name/inventory in Inspector. If confirmed removed, "
                                "no action needed; otherwise investigate why it is absent.",
            client_ticket_likely=False,
        )

    if verification.is_kernel_package and verified_installed and running_kernel:
        if running_kernel not in verified_installed and running_kernel != verified_installed:
            reboot_flag = verification.reboot_required
            return Analysis(
                rule="Rule 4: Kernel package updated, running kernel older",
                assessment="Kernel package updated. Pending reboot likely required."
                            + (" Reboot need confirmed by system check." if reboot_flag else
                               " Reboot need not confirmed by system check; review manually."),
                recommended_action="Schedule reboot during maintenance window.",
                client_ticket_likely=True,
            )

    if verification.esm_suspected and "esm" in (fixed or "").lower():
        return Analysis(
            rule="Rule 5: Ubuntu Pro / ESM",
            assessment="Inspector expects an ESM-patched version and Ubuntu Pro/ESM does not appear to be "
                        "attached. Ubuntu Pro / ESM package may be required.",
            recommended_action="Verify Ubuntu Pro / ESM attachment status and entitlement for this instance.",
            client_ticket_likely=True,
        )

    if fixed and verified_installed:
        if verified_installed == fixed or (installed and installed == fixed):
            return Analysis(
                rule="Rule 3/7: Installed equals fixed version",
                assessment="Evidence suggests package already updated. Finding may clear after Inspector "
                            "refresh. Review LastObserved timestamp.",
                recommended_action="No manual remediation needed; monitor for Inspector re-scan to confirm "
                                    "the finding clears.",
                client_ticket_likely=False,
            )
        if installed and fixed and installed == verified_installed and installed != fixed:
            return Analysis(
                rule="Rule 2: Installed older than fixed",
                assessment="Package update required. Installed version does not match the vendor-fixed "
                            "version.",
                recommended_action=f"Update '{finding.package_name}' to version {fixed} or later during "
                                    f"the next maintenance window.",
                client_ticket_likely=True,
            )

    if candidate and verified_installed and candidate != verified_installed:
        return Analysis(
            rule="Rule 6: Candidate newer than installed",
            assessment="Package update available. A newer candidate version exists in the repository.",
            recommended_action=f"Update '{finding.package_name}' from {verified_installed} to {candidate} "
                                f"during the next maintenance window.",
            client_ticket_likely=True,
        )

    if verified_installed and installed and verified_installed != installed:
        return Analysis(
            rule="Rule 7: Package already updated",
            assessment="Running/installed version differs from what Inspector recorded. Appears resolved "
                        "pending Inspector refresh.",
            recommended_action="No manual remediation needed; monitor for Inspector re-scan to confirm "
                                "the finding clears.",
            client_ticket_likely=False,
        )

    return Analysis(
        rule="Unclassified",
        assessment="Automated verification was inconclusive. Requires further validation.",
        recommended_action="Manually review the package status on the instance and cross-check against "
                            "the vendor advisory.",
        client_ticket_likely=False,
    )


# --------------------------------------------------------------------------
# Display helpers
# --------------------------------------------------------------------------

def print_instance_metadata(meta: InstanceMetadata) -> None:
    print("=" * 65)
    print("INSTANCE METADATA")
    print("=" * 65)
    print(f"Instance ID       : {meta.instance_id}")
    print(f"Name              : {meta.name}")
    print(f"Region            : {meta.region}")
    print(f"Availability Zone : {meta.availability_zone}")
    print(f"Platform          : {meta.platform}")
    print(f"Platform Details  : {meta.platform_details}")
    print(f"AMI ID            : {meta.ami_id}")
    print(f"State             : {meta.state}")
    print(f"Instance Type     : {meta.instance_type}")
    print()


def print_basic_findings_table(findings: List[Finding]) -> None:
    print("=" * 90)
    print(f"INSPECTOR FINDINGS - BASIC SUMMARY ({len(findings)} found, no server contact made yet)")
    print("=" * 90)
    for i, f in enumerate(findings, start=1):
        print(f"[{i}] {f.cve or '(no CVE)'}  |  {f.package_name or '(unknown package)'}  |  Severity: {f.severity}")
        print(f"     Title            : {f.title}")
        print(f"     Inspector Installed: {f.installed_version or '?'}   Fixed: {f.fixed_version or '?'}")
        print(f"     Last Observed    : {f.last_observed}")
        print()


def print_finding_detail(index: int, finding: Finding) -> None:
    print("=" * 65)
    print(f"Finding {index} - Full Detail (Inspector data only, no server contact)")
    print("=" * 65)
    print(f"ARN                   : {finding.arn}")
    print(f"Title                 : {finding.title}")
    print(f"CVE                   : {finding.cve}")
    print(f"Severity              : {finding.severity}")
    print(f"Status                : {finding.status}")
    print(f"Package               : {finding.package_name}")
    print(f"Installed Version     : {finding.installed_version}")
    print(f"Fixed Version         : {finding.fixed_version}")
    print(f"Vendor Severity Source: {finding.vendor_severity}")
    print(f"CVSS                  : {finding.cvss}")
    print(f"First Observed        : {finding.first_observed}")
    print(f"Last Observed         : {finding.last_observed}")
    print(f"Description           : {finding.description}")
    print(f"Remediation (vendor)  : {finding.remediation}")
    if finding.reference_urls:
        print("Reference URLs        :")
        for url in finding.reference_urls:
            print(f"  - {url}")
    print()


def print_verification_report(index: int, finding: Finding, verification: VerificationResult, analysis: Analysis) -> None:
    print("=" * 65)
    print(f"Finding {index} - SERVER VERIFICATION RESULT")
    print("=" * 65)
    print(f"Package               : {finding.package_name or '(unknown)'}")
    print(f"CVE                   : {finding.cve}")
    print()
    print("Inspector reported:")
    print(f"  Installed Version   : {finding.installed_version}")
    print(f"  Fixed Version       : {finding.fixed_version}")
    print()
    print("Verified on instance:")
    print(f"  Package Found       : {verification.package_found}")
    print(f"  Installed Version   : {verification.installed_version_verified}")
    print(f"  Candidate Version   : {verification.candidate_version}")
    if verification.is_kernel_package:
        print(f"  Running Kernel      : {verification.running_kernel}")
        print(f"  Reboot Required     : {verification.reboot_required}")
    if verification.notes:
        for note in verification.notes:
            print(f"  Note                : {note}")
    print()
    print(f"Assessment            : {analysis.assessment}")
    print(f"Rule Applied          : {analysis.rule}")
    print(f"Recommended Action    : {analysis.recommended_action}")
    print(f"Client Ticket         : {'Likely Required' if analysis.client_ticket_likely else 'Not Likely Required'}")
    print()


def print_summary(results: Dict[int, Tuple[VerificationResult, Analysis]], total_findings: int) -> None:
    counts = _compute_summary_counts(results)
    print("=" * 65)
    print("SUMMARY (of findings verified so far)")
    print("=" * 65)
    print(f"Findings Reviewed                 : {total_findings}")
    print(f"Findings Verified On Server        : {len(results)}")
    print(f"Package Updates Required          : {counts['package_updates_required']}")
    print(f"Kernel Reboot Required             : {counts['kernel_reboot_required']}")
    print(f"Possible Inspector Refresh Cases  : {counts['possible_inspector_refresh_cases']}")
    print(f"Ubuntu Pro / ESM Related           : {counts['ubuntu_pro_esm_related']}")
    print(f"Further Manual Investigation       : {counts['further_manual_investigation']}")


def _compute_summary_counts(results: Dict[int, Tuple[VerificationResult, Analysis]]) -> dict:
    items = list(results.values())
    updates_required = sum(1 for _, a in items if a.rule.startswith("Rule 2") or a.rule.startswith("Rule 6"))
    kernel_reboot = sum(1 for _, a in items if a.rule.startswith("Rule 4"))
    refresh_cases = sum(1 for _, a in items if a.rule.startswith("Rule 3") or a.rule.startswith("Rule 7"))
    esm_cases = sum(1 for _, a in items if a.rule.startswith("Rule 5"))
    further_review = sum(1 for _, a in items if a.rule == "Unclassified")
    return {
        "package_updates_required": updates_required,
        "kernel_reboot_required": kernel_reboot,
        "possible_inspector_refresh_cases": refresh_cases,
        "ubuntu_pro_esm_related": esm_cases,
        "further_manual_investigation": further_review,
    }


def build_json_report(
    meta: InstanceMetadata,
    findings: List[Finding],
    verified: Dict[int, Tuple[VerificationResult, Analysis]],
) -> dict:
    finding_entries = []
    for i, f in enumerate(findings, start=1):
        entry = {"index": i, "finding": asdict(f)}
        if i in verified:
            v, a = verified[i]
            entry["verification"] = asdict(v)
            entry["analysis"] = asdict(a)
        finding_entries.append(entry)

    return {
        "instance": asdict(meta),
        "findings": finding_entries,
        "summary": {
            "findings_reviewed": len(findings),
            "findings_verified_on_server": len(verified),
            **_compute_summary_counts(verified),
        },
    }


# --------------------------------------------------------------------------
# Interactive prompts
# --------------------------------------------------------------------------

def prompt(text: str) -> str:
    try:
        return input(text).strip()
    except EOFError:
        return ""


def prompt_instance_id() -> Optional[str]:
    print()
    instance_id = prompt("Enter the EC2 Instance ID to investigate (or 'q' to quit): ")
    if instance_id.lower() in ("q", "quit", "exit", ""):
        return None
    return instance_id


def prompt_filters(default_severity: str, default_status: str) -> Tuple[str, str]:
    use_defaults = prompt(
        f"Use default filters? Severity={default_severity}, Status={default_status} [Y/n]: "
    ).lower()
    if use_defaults in ("", "y", "yes"):
        return default_severity, default_status

    severity = prompt(f"Severity filter [{default_severity}]: ") or default_severity
    status = prompt(f"Status filter [{default_status}]: ") or default_status
    return severity.upper(), status.upper()


def show_menu() -> str:
    print("-" * 65)
    print("What would you like to do next?")
    print("  [1] View full details of a finding (no server contact)")
    print("  [2] Verify a SPECIFIC finding on the server (confirms if real)")
    print("  [3] Verify ALL findings on the server")
    print("  [4] Show summary of findings verified so far")
    print("  [5] Export everything gathered so far to a JSON file")
    print("  [6] Investigate a different instance")
    print("  [7] Exit")
    print("-" * 65)
    return prompt("Choose an option [1-7]: ")


def prompt_finding_index(max_index: int) -> Optional[int]:
    raw = prompt(f"Enter finding number [1-{max_index}]: ")
    try:
        idx = int(raw)
        if 1 <= idx <= max_index:
            return idx
    except ValueError:
        pass
    print("Invalid finding number.")
    return None


# --------------------------------------------------------------------------
# Instance investigation session
# --------------------------------------------------------------------------

def run_instance_session(
    ec2_client, inspector_client, ssm_client, instance_id: str, region: str,
    default_severity: str, default_status: str, logger: logging.Logger, auto_json_path: Optional[str] = None,
) -> None:
    instance = validate_and_get_instance(ec2_client, instance_id, logger)
    if instance is None:
        print(f"Instance '{instance_id}' does not exist or is not accessible.")
        return

    meta = extract_metadata(instance, region)
    print_instance_metadata(meta)

    severity, status = prompt_filters(default_severity, default_status)

    try:
        findings = get_inspector_findings(inspector_client, instance_id, severity, status, logger)
    except (ClientError, BotoCoreError) as e:
        print(f"Failed to retrieve Inspector findings: {e}")
        return

    if not findings:
        print(f"No {severity} / {status} Inspector findings for {instance_id}. Nothing to investigate.")
        return

    print_basic_findings_table(findings)

    os_family_cache: Dict[str, str] = {}
    verified: Dict[int, Tuple[VerificationResult, Analysis]] = {}

    def get_os_family() -> str:
        if instance_id not in os_family_cache:
            print("Detecting operating system on the instance (read-only SSM command)...")
            os_family_cache[instance_id] = detect_os(ssm_client, instance_id, logger)
            print(f"Detected OS family: {os_family_cache[instance_id]}\n")
        return os_family_cache[instance_id]

    def verify_one(idx: int) -> None:
        finding = findings[idx - 1]
        os_family = get_os_family()
        print(f"Connecting to {instance_id} via SSM to verify '{finding.package_name}' (read-only)...")
        verification = verify_package(ssm_client, instance_id, os_family, finding.package_name, logger)
        analysis = analyze_finding(finding, verification, os_family)
        verified[idx] = (verification, analysis)
        print_verification_report(idx, finding, verification, analysis)

    while True:
        choice = show_menu()

        if choice == "1":
            idx = prompt_finding_index(len(findings))
            if idx:
                print_finding_detail(idx, findings[idx - 1])

        elif choice == "2":
            idx = prompt_finding_index(len(findings))
            if idx:
                verify_one(idx)

        elif choice == "3":
            confirm = prompt(
                f"This will run read-only checks on {instance_id} for all {len(findings)} findings. Continue? [Y/n]: "
            ).lower()
            if confirm in ("", "y", "yes"):
                for i in range(1, len(findings) + 1):
                    verify_one(i)

        elif choice == "4":
            if not verified:
                print("No findings have been verified on the server yet.")
            else:
                print_summary(verified, len(findings))

        elif choice == "5":
            path = auto_json_path or prompt("Output file path [inspector_report.json]: ") or "inspector_report.json"
            report = build_json_report(meta, findings, verified)
            try:
                with open(path, "w") as fh:
                    json.dump(report, fh, indent=2, default=str)
                print(f"Report written to {path}")
            except OSError as e:
                print(f"Failed to write report: {e}")

        elif choice == "6":
            return

        elif choice == "7":
            sys.exit(0)

        else:
            print("Please choose a valid option (1-7).")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Interactive, read-only investigation tool for Amazon Inspector findings on an EC2 "
                    "instance. Run with no arguments for the guided CloudShell-friendly flow."
    )
    parser.add_argument(
        "--instance-id", default=None,
        help="EC2 instance ID. If omitted, the tool will prompt for it interactively.",
    )
    parser.add_argument("--severity", default=DEFAULT_SEVERITY, help="Default finding severity filter (default: CRITICAL)")
    parser.add_argument("--status", default=DEFAULT_STATUS, help="Default finding status filter (default: ACTIVE)")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose/debug logging")
    parser.add_argument("--region", default=None, help="AWS region override (default: CloudShell/session default)")
    parser.add_argument(
        "--json-out", default=None,
        help="If set, skips the file-path prompt on export and always writes to this path.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    logger = setup_logging(args.verbose)

    session = boto3.Session(region_name=args.region) if args.region else boto3.Session()
    region = session.region_name or "us-east-1"
    boto_config = Config(retries={"max_attempts": 5, "mode": "standard"})

    ec2_client = session.client("ec2", config=boto_config)
    inspector_client = session.client("inspector2", config=boto_config)
    ssm_client = session.client("ssm", config=boto_config)

    print("Amazon Inspector Investigation Tool (read-only)")
    print(f"AWS Region: {region}")

    instance_id = args.instance_id
    while True:
        if not instance_id:
            instance_id = prompt_instance_id()
            if not instance_id:
                print("Goodbye.")
                return 0

        try:
            run_instance_session(
                ec2_client, inspector_client, ssm_client, instance_id, region,
                args.severity, args.status, logger, auto_json_path=args.json_out,
            )
        except SafetyViolation as e:
            print(f"SAFETY VIOLATION - aborting this instance's checks: {e}", file=sys.stderr)

        instance_id = None  # force re-prompt for the next loop (option 6 or natural fallthrough)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
