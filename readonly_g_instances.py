#!/usr/bin/env python3
"""
readonly_g_instances.py

Read-only script to list all EC2 G-family instances (g3, g4dn, g5, etc.)
in the current AWS account and region.

This script DOES NOT make any changes.

Usage:
    python3 readonly_g_instances.py
"""

import boto3
from botocore.exceptions import ClientError

# G-family prefixes to include
G_INSTANCE_PREFIXES = (
    "g2.",
    "g3.",
    "g4ad.",
    "g4dn.",
    "g5.",
    "g5g.",
    "gr6.",
)

ec2 = boto3.client("ec2")


def get_name_tag(tags):
    """Extract Name tag value."""
    if not tags:
        return "N/A"

    for tag in tags:
        if tag.get("Key") == "Name":
            return tag.get("Value")

    return "N/A"


def main():
    print("\nFetching G-family instances (read-only)...\n")

    paginator = ec2.get_paginator("describe_instances")

    instances = []

    try:
        for page in paginator.paginate():

            for reservation in page["Reservations"]:

                for instance in reservation["Instances"]:

                    instance_type = instance["InstanceType"]

                    if not instance_type.startswith(G_INSTANCE_PREFIXES):
                        continue

                    instances.append(
                        {
                            "Name": get_name_tag(instance.get("Tags", [])),
                            "InstanceId": instance["InstanceId"],
                            "InstanceType": instance_type,
                            "State": instance["State"]["Name"],
                            "AZ": instance["Placement"]["AvailabilityZone"],
                        }
                    )

    except ClientError as e:
        print(f"AWS Error: {e}")
        return

    if not instances:
        print("No G-family instances found.")
        return

    print(
        f"{'Name':40} "
        f"{'Instance ID':20} "
        f"{'Type':15} "
        f"{'State':12} "
        f"{'AZ':15}"
    )

    print("-" * 110)

    for instance in sorted(
        instances,
        key=lambda x: (
            x["InstanceType"],
            x["State"],
            x["Name"],
        ),
    ):

        print(
            f"{instance['Name'][:40]:40} "
            f"{instance['InstanceId']:20} "
            f"{instance['InstanceType']:15} "
            f"{instance['State']:12} "
            f"{instance['AZ']:15}"
        )

    print("\nSummary")
    print("=" * 50)

    print(f"Total G-family instances: {len(instances)}")

    state_summary = {}

    for instance in instances:
        state_summary[instance["State"]] = (
            state_summary.get(instance["State"], 0) + 1
        )

    print("\nBy State:")

    for state, count in sorted(state_summary.items()):
        print(f"  {state:<12}: {count}")

    type_summary = {}

    for instance in instances:
        type_summary[instance["InstanceType"]] = (
            type_summary.get(instance["InstanceType"], 0) + 1
        )

    print("\nBy Instance Type:")

    for instance_type, count in sorted(type_summary.items()):
        print(f"  {instance_type:<15}: {count}")


if __name__ == "__main__":
    main()
