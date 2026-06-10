#!/usr/bin/env python3
"""
ec2_patch_single.py – Production-safe Windows EC2 single-instance patching via SSM Run Command.

Reads instance Name tags from instances.txt, validates them, then patches ONE instance
using AWS-RunPatchBaseline. Designed for AWS CloudShell.

Safety contract
---------------
* Requires operator to select exactly one instance.
* Instance must be in 'stopped' state before patching begins.
* Starts stopped instance, waits for running + SSM Online.
* Records original state to instance_state.json before any mutation.
* Restores instance to stopped state after patching.
* Exits immediately on validation failure or patch failure.
* Requires explicit "yes" confirmation before patching.
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INPUT_FILE = "instances.txt"
STATE_FILE = "instance_state.json"
LOG_FILE = "patch_execution.log"

SSM_DOCUMENT = "AWS-RunPatchBaseline"
PATCH_PARAMETERS = {"Operation": ["Install"], "RebootOption": ["RebootIfNeeded"]}

EC2_RUNNING_TIMEOUT_SEC = 900      # 15 min to reach 'running'
SSM_ONLINE_TIMEOUT_SEC = 900       # 15 min to reach SSM Online after running
EC2_STOPPED_TIMEOUT_SEC = 300      # 5 min to reach 'stopped'
PATCH_TIMEOUT_SEC = 10800          # 3 hrs for patch to complete
POLL_INTERVAL_SEC = 30

TERMINAL_SSM_STATUSES = {
    "Success", "Failed", "Cancelled",
    "TimedOut", "DeliveryTimedOut", "ExecutionTimedOut",
}
FAILURE_SSM_STATUSES = {
    "Failed", "Cancelled", "TimedOut",
    "DeliveryTimedOut", "ExecutionTimedOut",
}

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging() -> logging.Logger:
    logger = logging.getLogger("ec2_patch_single")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%Y-%m-%dT%H:%M:%SZ")

    fh = logging.FileHandler(LOG_FILE)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


log = setup_logging()

# ---------------------------------------------------------------------------
# AWS clients
# ---------------------------------------------------------------------------

def get_ec2():
    return boto3.client("ec2")

def get_ssm():
    return boto3.client("ssm")

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def ts() -> str:
    return datetime.now(timezone.utc).isoformat()

def confirm(prompt: str) -> bool:
    """Ask user for yes/no. Only 'yes' (case-insensitive) returns True."""
    try:
        answer = input(f"\n{prompt} (yes/no): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted by user.")
        return False
    return answer == "yes"

def write_json(path: str, data: dict) -> None:
    Path(path).write_text(json.dumps(data, indent=2))
    log.debug("Wrote %s", path)

def read_json(path: str) -> dict:
    return json.loads(Path(path).read_text())

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def resolve_instance_by_name(name: str, ec2) -> dict:
    """Return the single EC2 instance dict matching Name tag, or raise."""
    resp = ec2.describe_instances(
        Filters=[{"Name": "tag:Name", "Values": [name]},
                 {"Name": "instance-state-name",
                  "Values": ["pending", "running", "stopping",
                             "stopped", "shutting-down"]}]
    )
    instances = [i for r in resp["Reservations"] for i in r["Instances"]]

    if len(instances) == 0:
        raise ValueError(f"No instance found with Name tag '{name}'")
    if len(instances) > 1:
        raise ValueError(f"Multiple instances found with Name tag '{name}' – must be unique.")
    inst = instances[0]
    if inst["State"]["Name"] == "terminated":
        raise ValueError(f"Instance '{name}' ({inst['InstanceId']}) is terminated.")
    return inst

def validate_instance(inst: dict) -> dict:
    """Validate single instance for Windows + stopped state."""
    state = inst["State"]["Name"]
    platform = inst.get("PlatformDetails", "")
    if "Windows" not in platform:
        raise ValueError(f"Instance {inst['InstanceId']} is not Windows (Platform: {platform}).")
    if state != "stopped":
        raise ValueError(f"Instance {inst['InstanceId']} must be stopped, but is {state}.")
    return {
        "name": next((t["Value"] for t in inst.get("Tags", []) if t["Key"] == "Name"), "UNKNOWN"),
        "instance_id": inst["InstanceId"],
        "instance_type": inst["InstanceType"],
        "availability_zone": inst["Placement"]["AvailabilityZone"],
        "current_state": state,
        "original_state": state,
    }

# ---------------------------------------------------------------------------
# EC2 state transitions
# ---------------------------------------------------------------------------

def wait_for_ec2_state(instance_id: str, target_state: str, timeout: int, ec2) -> bool:
    deadline = time.monotonic() + timeout
    log.info("    Waiting for %s to reach EC2 state '%s' …", instance_id, target_state)
    while time.monotonic() < deadline:
        resp = ec2.describe_instances(InstanceIds=[instance_id])
        state = resp["Reservations"][0]["Instances"][0]["State"]["Name"]
        log.debug("    %s state: %s", instance_id, state)
        if state == target_state:
            log.info("    %s reached '%s'.", instance_id, target_state)
            return True
        time.sleep(POLL_INTERVAL_SEC)
    log.error("    Timeout waiting for %s to reach '%s'.", instance_id, target_state)
    return False

def wait_for_ssm_online(instance_id: str, timeout: int, ssm) -> bool:
    deadline = time.monotonic() + timeout
    log.info("    Waiting for %s to become SSM Online …", instance_id)
    while time.monotonic() < deadline:
        resp = ssm.describe_instance_information(Filters=[{"Key": "InstanceIds", "Values": [instance_id]}])
        items = resp.get("InstanceInformationList", [])
        if items and items[0].get("PingStatus") == "Online":
            log.info("    %s is SSM Online.", instance_id)
            return True
        time.sleep(POLL_INTERVAL_SEC)
    log.error("    Timeout waiting for %s to become SSM Online.", instance_id)
    return False

def start_instance(instance: dict, ec2, ssm) -> bool:
    iid = instance["instance_id"]
    name = instance["name"]
    log.info("  Starting instance %s (%s) …", name, iid)
    try:
        ec2.start_instances(InstanceIds=[iid])
    except ClientError as exc:
        log.error("  Failed to start %s: %s", iid, exc)
        return False
    if not wait_for_ec2_state(iid, "running", EC2_RUNNING_TIMEOUT_SEC, ec2):
        return False
    return wait_for_ssm_online(iid, SSM_ONLINE_TIMEOUT_SEC, ssm)

def stop_instance(instance_id: str, ec2) -> bool:
    log.info("  Stopping instance %s …", instance_id)
    try:
        ec2.stop_instances(InstanceIds=[instance_id])
    except ClientError as exc:
        log.error("  Failed to stop %s: %s", instance_id, exc)
        return False
    return wait_for_ec2_state(instance_id, "stopped", EC2_STOPPED_TIMEOUT_SEC, ec2)

# ---------------------------------------------------------------------------
# Patching
# ---------------------------------------------------------------------------

def send_patch_command(instance_id: str, ssm) -> str:
    log.info("  Sending Run Command to instance %s …", instance_id)
    resp = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName=SSM_DOCUMENT,
        Parameters=PATCH_PARAMETERS,
        Comment=f"ec2_patch_single.py – {ts()}",
        TimeoutSeconds=PATCH_TIMEOUT_SEC,
    )
    cmd_id = resp["Command"]["CommandId"]
    log.info("  Command ID: %s", cmd_id)
    return cmd_id

def poll_patch_command(command_id: str, instance: dict, ssm) -> str:
    iid = instance["instance_id"]
    status = "Pending"
    deadline = time.monotonic() + PATCH_TIMEOUT_SEC
    while time.monotonic() < deadline:
        try:
            resp = ssm.get_command_invocation(CommandId=command_id, InstanceId=iid)
            new_status = resp["StatusDetails"]
            if new_status != status:
                log.info("    %s → %s", iid, new_status)
