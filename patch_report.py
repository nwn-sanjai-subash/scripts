import boto3
from datetime import datetime

ssm = boto3.client("ssm")
ec2 = boto3.client("ec2")


# ✅ Load instance IDs from file
def load_instance_ids(file_path):
    with open(file_path, "r") as f:
        return [line.strip() for line in f if line.strip()]


INSTANCE_IDS = load_instance_ids("instances.txt")


# ✅ Get instance names
def get_instance_names(instance_ids):
    names = {}

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


# ✅ Get patch state
def get_patch_state(instance_id):
    try:
        response = ssm.describe_instance_patch_states(
            InstanceIds=[instance_id]
        )
        if response["InstancePatchStates"]:
            return response["InstancePatchStates"][0]
    except:
        pass
    return None


# 🔹 Main execution
instance_names = get_instance_names(INSTANCE_IDS)

print("\nPatch Pre-Check Report\n")

# Header
print(f"{'Instance ID':<20} {'Name':<30} {'Compliance':<15} {'Missing':<8} {'SecNC':<6} {'Avail':<8} {'Status'}")
print("-" * 110)

for instance_id in INSTANCE_IDS:

    name = instance_names.get(instance_id, "N/A")
    state = get_patch_state(instance_id)

    if not state:
        print(f"{instance_id:<20} {name:<30} {'NoData':<15} {'-':<8} {'-':<6} {'-':<8} {'No patch data'}")
        continue

    missing = state.get("MissingCount", 0)
    failed = state.get("FailedCount", 0)
    security_nc = state.get("SecurityNonCompliantCount", 0)
    available = state.get("AvailableSecurityUpdateCount", 0)

    if missing > 0 or failed > 0:
        compliance = "NON_COMPLIANT"
        status = "Needs patching"
    elif security_nc > 0:
        compliance = "COMPLIANT"
        status = "Pending updates"
    else:
        compliance = "COMPLIANT"
        status = "Fully compliant"

    print(f"{instance_id:<20} {name:<30} {compliance:<15} {missing:<8} {security_nc:<6} {available:<8} {status}")
