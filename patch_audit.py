import boto3
import csv
from datetime import datetime

ssm = boto3.client("ssm")

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

output_file = f"patch_audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

# ✅ Step 1: Get only SSM managed instances
def get_managed_instances():
    managed = set()
    paginator = ssm.get_paginator("describe_instance_information")

    for page in paginator.paginate():
        for inst in page["InstanceInformationList"]:
            managed.add(inst["InstanceId"])

    return managed

# ✅ Step 2: Get patch summary
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

# ✅ Step 3: Get patch details
def get_patches(instance_id, state):
    patches = []
    paginator = ssm.get_paginator("describe_instance_patches")

    try:
        for page in paginator.paginate(
            InstanceId=instance_id,
            Filters=[{"Key": "State", "Values": [state]}]
        ):
            patches.extend(page["Patches"])
    except:
        pass

    return patches


managed_instances = get_managed_instances()

with open(output_file, "w", newline="") as csvfile:
    writer = csv.writer(csvfile)

    writer.writerow([
        "InstanceId",
        "OperatingSystem",
        "ComplianceState",
        "InstalledCount",
        "MissingCount",
        "FailedCount",
        "PatchState",
        "PatchTitle",
        "KBId",
        "Classification",
        "Severity",
        "Reason"
    ])

    for instance_id in INSTANCE_IDS:

        # ❗ Skip non-SSM instances
        if instance_id not in managed_instances:
            print(f"Skipping (not SSM managed): {instance_id}")
            continue

        summary = get_patch_summary(instance_id)

        if not summary:
            print(f"No patch data found: {instance_id}")
            continue

        os = summary.get("OperatingSystem", "Unknown")
        compliance = summary.get("ComplianceState", "Unknown")
        installed_count = summary.get("InstalledCount", 0)
        missing_count = summary.get("MissingCount", 0)
        failed_count = summary.get("FailedCount", 0)

        # Installed patches
        installed = get_patches(instance_id, "Installed")
        for p in installed:
            writer.writerow([
                instance_id,
                os,
                compliance,
                installed_count,
                missing_count,
                failed_count,
                "Installed",
                p.get("Title"),
                p.get("KBId"),
                p.get("Classification"),
                p.get("Severity"),
                "Installed"
            ])

        # Missing patches
        missing = get_patches(instance_id, "Missing")
        for p in missing:
            writer.writerow([
                instance_id,
                os,
                compliance,
                installed_count,
                missing_count,
                failed_count,
                "Missing",
                p.get("Title"),
                p.get("KBId"),
                p.get("Classification"),
                p.get("Severity"),
                "Missing - likely pending approval"
            ])

print(f"\nCSV report generated: {output_file}")
