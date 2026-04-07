import boto3
import json

backup = boto3.client('backup')

def main():
    plans = backup.list_backup_plans()['BackupPlansList']

    print("\n=== BACKUP PLANS OVERVIEW ===\n")

    for plan in plans:
        plan_id = plan['BackupPlanId']
        plan_name = plan['BackupPlanName']

        print(f"\n🔹 Backup Plan: {plan_name}")
        print(f"   Plan ID: {plan_id}")

        # Get full plan details
        plan_details = backup.get_backup_plan(BackupPlanId=plan_id)

        print("\n   📅 Rules:")
        for rule in plan_details['BackupPlan']['Rules']:
            print(f"     - Rule Name: {rule['RuleName']}")
            print(f"       Schedule: {rule.get('ScheduleExpression', 'N/A')}")
            print(f"       Target Vault: {rule.get('TargetBackupVaultName', 'N/A')}")

        # Get selections (resource assignments)
        selections = backup.list_backup_selections(
            BackupPlanId=plan_id
        )['BackupSelectionsList']

        print("\n   📦 Selections:")

        for sel in selections:
            sel_id = sel['SelectionId']
            sel_name = sel['SelectionName']

            print(f"\n     ➤ Selection Name: {sel_name}")
            print(f"       Selection ID: {sel_id}")

            sel_details = backup.get_backup_selection(
                BackupPlanId=plan_id,
                SelectionId=sel_id
            )

            selection = sel_details['BackupSelection']

            print(f"       IAM Role: {selection.get('IamRoleArn', 'N/A')}")

            # Check assignment type
            resources = selection.get('Resources', [])
            tags = selection.get('ListOfTags', [])

            if resources:
                print("       Type: MANUAL (Explicit Resources)")
                print(f"       Resource Count: {len(resources)}")

                # Show first few resources only (avoid clutter)
                print("       Sample Resources:")
                for r in resources[:3]:
                    print(f"         - {r}")

            elif tags:
                print("       Type: TAG-BASED")
                print("       Tag Conditions:")

                for tag in tags:
                    print(f"         - {tag}")

            else:
                print("       Type: UNKNOWN / EMPTY")

        print("\n" + "-"*60)


if __name__ == "__main__":
    main()
