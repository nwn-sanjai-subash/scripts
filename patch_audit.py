import boto3
import csv
from datetime import datetime

ssm = boto3.client("ssm")
ec2 = boto3.client("ec2")

INSTANCE_IDS = [
    "i-0dff42f5f8f9ead6e",
    "i-0561a26ca101c8837",
    "i-0789137251d9055e9",
    "i-0405748e8aa0664e1",
    "i-05a38e5d40c619e7b",
    "i-00092351dcb505ec0",
    "i-0c601c575fe3d9c0e",
    "i-0b6ab5f48ed85f57b",
    "i-0ae0df63540a92f86",
    "i-092e505acbbc109cf",
    "i-0d5b123d37bac51e4",
    "i-0c2bcb07a381a6b5a"
]

output_file = f"patch_summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"


# ✅ Get instance names
def get_instance_names():
    names = {}
    response = ec2.describe_instances(InstanceIds=INSTANCE_IDS)

    for res in response["Reservations"]:
        for inst in res["Instances"]:
            name = "N/A"
            for tag in inst.get("Tags", []):
                if tag["Key"] == "Name":
                    name = tag["Value"]
                    break
            names[inst["InstanceId"]] = name

    return names


# ✅ Get patch summary
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


# ✅ Get available (pending) patches → IMPORTANT FIX
def get_available_patches(instance_id):
    patches = []
    paginator = ssm.get_paginator("describe_instance_patches")

    try:
        for page in paginator.paginate(
            InstanceId=instance_id,
            Filters=[{"Key": "State", "Values": ["Available"]}]
        ):
            patches.extend(page["Patches"])
    except:
        pass

    return patches


instance_names = get_instance_names()

with open(output_file, "w", newline="") as csvfile:
    writer = csv.writer(csvfile)

    writer.writerow(["Instance", "Name", "Compliance", "Patch", "KB", "Reason"])

    for instance_id in INSTANCE_IDS:

        name = instance_names.get(instance_id, "N/A")
        summary = get_patch_summary(instance_id)

        if not summary:
            writer.writerow([instance_id, name, "NoData", "-", "-", "No patch data"])
            continue

        # ✅ Correct compliance logic
        if summary.get("MissingCount", 0) > 0 or summary.get("FailedCount", 0) > 0:
            compliance = "NON_COMPLIANT"
        else:
            compliance = "COMPLIANT"

        available_security = summary.get("AvailableSecurityUpdateCount", 0)
        security_non_compliant = summary.get("SecurityNonCompliantCount", 0)

        patches = get_available_patches(instance_id)

        if not patches:
            writer.writerow([instance_id, name, compliance, "-", "-", "Fully compliant"])
            continue

        for p in patches:
            title = p.get("Title", "N/A")
            kb = p.get("KBId") or p.get("Id") or "N/A"  # ✅ fallback fix

            # ✅ Reason logic (based on real AWS fields)
            if security_non_compliant > 0:
                reason = "🔴 Security update pending (needs attention)"
            elif available_security > 0:
                reason = "🟡 Pending approval (baseline delay)"
            else:
                reason = "⚠️ Review required"

            writer.writerow([instance_id, name, compliance, title, kb, reason])

print(f"\nCSV report generated: {output_file}")
