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
# Extract tag conditions (FIX)
# -------------------------------
def extract_tag_conditions(selection):
    tags = []

    # Case 1: ListOfTags
    if selection.get('ListOfTags'):
        tags.extend(selection['ListOfTags'])

    # Case 2: Conditions (console-created)
    conditions = selection.get('Conditions', {})
    for cond_type in ['StringEquals', 'StringLike']:
        for cond in conditions.get(cond_type, []):
            tags.append({
                "ConditionKey": cond['ConditionKey'],
                "ConditionValue": cond['ConditionValue']
            })

    return tags


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
# Resolve EC2 instances
# -------------------------------
def resolve_ec2_instances(selection, all_instances):
    resources = selection.get('Resources', [])
    tag_conditions = extract_tag_conditions(selection)

    has_ec2 = any("instance" in r for r in resources)

    # Case 1: EC2 + TAG → FILTER
    if has_ec2 and tag_conditions:
        return filter_by_tags(all_instances, tag_conditions), "EC2 + TAG FILTER", tag_conditions

    # Case 2: EC2 only → ALL
    elif has_ec2:
        return all_instances, "ALL EC2 (No Tag Filter)", []

    # Case 3: TAG only
    elif tag_conditions:
        return filter_by_tags(all_instances, tag_conditions), "TAG-BASED", tag_conditions

    # Case 4: explicit
    else:
        matched = []
        for r in resources:
            if ":instance/" in r:
                iid = r.split("/")[-1]
                for inst in all_instances:
                    if inst["InstanceId"] == iid:
                        matched.append(inst)
        return matched, "EXPLICIT", []


# -------------------------------
# Main
# -------------------------------
def main():
    print("\n=== BACKUP CONFIGURATION AUDIT (FINAL CORRECTED) ===\n")

    all_instances = get_all_ec2_instances()
    print(f"Total EC2 Instances: {len(all_instances)}\n")

    plans = backup.list_backup_plans()['BackupPlansList']

    for plan in plans:
        print(f"\n🔹 Plan: {plan['BackupPlanName']}")

        selections = backup.list_backup_selections(
            BackupPlanId=plan['BackupPlanId']
        )['BackupSelectionsList']

        for sel in selections:
            print(f"\n   ➤ Selection: {sel['SelectionName']}")

            sel_details = backup.get_backup_selection(
                BackupPlanId=plan['BackupPlanId'],
                SelectionId=sel['SelectionId']
            )

            selection = sel_details['BackupSelection']

            matched_instances, sel_type, tag_conditions = resolve_ec2_instances(
                selection, all_instances
            )

            print(f"     Type: {sel_type}")

            if tag_conditions:
                for tag in tag_conditions:
                    print(f"       Tag: {tag['ConditionKey']} = {tag['ConditionValue']}")

            print(f"     Instances ({len(matched_instances)}):")

            for inst in matched_instances:
                print(f"       - {inst['InstanceId']} | {inst['Name']}")

        print("\n" + "-" * 60)


if __name__ == "__main__":
    main()
