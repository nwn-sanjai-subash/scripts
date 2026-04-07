import boto3

backup = boto3.client('backup')
ec2 = boto3.client('ec2')


# -------------------------------
# Get all EC2 instances
# -------------------------------
def get_all_ec2_instances():
    instances = []
    paginator = ec2.get_paginator('describe_instances')

    for page in paginator.paginate():
        for res in page['Reservations']:
            for inst in res['Instances']:
                instance_id = inst['InstanceId']
                tags = {t['Key']: t['Value'] for t in inst.get('Tags', [])}

                instances.append({
                    "InstanceId": instance_id,
                    "Name": tags.get("Name", "N/A"),
                    "Tags": tags
                })

    return instances


# -------------------------------
# Filter instances by tag
# -------------------------------
def filter_by_tags(instances, tag_conditions):
    matched = []

    for inst in instances:
        match = True
        for tag in tag_conditions:
            key = tag['ConditionKey']
            val = tag['ConditionValue']

            if inst['Tags'].get(key) != val:
                match = False
                break

        if match:
            matched.append(inst)

    return matched


# -------------------------------
# Resolve EC2 instances for selection
# -------------------------------
def resolve_ec2_instances(selection, all_instances):
    resources = selection.get('Resources', [])
    tags = selection.get('ListOfTags', [])

    # Case 1: instance/* + TAG → FILTER
    if any("instance/*" in r for r in resources) and tags:
        return filter_by_tags(all_instances, tags), "EC2 + TAG FILTER"

    # Case 2: instance/* only → ALL EC2
    elif any("instance/*" in r for r in resources):
        return all_instances, "ALL EC2 (No Tag Filter)"

    # Case 3: TAG only → filter
    elif tags:
        return filter_by_tags(all_instances, tags), "TAG-BASED"

    # Case 4: explicit ARNs
    else:
        matched = []
        for r in resources:
            if ":instance/" in r:
                iid = r.split("/")[-1]
                for inst in all_instances:
                    if inst["InstanceId"] == iid:
                        matched.append(inst)
        return matched, "EXPLICIT"


# -------------------------------
# Main
# -------------------------------
def main():
    print("\n=== BACKUP CONFIGURATION AUDIT (READ-ONLY) ===\n")

    all_instances = get_all_ec2_instances()
    print(f"Total EC2 Instances discovered: {len(all_instances)}\n")

    plans = backup.list_backup_plans()['BackupPlansList']

    for plan in plans:
        plan_id = plan['BackupPlanId']
        plan_name = plan['BackupPlanName']

        print(f"\n🔹 Plan: {plan_name}")

        selections = backup.list_backup_selections(
            BackupPlanId=plan_id
        )['BackupSelectionsList']

        for sel in selections:
            sel_id = sel['SelectionId']
            sel_name = sel['SelectionName']

            sel_details = backup.get_backup_selection(
                BackupPlanId=plan_id,
                SelectionId=sel_id
            )

            selection = sel_details['BackupSelection']

            print(f"\n   ➤ Selection: {sel_name}")

            # Resolve instances
            matched_instances, sel_type = resolve_ec2_instances(selection, all_instances)

            print(f"     Type: {sel_type}")

            # Print tag conditions if present
            if selection.get('ListOfTags'):
                for tag in selection['ListOfTags']:
                    print(f"       Tag: {tag['ConditionKey']} = {tag['ConditionValue']}")

            # Output instances
            print(f"     Instances ({len(matched_instances)}):")

            if matched_instances:
                for inst in matched_instances:
                    print(f"       - {inst['InstanceId']} | {inst['Name']}")
            else:
                print("       - None")

        print("\n" + "-" * 60)


if __name__ == "__main__":
    main()
