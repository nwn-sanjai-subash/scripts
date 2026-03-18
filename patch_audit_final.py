import boto3
import csv
from datetime import datetime

ssm = boto3.client("ssm")
ec2 = boto3.client("ec2")

output_file = f"patch_summary_clean_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"


def get_all_instances():
    instance_ids = []
    instance_names = {}

    paginator = ec2.get_paginator("describe_instances")

    for page in paginator.paginate():
        for res in page["Reservations"]:
            for inst in res["Instances"]:
                instance_id = inst["InstanceId"]
                instance_ids.append(instance_id)

                name = "N/A"
                for tag in inst.get("Tags", []):
                    if tag["Key"] == "Name":
                        name = tag["Value"]
                        break

                instance_names[instance_id] = name

    return instance_ids, instance_names


def get_patch_summary(instance_id):
    try:
        response = ssm.describe_instance_patch_states(
            InstanceIds=[instance_id]
        )
        if response["InstancePatchStates"]:
            return response["InstancePatchStates"][0]
    except:
        pass
    return None


def get_security_updates(instance_id):
    patches = []
    paginator = ssm.get_paginator("describe_instance_patches")

    try:
        for page in paginator.paginate(
            InstanceId=instance_id,
            Filters=[{"Key": "State", "Values": ["AvailableSecurityUpdate"]}]
        ):
            patches.extend(page["Patches"])
    except:
        pass

    return patches


instance_ids, instance_names = get_all_instances()

with open(output_file, "w", newline="") as csvfile:
    writer = csv.writer(csvfile)

    writer.writerow([
        "Instance",
        "Name",
        "Compliance",
        "PendingPatch",
        "PendingKB",
        "Status"
    ])

    for instance_id in instance_ids:

        name = instance_names.get(instance_id, "N/A")
        summary = get_patch_summary(instance_id)

        if not summary:
            writer.writerow([instance_id, name, "NoData", "-", "-", "No patch data"])
            continue

        if summary.get("MissingCount", 0) > 0 or summary.get("FailedCount", 0) > 0:
            compliance = "NON_COMPLIANT"
        else:
            compliance = "COMPLIANT"

        security_updates = get_security_updates(instance_id)

        if not security_updates:
            writer.writerow([
                instance_id,
                name,
                compliance,
                "-",
                "-",
                "Fully compliant"
            ])
            continue

        for p in security_updates:
            title = p.get("Title", "N/A")
            kb = p.get("KBId") or p.get("Id") or "N/A"

            if compliance == "COMPLIANT":
                status = "Pending approval (baseline delay)"
            else:
                status = "Missing approved patch - action required"

            writer.writerow([
                instance_id,
                name,
                compliance,
                title,
                kb,
                status
            ])

print(f"\nCSV report generated: {output_file}")
