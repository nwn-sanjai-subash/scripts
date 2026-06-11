#!/usr/bin/env python3

import sys
import boto3
from botocore.exceptions import ClientError


def print_header(title):
    print("\n" + "=" * 80)
    print(title)
    print("=" * 80)


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <maintenance-window-id>")
        sys.exit(1)

    mw_id = sys.argv[1]

    ssm = boto3.client("ssm")

    try:
        # ------------------------------------------------------------------
        # Maintenance Window Details
        # ------------------------------------------------------------------
        mw = ssm.get_maintenance_window(WindowId=mw_id)

        print_header("MAINTENANCE WINDOW DETAILS")

        print(f"Window ID                 : {mw['WindowId']}")
        print(f"Name                      : {mw['Name']}")
        print(f"Enabled                   : {mw['Enabled']}")
        print(f"Schedule                  : {mw['Schedule']}")
        print(f"Schedule Timezone         : {mw.get('ScheduleTimezone', 'N/A')}")
        print(f"Duration (hours)          : {mw['Duration']}")
        print(f"Cutoff (hours)            : {mw['Cutoff']}")
        print(f"Allow Unassociated Target : {mw['AllowUnassociatedTargets']}")

        # ------------------------------------------------------------------
        # Targets
        # ------------------------------------------------------------------
        print_header("REGISTERED TARGETS")

        targets = ssm.describe_maintenance_window_targets(
            WindowId=mw_id
        )["Targets"]

        for target in targets:
            print(f"\nTarget ID      : {target['WindowTargetId']}")
            print(f"Resource Type  : {target['ResourceType']}")
            print(f"Owner Info     : {target.get('OwnerInformation', 'N/A')}")

            for t in target.get("Targets", []):
                print(f"Target Key     : {t['Key']}")
                print(
                    f"Target Values  : "
                    f"{', '.join(t['Values'])}"
                )

        # ------------------------------------------------------------------
        # Tasks
        # ------------------------------------------------------------------
        print_header("REGISTERED TASKS")

        tasks = ssm.describe_maintenance_window_tasks(
            WindowId=mw_id
        )["Tasks"]

        tasks = sorted(tasks, key=lambda x: x["Priority"])

        for task in tasks:
            print(f"\nTask Name      : {task['Name']}")
            print(f"Task ID        : {task['WindowTaskId']}")
            print(f"Priority       : {task['Priority']}")
            print(f"Task Type      : {task['Type']}")
            print(f"Task ARN       : {task['TaskArn']}")
            print(f"Service Role   : {task.get('ServiceRoleArn', 'N/A')}")
            print(f"Concurrency    : {task['MaxConcurrency']}")
            print(f"Max Errors     : {task['MaxErrors']}")

            details = ssm.get_maintenance_window_task(
                WindowId=mw_id,
                WindowTaskId=task["WindowTaskId"]
            )

            params = details.get("TaskInvocationParameters", {})

            if task["Type"] == "RUN_COMMAND":
                print("\nRun Command Parameters:")

                rc = params.get(
                    "MaintenanceWindowRunCommandParameters",
                    {}
                )

                if "Parameters" in rc:
                    for key, value in rc["Parameters"].items():
                        print(f"  {key}: {value}")

            elif task["Type"] == "AUTOMATION":
                print("\nAutomation Parameters:")

                auto = params.get(
                    "MaintenanceWindowAutomationParameters",
                    {}
                )

                print(
                    f"  Document Version : "
                    f"{auto.get('DocumentVersion', 'N/A')}"
                )

                print(
                    f"  Mode             : "
                    f"{auto.get('Mode', 'N/A')}"
                )

                if "Parameters" in auto:
                    print("  Parameters:")

                    for key, value in auto[
                        "Parameters"
                    ].items():

                        print(
                            f"    {key}: {value}"
                        )

        # ------------------------------------------------------------------
        # Existing Automation Documents
        # ------------------------------------------------------------------
        print_header("EXISTING AUTOMATION DOCUMENTS")

        paginator = ssm.get_paginator("list_documents")

        count = 0

        for page in paginator.paginate(
            Filters=[
                {
                    "Key": "DocumentType",
                    "Value": "Automation"
                }
            ]
        ):

            for doc in page["DocumentIdentifiers"]:

                count += 1

                print(
                    f"{doc['Name']}"
                    f" (Owner: {doc['Owner']})"
                )

        print(f"\nTotal Automation Documents: {count}")

    except ClientError as e:
        print(f"\nAWS Error:")
        print(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
