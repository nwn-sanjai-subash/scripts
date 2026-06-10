#!/usr/bin/env python3

import argparse
import boto3
from botocore.exceptions import ClientError

ec2 = boto3.client("ec2")
ssm = boto3.client("ssm")


def read_instance_names(file_path):
    with open(file_path, "r") as f:
        return [line.strip() for line in f if line.strip()]


def get_instance_details(instance_name):
    response = ec2.describe_instances(
        Filters=[
            {"Name": "tag:Name", "Values": [instance_name]},
            {
                "Name": "instance-state-name",
                "Values": [
                    "pending",
                    "running",
                    "stopping",
                    "stopped",
                    "shutting-down",
                    "terminated",
                ],
            },
        ]
    )

    instances = []

    for reservation in response["Reservations"]:
        for instance in reservation["Instances"]:
            instances.append(
                {
                    "InstanceId": instance["InstanceId"],
                    "State": instance["State"]["Name"],
                }
            )

    return instances


def get_ssm_managed_instances():
    managed = set()

    paginator = ssm.get_paginator("describe_instance_information")

    for page in paginator.paginate():
        for inst in page["InstanceInformationList"]:
            managed.add(inst["InstanceId"])

    return managed


def get_latest_execution(mw_id):
    response = ssm.describe_maintenance_window_executions(
        WindowId=mw_id,
        MaxResults=10,
    )

    executions = response.get("WindowExecutions", [])

    if not executions:
        return None

    executions.sort(key=lambda x: x["StartTime"], reverse=True)

    return executions[0]


def get_task_details(window_execution_id):
    response = ssm.describe_maintenance_window_execution_tasks(
        WindowExecutionId=window_execution_id
    )

    return response.get("WindowExecutionTaskIdentities", [])


def get_task_invocations(window_execution_id, task_execution_id):
    response = ssm.describe_maintenance_window_execution_task_invocations(
        WindowExecutionId=window_execution_id,
        TaskId=task_execution_id,
    )

    return response.get("WindowExecutionTaskInvocationIdentities", [])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mw-id", required=True)
    parser.add_argument("--instance-file", required=True)

    args = parser.parse_args()

    print("\n=== Resolving Instance Names ===\n")

    names = read_instance_names(args.instance_file)

    managed_instances = get_ssm_managed_instances()

    for name in names:
        instances = get_instance_details(name)

        if not instances:
            print(f"[ERROR] {name}: No EC2 instance found")
            continue

        if len(instances) > 1:
            print(f"[WARNING] {name}: Multiple instances found")

        for inst in instances:
            managed = (
                "Yes"
                if inst["InstanceId"] in managed_instances
                else "No"
            )

            print(
                f"{name:<40} "
                f"{inst['InstanceId']:<20} "
                f"State={inst['State']:<12} "
                f"SSM Managed={managed}"
            )

    print("\n=== Maintenance Window Execution ===\n")

    latest = get_latest_execution(args.mw_id)

    if not latest:
        print("No executions found for this maintenance window.")
        return

    execution_id = latest["WindowExecutionId"]

    print(f"Execution ID : {execution_id}")
    print(f"Status       : {latest['Status']}")
    print(f"Start Time   : {latest['StartTime']}")

    print("\n=== Task Executions ===\n")

    tasks = get_task_details(execution_id)

    for task in tasks:
        print(f"Task ARN     : {task['TaskArn']}")
        print(f"Task ID      : {task['TaskExecutionId']}")
        print(f"Status       : {task['Status']}")
        print(f"Details      : {task.get('StatusDetails', '-')}")
        print("-" * 80)

        invocations = get_task_invocations(
            execution_id,
            task["TaskExecutionId"],
        )

        for inv in invocations:
            print(
                f"  Invocation Status : {inv['Status']}"
            )
            print(
                f"  Details           : {inv.get('StatusDetails', '-')}"
            )

            if inv.get("Parameters"):
                print(
                    f"  Parameters        : {inv['Parameters']}"
                )

            print()

        print()


if __name__ == "__main__":
    try:
        main()
    except ClientError as e:
        print(f"AWS Error: {e}")
