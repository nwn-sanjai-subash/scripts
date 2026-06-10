#!/usr/bin/env python3
"""
ec2_patch.py – Production-safe Windows EC2 patching via SSM Run Command.

Reads instance Name tags from instances.txt, validates them, then patches
in batches of 5 using AWS-RunPatchBaseline. Designed for AWS CloudShell.

Safety contract
---------------
* Never starts all stopped instances simultaneously.
* Starts stopped instances one at a time, waiting for running + SSM Online.
* Records original state to instance_state.json before any mutation.
* Restores originally-stopped instances to stopped on any failure.
* Exits immediately on InsufficientInstanceCapacity.
* Never stops an instance that was originally running.
* Requires explicit "yes" confirmation before every batch.
* Never auto-advances to the next batch.
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

BATCH_SIZE = 5
INPUT_FILE = "instances.txt"
STATE_FILE = "instance_state.json"
RESULTS_FILE = "batch_results.json"
LOG_FILE = "patch_execution.log"

SSM_DOCUMENT = "AWS-RunPatchBaseline"
PATCH_PARAMETERS = {"Operation": ["Install"], "RebootOption": ["RebootIfNeeded"]}

EC2_RUNNING_TIMEOUT_SEC = 300      # 5 min to reach 'running'
SSM_ONLINE_TIMEOUT_SEC = 300       # 5 min to reach SSM Online after running
EC2_STOPPED_TIMEOUT_SEC = 300      # 5 min to reach 'stopped'
PATCH_TIMEOUT_SEC = 7200           # 2 hrs for patch to complete
POLL_INTERVAL_SEC = 15

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
    logger = logging.getLogger("ec2_patch")
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
# AWS clients (module-level, lazily reused)
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


def append_batch_result(batch_num: int, result: dict) -> None:
    results_path = Path(RESULTS_FILE)
    data = read_json(RESULTS_FILE) if results_path.exists() else {}
    data[f"batch_{batch_num}"] = result
    write_json(RESULTS_FILE, data)

# ---------------------------------------------------------------------------
# Phase 1 – Validation
# ---------------------------------------------------------------------------

def resolve_instance_by_name(name: str, ec2) -> dict:
    """Return the single EC2 instance dict matching Name tag, or raise."""
    resp = ec2.describe_instances(
        Filters=[{"Name": "tag:Name", "Values": [name]},
                 {"Name": "instance-state-name",
                  "Values": ["pending", "running", "stopping",
                             "stopped", "shutting-down"]}]
    )
    instances = [i
                 for r in resp["Reservations"]
                 for i in r["Instances"]]

    if len(instances) == 0:
        raise ValueError(f"No instance found with Name tag '{name}'")
    if len(instances) > 1:
        raise ValueError(
            f"Multiple instances ({len(instances)}) found with Name tag '{name}' "
            f"– Name tags must be unique.")
    inst = instances[0]
    if inst["State"]["Name"] == "terminated":
        raise ValueError(f"Instance '{name}' ({inst['InstanceId']}) is terminated.")
    return inst


def check_ssm_managed(instance_id: str, ssm) -> bool:
    resp = ssm.describe_instance_information(
        Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
    )
    items = resp.get("InstanceInformationList", [])
    return len(items) > 0


def validate_instances(names: list[str]) -> list[dict]:
    """
    Validate all names. Returns list of info dicts on full success.
    Exits on any validation failure.
    """
    ec2 = get_ec2()
    ssm = get_ssm()

    log.info("=" * 60)
    log.info("PHASE 1 – VALIDATION (read-only)")
    log.info("=" * 60)

    results = []
    errors = []

    for name in names:
        try:
            inst = resolve_instance_by_name(name, ec2)
            instance_id = inst["InstanceId"]
            state = inst["State"]["Name"]
            az = inst["Placement"]["AvailabilityZone"]
            itype = inst["InstanceType"]
            ssm_managed = check_ssm_managed(instance_id, ssm)

            info = {
                "name": name,
                "instance_id": instance_id,
                "instance_type": itype,
                "availability_zone": az,
                "current_state": state,
                "ssm_managed": ssm_managed,
                "original_state": state,
            }
            results.append(info)

            ssm_label = "✓ SSM Managed" if ssm_managed else "✗ NOT SSM Managed"
            log.info("  %-40s  %-20s  %-10s  %-18s  %s",
                     name, instance_id, state, az, ssm_label)

            if not ssm_managed:
                errors.append(f"Instance '{name}' ({instance_id}) is NOT managed by SSM.")

        except (ValueError, ClientError) as exc:
            log.error("  VALIDATION FAILED for '%s': %s", name, exc)
            errors.append(str(exc))

    if errors:
        log.error("\nValidation failed with %d error(s):", len(errors))
        for e in errors:
            log.error("  - %s", e)
        log.error("No changes made. Exiting.")
        sys.exit(1)

    log.info("\nValidation passed for all %d instances.", len(results))
    return results

# ---------------------------------------------------------------------------
# Original state tracking
# ---------------------------------------------------------------------------

def save_original_state(instances: list[dict]) -> None:
    state = {
        i["instance_id"]: {
            "name": i["name"],
            "original_state": i["original_state"],
        }
        for i in instances
    }
    write_json(STATE_FILE, state)
    log.info("Original state saved to %s", STATE_FILE)


def load_original_state() -> dict:
    return read_json(STATE_FILE)

# ---------------------------------------------------------------------------
# EC2 state transitions
# ---------------------------------------------------------------------------

def wait_for_ec2_state(instance_id: str, target_state: str,
                       timeout: int, ec2) -> bool:
    """Poll until instance reaches target_state. Returns True on success."""
    deadline = time.monotonic() + timeout
    log.info("    Waiting for %s to reach EC2 state '%s' …",
             instance_id, target_state)
    while time.monotonic() < deadline:
        resp = ec2.describe_instances(InstanceIds=[instance_id])
        state = resp["Reservations"][0]["Instances"][0]["State"]["Name"]
        log.debug("    %s state: %s", instance_id, state)
        if state == target_state:
            log.info("    %s reached '%s'.", instance_id, target_state)
            return True
        if state in ("terminated", "shutting-down") and target_state == "running":
            log.error("    %s entered unexpected state '%s'.", instance_id, state)
            return False
        time.sleep(POLL_INTERVAL_SEC)
    log.error("    Timeout waiting for %s to reach '%s'.", instance_id, target_state)
    return False


def wait_for_ssm_online(instance_id: str, timeout: int, ssm) -> bool:
    """Poll until SSM reports the instance as Online."""
    deadline = time.monotonic() + timeout
    log.info("    Waiting for %s to become SSM Online …", instance_id)
    while time.monotonic() < deadline:
        resp = ssm.describe_instance_information(
            Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
        )
        items = resp.get("InstanceInformationList", [])
        if items and items[0].get("PingStatus") == "Online":
            log.info("    %s is SSM Online.", instance_id)
            return True
        time.sleep(POLL_INTERVAL_SEC)
    log.error("    Timeout waiting for %s to become SSM Online.", instance_id)
    return False


def start_instance(instance: dict, ec2, ssm) -> bool:
    """
    Start a single stopped instance and wait for running + SSM Online.
    Returns True on success, False on failure.
    Raises RuntimeError with 'InsufficientInstanceCapacity' in message if capacity error.
    """
    iid = instance["instance_id"]
    name = instance["name"]
    log.info("  Starting instance %s (%s) …", name, iid)

    try:
        ec2.start_instances(InstanceIds=[iid])
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code == "InsufficientInstanceCapacity":
            raise RuntimeError(
                f"InsufficientInstanceCapacity for {name} ({iid}): {exc}"
            ) from exc
        raise

    ok = wait_for_ec2_state(iid, "running", EC2_RUNNING_TIMEOUT_SEC, ec2)
    if not ok:
        return False

    ok = wait_for_ssm_online(iid, SSM_ONLINE_TIMEOUT_SEC, ssm)
    return ok


def stop_instance(instance_id: str, ec2) -> bool:
    """Stop an instance and wait until stopped. Returns True on success."""
    log.info("  Stopping instance %s …", instance_id)
    try:
        ec2.stop_instances(InstanceIds=[instance_id])
    except ClientError as exc:
        log.error("  Failed to stop %s: %s", instance_id, exc)
        return False
    return wait_for_ec2_state(instance_id, "stopped", EC2_STOPPED_TIMEOUT_SEC, ec2)


def restore_batch_to_stopped(batch: list[dict], ec2) -> None:
    """
    Stop any instance in the batch that was originally stopped
    and is now running. Best-effort; logs errors but does not raise.
    """
    log.info("  Restoring originally-stopped instances to stopped state …")
    for inst in batch:
        if inst["original_state"] != "stopped":
            continue
        iid = inst["instance_id"]
        # Check current state
        try:
            resp = ec2.describe_instances(InstanceIds=[iid])
            cur = resp["Reservations"][0]["Instances"][0]["State"]["Name"]
        except ClientError as exc:
            log.error("    Cannot check state of %s: %s", iid, exc)
            continue

        if cur in ("running", "pending"):
            ok = stop_instance(iid, ec2)
            if ok:
                log.info("    %s (%s) restored to stopped.",
                         inst["name"], iid)
            else:
                log.error("    FAILED to stop %s (%s) – manual intervention needed.",
                          inst["name"], iid)
        else:
            log.info("    %s (%s) is already %s – no action needed.",
                     inst["name"], iid, cur)

# ---------------------------------------------------------------------------
# Patching
# ---------------------------------------------------------------------------

def send_patch_command(instance_ids: list[str], ssm) -> str:
    """Send AWS-RunPatchBaseline to the given instance IDs. Returns Command ID."""
    log.info("  Sending Run Command to %d instance(s) …", len(instance_ids))
    resp = ssm.send_command(
        InstanceIds=instance_ids,
        DocumentName=SSM_DOCUMENT,
        Parameters=PATCH_PARAMETERS,
        Comment=f"ec2_patch.py – {ts()}",
        TimeoutSeconds=PATCH_TIMEOUT_SEC,
    )
    cmd_id = resp["Command"]["CommandId"]
    log.info("  Command ID: %s", cmd_id)
    return cmd_id


def poll_patch_command(command_id: str, batch: list[dict], ssm) -> dict[str, str]:
    """
    Poll until all invocations reach a terminal status.
    Returns dict of {instance_id: final_status}.
    """
    log.info("  Monitoring patch command %s …", command_id)
    instance_ids = [i["instance_id"] for i in batch]
    statuses: dict[str, str] = {iid: "Pending" for iid in instance_ids}
    deadline = time.monotonic() + PATCH_TIMEOUT_SEC + 60  # small grace period

    while time.monotonic() < deadline:
        all_done = True
        for iid in instance_ids:
            if statuses[iid] in TERMINAL_SSM_STATUSES:
                continue
            all_done = False
            try:
                resp = ssm.get_command_invocation(
                    CommandId=command_id, InstanceId=iid
                )
                status = resp["StatusDetails"]
                if status != statuses[iid]:
                    log.info("    %s → %s", iid, status)
                    statuses[iid] = status
            except ssm.exceptions.InvocationDoesNotExist:
                pass  # Not yet registered
            except ClientError as exc:
                log.warning("    Error polling %s: %s", iid, exc)

        # Pretty-print current state table
        lines = ["    Current patch status:"]
        for inst in batch:
            iid = inst["instance_id"]
            lines.append(f"      {inst['name']:<40} {iid}  {statuses[iid]}")
        log.info("\n".join(lines))

        if all_done:
            break
        time.sleep(POLL_INTERVAL_SEC)
    else:
        log.error("  Polling loop exceeded deadline for command %s.", command_id)

    return statuses

# ---------------------------------------------------------------------------
# Batch orchestration
# ---------------------------------------------------------------------------

def process_batch(batch_num: int, batch: list[dict],
                  ec2, ssm) -> bool:
    """
    Orchestrate a single batch:
      1. Confirm with operator.
      2. Start stopped instances one-at-a-time.
      3. Patch.
      4. Restore originally-stopped instances.

    Returns True if batch succeeded fully, False otherwise.
    """
    log.info("\n" + "=" * 60)
    log.info("BATCH %d", batch_num)
    log.info("=" * 60)
    for inst in batch:
        log.info("  %s  (%s)  originally: %s",
                 inst["name"], inst["instance_id"], inst["original_state"])

    if not confirm(f"Proceed with Batch {batch_num}?"):
        log.info("Operator declined. Exiting safely.")
        sys.exit(0)

    # ---- Start stopped instances one-at-a-time ----
    started_in_this_batch: list[dict] = []
    for inst in batch:
        if inst["original_state"] != "stopped":
            log.info("  %s is already running – skipping start.",
                     inst["name"])
            continue
        try:
            ok = start_instance(inst, ec2, ssm)
        except RuntimeError as exc:
            if "InsufficientInstanceCapacity" in str(exc):
                log.error("\n⛔  INSUFFICIENT CAPACITY: %s", exc)
                log.error("Restoring batch to original state …")
                restore_batch_to_stopped(started_in_this_batch, ec2)
                log.error("Exiting. No further changes made.")
                sys.exit(1)
            raise

        if not ok:
            log.error("  Failed to bring %s online. Restoring batch …",
                      inst["name"])
            restore_batch_to_stopped(started_in_this_batch, ec2)
            log.error("Exiting due to instance start failure.")
            sys.exit(1)

        started_in_this_batch.append(inst)

    # ---- Patch ----
    instance_ids = [i["instance_id"] for i in batch]
    try:
        command_id = send_patch_command(instance_ids, ssm)
    except ClientError as exc:
        log.error("Failed to send patch command: %s", exc)
        restore_batch_to_stopped(batch, ec2)
        sys.exit(1)

    statuses = poll_patch_command(command_id, batch, ssm)

    # ---- Evaluate results ----
    failures = {iid: s for iid, s in statuses.items()
                if s in FAILURE_SSM_STATUSES}
    batch_ok = len(failures) == 0

    # ---- Restore originally-stopped instances ----
    log.info("\n  Post-patch: restoring originally-stopped instances …")
    for inst in batch:
        if inst["original_state"] == "stopped":
            ok = stop_instance(inst["instance_id"], ec2)
            inst["returned_to_stopped"] = ok
            if ok:
                log.info("  %s returned to stopped.", inst["name"])
            else:
                log.error("  FAILED to stop %s – manual action required.",
                          inst["name"])
        else:
            inst["returned_to_stopped"] = None  # was running, leave running

    # ---- Batch summary ----
    log.info("\n  Batch %d summary:", batch_num)
    batch_result = {"command_id": command_id, "instances": []}
    for inst in batch:
        iid = inst["instance_id"]
        final_status = statuses.get(iid, "Unknown")
        orig = inst["original_state"]
        if orig == "stopped":
            disposition = (
                "returned to stopped"
                if inst.get("returned_to_stopped")
                else "FAILED to stop – manual action required"
            )
        else:
            disposition = "remained running"
        log.info("  %-40s  %-20s  %s",
                 inst["name"], final_status, disposition)
        batch_result["instances"].append({
            "name": inst["name"],
            "instance_id": iid,
            "patch_status": final_status,
            "disposition": disposition,
        })

    append_batch_result(batch_num, batch_result)

    if failures:
        log.error("\n⛔  Batch %d had failures: %s", batch_num, failures)
        log.error("Stopping. Do NOT proceed to the next batch.")
        return False

    log.info("\n✓  Batch %d completed successfully.", batch_num)
    return True

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Production-safe Windows EC2 patching via SSM Run Command."
    )
    parser.add_argument(
        "--input", default=INPUT_FILE,
        help=f"Path to instance name list (default: {INPUT_FILE})"
    )
    parser.add_argument(
        "--batch-size", type=int, default=BATCH_SIZE,
        help=f"Instances per batch (default: {BATCH_SIZE})"
    )
    return parser.parse_args()


def read_instance_names(path: str) -> list[str]:
    lines = Path(path).read_text().splitlines()
    names = [l.strip() for l in lines if l.strip()]
    if not names:
        log.error("Input file '%s' is empty.", path)
        sys.exit(1)
    log.info("Read %d instance name(s) from %s.", len(names), path)
    return names


def build_batches(instances: list[dict], size: int) -> list[list[dict]]:
    return [instances[i:i + size] for i in range(0, len(instances), size)]


def display_plan(batches: list[list[dict]]) -> None:
    log.info("\nPatching plan:")
    for idx, batch in enumerate(batches, 1):
        log.info("  Batch %d:", idx)
        for inst in batch:
            log.info("    %s  (%s)  [%s]",
                     inst["name"], inst["instance_id"], inst["original_state"])


def main():
    args = parse_args()
    log.info("ec2_patch.py started at %s", ts())
    log.info("Log file: %s", LOG_FILE)

    # Read input
    names = read_instance_names(args.input)

    # Phase 1 – Validation
    ec2 = get_ec2()
    ssm = get_ssm()
    instances = validate_instances(names)

    # Save original state immediately after validation (before any mutation)
    save_original_state(instances)

    # Build and display plan
    batches = build_batches(instances, args.batch_size)
    display_plan(batches)

    # Process batches
    total = len(batches)
    for batch_num, batch in enumerate(batches, 1):
        success = process_batch(batch_num, batch, ec2, ssm)
        if not success:
            log.error("Halting after Batch %d failure. "
                      "Remaining batches NOT processed.", batch_num)
            sys.exit(1)

        if batch_num < total:
            if not confirm(f"Proceed to Batch {batch_num + 1}?"):
                log.info("Operator chose to stop after Batch %d. Exiting safely.",
                         batch_num)
                sys.exit(0)

    log.info("\n✓  All %d batch(es) completed successfully at %s.", total, ts())
    log.info("Logs: %s   State: %s   Results: %s",
             LOG_FILE, STATE_FILE, RESULTS_FILE)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.warning("\nInterrupted by operator (Ctrl-C).")
        log.warning("Check %s to determine original states and perform "
                    "any manual restoration needed.", STATE_FILE)
        sys.exit(130)
    except Exception as exc:
        log.exception("Unhandled exception: %s", exc)
        log.error("Check %s for original instance states.", STATE_FILE)
        sys.exit(1)
