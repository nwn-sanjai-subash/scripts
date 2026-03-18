import boto3
import csv
from datetime import datetime, timezone

ssm = boto3.client("ssm")
ec2 = boto3.client("ec2")

# 🔹 Your instances
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

# 🔹 Set your baseline approval delay (IMPORTANT - adjust if needed)
APPROVAL_DELAY_DAYS = 7

output_file = f"patch_detailed_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"


# ✅ Get instance names
def get_instance_names():
    names = {}
    response = ec2.describe_instances(InstanceIds=INSTANCE_IDS)

    for res in response["Reservations"]:
        for inst in res["Instances"]:
            instance_id = inst["InstanceId"]
            name = "N/A"

            if "Tags" in inst:
                for tag in inst["Tags"]:
                    if tag["Key"] == "Name":
                        name = tag["Value"]
                        break

            names[instance_id] = name

    return names


# ✅ Patch summary
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


# ✅ Missing patches
def get_missing_patches(instance_id):
    patches = []
    paginator = ssm.get_paginator("describe_instance_patches")

    try:
        for page in paginator.paginate(
            InstanceId=instance_id,
            Filters=[{"Key": "State", "Values": ["Missing"]}]
        ):
            patches.extend(page["Patches"])
    except:
        pass

    return patches


instance_names = get_instance_names()
today = datetime.now(timezone.utc)

with open(output_file, "w", newline="") as csvfile:
    writer = csv.writer(csvfile)

    writer.writerow([
        "InstanceId",
        "InstanceName",
        "Patched",
        "ComplianceStatus",
        "MissingPatch",
        "KBId",
        "Severity",
        "ReleaseDate",
        "DaysSinceRelease",
        "Reason"
    ])

    for instance_id in INSTANCE_IDS:

        summary = get_patch_summary(instance_id)

        name = instance_names.get(instance_id, "N/A")

        if not summary:
            writer.writerow([instance_id, name, "No Data", "Unknown", "-", "-", "-", "-", "-", "No patch data"])
            continue

        compliance = summary.get("ComplianceState", "Unknown")
        missing_count = summary.get("MissingCount", 0)

        patched = "Yes" if compliance == "COMPLIANT" else "No"

        missing_patches = get_missing_patches(instance_id)

        if not missing_patches:
            writer.writerow([instance_id, name, patched, compliance, "-", "-", "-", "-", "-", "-"])
            continue

        for p in missing_patches:

            release_date = p.get("ReleaseDate")
            severity = p.get("Severity")
            title = p.get("Title")
            kb = p.get("KBId")

            days_since_release = "N/A"
            reason = "Unknown"

            if release_date:
                delta = today - release_date
                days_since_release = delta.days

                if delta.days < APPROVAL_DELAY_DAYS:
                    reason = "🟡 Pending approval"
                else:
                    if severity in ["Critical", "Important"]:
                        reason = "🔴 Critical missing - needs attention"
                    else:
                        reason = "Not approved / needs review"

            writer.writerow([
                instance_id,
                name,
                patched,
                compliance,
                title,
                kb,
                severity,
                release_date,
                days_since_release,
                reason
            ])

print(f"\nDetailed CSV report generated: {output_file}")
