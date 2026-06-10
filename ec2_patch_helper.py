#!/usr/bin/env python3
import json
import logging
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

INPUT_FILE = "instances.txt"
BATCH_SIZE = 5
POLL_INTERVAL = 30
EC2_TIMEOUT = 900
SSM_TIMEOUT = 900

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("patch_helper")

ec2 = boto3.client("ec2")
ssm = boto3.client("ssm")


def confirm(prompt):
    return input(f"{prompt} (yes/no): ").strip().lower() == "yes"


def read_instances():
    path = Path(INPUT_FILE)
    if not path.exists():
        log.error("%s not found", INPUT_FILE)
        sys.exit(1)

    names = [x.strip() for x in path.read_text().splitlines() if x.strip()]
    if not names:
        log.error("%s is empty", INPUT_FILE)
        sys.exit(1)

    return names


def resolve_instance(name):
    resp = ec2.describe_instances(
        Filters=[
            {"Name": "tag:Name", "Values": [name]},
            {
                "Name": "instance-state-name",
                "Values": ["stopped", "running", "pending", "stopping"],
            },
        ]
    )

    instances = [
        i
        for r in resp["Reservations"]
        for i in r["Instances"]
    ]

    if len(instances) == 0:
        raise ValueError(f"No instance found for {name}")

    if len(instances) > 1:
        raise ValueError(f"Multiple instances found for {name}")

    inst = instances[0]

    return {
        "name": name,
        "instance_id": inst["InstanceId"],
        "state": inst["State"]["Name"],
    }


def build_batches(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]


def wait_for_ec2(instance_id, target):
    deadline = time.time() + EC2_TIMEOUT

    while time.time() < deadline:
        state = ec2.describe_instances(
            InstanceIds=[instance_id]
        )["Reservations"][0]["Instances"][0]["State"]["Name"]

        if state == target:
            return True

        time.sleep(POLL_INTERVAL)

    return False


def wait_for_ssm(instance_id):
    deadline = time.time() + SSM_TIMEOUT

    while time.time() < deadline:
        try:
            resp = ssm.describe_instance_information(
                Filters=[
                    {
                        "Key": "InstanceIds",
                        "Values": [instance_id],
                    }
                ]
            )

            items = resp.get("InstanceInformationList", [])

            if items and items[0].get("PingStatus") == "Online":
                return True

        except ClientError:
            pass

        time.sleep(POLL_INTERVAL)

    return False


def start_batch(batch):
    ids = [x["instance_id"] for x in batch]

    log.info("Starting instances...")
    ec2.start_instances(InstanceIds=ids)

    for inst in batch:
        log.info("Waiting for %s to reach running", inst["name"])

        if not wait_for_ec2(inst["instance_id"], "running"):
            raise RuntimeError(
                f"{inst['name']} failed to reach running state"
            )

        log.info("Waiting for %s to become SSM Online", inst["name"])

        if not wait_for_ssm(inst["instance_id"]):
            raise RuntimeError(
                f"{inst['name']} failed to become SSM Online"
            )


def stop_batch(batch):
    ids = [x["instance_id"] for x in batch]

    log.info("Stopping instances...")
    ec2.stop_instances(InstanceIds=ids)

    for inst in batch:
        log.info("Waiting for %s to stop", inst["name"])

        if not wait_for_ec2(inst["instance_id"], "stopped"):
            log.warning(
                "%s did not stop within timeout",
                inst["name"],
            )


def main():
    names = read_instances()

    instances = []

    for name in names:
        try:
            inst = resolve_instance(name)

            if inst["state"] != "stopped":
                log.error(
                    "%s (%s) is %s. "
                    "Only stopped instances are supported.",
                    inst["name"],
                    inst["instance_id"],
                    inst["state"],
                )
                sys.exit(1)

            instances.append(inst)

        except Exception as exc:
            log.error("%s", exc)
            sys.exit(1)

    batches = build_batches(instances, BATCH_SIZE)

    log.info("Total instances: %s", len(instances))
    log.info("Total batches: %s", len(batches))

    for idx, batch in enumerate(batches, start=1):
        print("\n" + "=" * 60)
        print(f"Batch {idx} of {len(batches)}")
        print("=" * 60)

        for inst in batch:
            print(
                f"{inst['name']} "
                f"({inst['instance_id']})"
            )

        if not confirm("Start this batch"):
            log.info("Exiting")
            sys.exit(0)

        try:
            start_batch(batch)

        except Exception as exc:
            log.error("%s", exc)

            if confirm(
                "Attempt to stop any started instances"
            ):
                stop_batch(batch)

            sys.exit(1)

        print("\nInstances are ready for patching.")
        print(
            "Use Systems Manager → Patch Manager "
            "→ Patch Now."
        )

        input(
            "\nPress Enter after patching has completed..."
        )

        if confirm("Stop this batch"):
            stop_batch(batch)

        else:
            log.warning(
                "Batch left running. "
                "Manual stop required."
            )

        if idx < len(batches):
            if not confirm("Proceed to next batch"):
                log.info("Exiting after Batch %s", idx)
                sys.exit(0)

    log.info("All batches processed")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.warning("Interrupted by operator")
        sys.exit(130)
