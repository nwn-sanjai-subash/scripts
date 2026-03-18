import boto3
import csv
from datetime import datetime

ssm = boto3.client("ssm")
ec2 = boto3.client("ec2")

output_file = f"patch_summary_all_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"


# ✅ Step 1: Get all SSM managed instances
def get_all_managed_instances():
    instance_ids = []
    paginator = ssm.get_paginator("describe_instance_information")

    for page in paginator.paginate():
        for inst in page["InstanceInformationList"]:
            instance_ids.append(inst["InstanceId"])

    return instance_ids


# ✅ Step 2: Get instance names
def get_instance_names(instance_ids):
    names = {}

    # EC2 API supports max 100 at a time
    for i in range(0, len(instance_ids), 100):
        batch = instance_ids[i:i+100]

        response = ec2.describe_instances(InstanceIds=batch)

        for res in response["Reservations"]:
            for inst in res["Instances"]:
                name = "N/A"
                for tag in inst.get("Tags", []):
                    if tag["Key"] == "Name":
                        name = tag["Value"]
                        break
                names[inst["InstanceId"]] = name

    return names


# ✅ Step 3: Patch summary
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


# ✅ Step 4: Pending security updates
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


# 🔹 MAIN FLOW

instance_ids = get_all_managed_instances()
instance_names = get_instance_names(instance_ids)

with open(output_file, "w", newline="") as csvfile:
    writer = csv.writer(csvfile)

    writer.writerow(["Instance", "Name", "Compliance", "Patch", "KB", "Reason"])

    for instance_id in instance_ids:

        name = instance_names.get(instance_id, "N/A")
        summary = get_patch_summary(instance_id)

        if not summary:
            writer.writerow([instance_id, name, "NoData", "-", "-", "No patch data"])
            continue

        # ✅ Compliance logic
        if summary.get("MissingCount", 0) > 0 or summary.get("FailedCount", 0) > 0:
            compliance = "NON_COMPLIANT"
        else:
            compliance = "COMPLIANT"

        security_updates = get_security_updates(instance_id)

        # ✅ No pending updates
        if not security_updates:
            writer.writerow([instance_id, name, compliance, "-", "-", "Fully compliant"])
            continue

        # ✅ Pending updates
        for p in security_updates:
            title = p.get("Title", "N/A")
            kb = p.get("KBId") or p.get("Id") or "N/A"

            if compliance == "COMPLIANT":
                reason = "🟡 Pending approval (baseline delay)"
            else:
                reason = "🔴 Missing approved patch (needs attention)"

            writer.writerow([
                instance_id,
                name,
                compliance,
                title,
                kb,
                reason
            ])

print(f"\nCSV report generated: {output_file}")
