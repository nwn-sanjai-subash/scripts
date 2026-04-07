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
            matched.append(inst['InstanceId'])

    return matched


# -------------------------------
# Main
# -------------------------------
def main():
    print("\n=== EC2 BACKUP FULL AUDIT (READ-ONLY) ===\n")

    all_instances = get_all_ec2_instances()
    all_instance_ids = [i['InstanceId'] for i in all_instances]

    print(f"Total EC2 Instances discovered: {len(all_instances)}\n")

    # Mapping: instance_id → details + assignments
    instance_mapping = {}

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

            matched_instances = []

            resources = selection.get('Resources', [])
            tags = selection.get('ListOfTags', [])

            # Case 1: wildcard EC2
            if any("instance/*" in r for r in resources):
                print("     Type: ALL EC2 (Wildcard)")
                matched_instances = all_instance_ids

            # Case 2: tag-based
            elif tags:
                print("     Type: TAG-BASED")
                matched_instances = filter_by_tags(all_instances, tags)

            # Case 3: explicit ARNs
            else:
                print("     Type: EXPLICIT")
                for r in resources:
                    if ":instance/" in r:
                        instance_id = r.split("/")[-1]
                        matched_instances.append(instance_id)

            print(f"     Instances Found: {len(matched_instances)}")

            # Map instances → assignments
            for inst in all_instances:
                iid = inst['InstanceId']

                if iid in matched_instances:
                    if iid not in instance_mapping:
                        instance_mapping[iid] = {
                            "Name": inst["Name"],
                            "BackupTag": inst["Tags"].get("backup", "N/A"),
                            "Assignments": []
                        }

                    instance_mapping[iid]["Assignments"].append(
                        f"{plan_name} -> {sel_name}"
                    )

    # -------------------------------
    # OVERLAP ANALYSIS
    # -------------------------------
    print("\n=== OVERLAP ANALYSIS (IMPORTANT) ===\n")

    overlap_found = False

    for iid, data in instance_mapping.items():
        if len(data["Assignments"]) > 1:
            overlap_found = True
            print(f"⚠ Instance: {iid}")
            print(f"   Name: {data['Name']}")
            print(f"   Backup Tag: {data['BackupTag']}")
            print("   Present in:")

            for a in data["Assignments"]:
                print(f"     - {a}")
            print("")

    if not overlap_found:
        print("No overlapping instances found.\n")

    # -------------------------------
    # SUMMARY
    # -------------------------------
    print("\n=== SUMMARY ===")
    print(f"Total EC2 Instances: {len(all_instances)}")
    print(f"Instances with backup coverage: {len(instance_mapping)}")

    uncovered = set(all_instance_ids) - set(instance_mapping.keys())

    print(f"Instances NOT covered by backup: {len(uncovered)}")

    if uncovered:
        print("\nUncovered Instances:")
        for iid in uncovered:
            print(f" - {iid}")


if __name__ == "__main__":
    main()
