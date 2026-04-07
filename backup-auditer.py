import boto3

backup = boto3.client('backup')
ec2 = boto3.client('ec2')


# -------------------------------
# Step 1: Get EC2 + group by tag
# -------------------------------
def get_ec2_by_backup_tag():
    tag_map = {
        "ec2-production": [],
        "ec2-production-weekly": []
    }

    all_instances = []

    paginator = ec2.get_paginator('describe_instances')

    for page in paginator.paginate():
        for res in page['Reservations']:
            for inst in res['Instances']:
                iid = inst['InstanceId']
                tags = {t['Key']: t['Value'] for t in inst.get('Tags', [])}
                name = tags.get("Name", "N/A")
                backup_tag = tags.get("backup")

                instance_obj = {
                    "InstanceId": iid,
                    "Name": name,
                    "BackupTag": backup_tag
                }

                all_instances.append(instance_obj)

                # Group by backup tag
                if backup_tag in tag_map:
                    tag_map[backup_tag].append(instance_obj)

    return tag_map, all_instances


# -------------------------------
# Step 2: Extract tag from selection
# -------------------------------
def extract_selection_tag(selection):
    # Check ListOfTags
    if selection.get('ListOfTags'):
        for tag in selection['ListOfTags']:
            if tag['ConditionKey'] == 'backup':
                return tag['ConditionValue']

    # Check Conditions
    conditions = selection.get('Conditions', {})
    for cond_type in ['StringEquals', 'StringLike']:
        for cond in conditions.get(cond_type, []):
            if cond['ConditionKey'] == 'backup':
                return cond['ConditionValue']

    return None


# -------------------------------
# Main
# -------------------------------
def main():
    print("\n=== BACKUP AUDIT (TAG-BASED APPROACH) ===\n")

    tag_map, all_instances = get_ec2_by_backup_tag()

    print(f"Total EC2 Instances: {len(all_instances)}\n")

    print("=== INSTANCE GROUPING (SOURCE OF TRUTH) ===")

    for tag, instances in tag_map.items():
        print(f"\nTag: backup = {tag}")
        print(f"Instances ({len(instances)}):")

        for inst in instances:
            print(f"  - {inst['InstanceId']} | {inst['Name']}")

    print("\n" + "="*60)

    # -------------------------------
    # Step 2: Backup Config
    # -------------------------------
    plans = backup.list_backup_plans()['BackupPlansList']

    print("\n=== BACKUP CONFIGURATION MAPPING ===\n")

    for plan in plans:
        plan_name = plan['BackupPlanName']
        print(f"\n🔹 Plan: {plan_name}")

        selections = backup.list_backup_selections(
            BackupPlanId=plan['BackupPlanId']
        )['BackupSelectionsList']

        for sel in selections:
            sel_name = sel['SelectionName']

            sel_details = backup.get_backup_selection(
                BackupPlanId=plan['BackupPlanId'],
                SelectionId=sel['SelectionId']
            )

            selection = sel_details['BackupSelection']

            backup_tag = extract_selection_tag(selection)

            print(f"\n   ➤ Selection: {sel_name}")

            if backup_tag:
                print(f"     Tag Mapping: backup = {backup_tag}")

                matched_instances = tag_map.get(backup_tag, [])

                print(f"     Instances ({len(matched_instances)}):")

                for inst in matched_instances:
                    print(f"       - {inst['InstanceId']} | {inst['Name']}")

            else:
                print("     No backup tag found (manual or broad assignment)")

        print("\n" + "-"*60)


if __name__ == "__main__":
    main()
