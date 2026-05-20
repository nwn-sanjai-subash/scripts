#!/usr/bin/env python3

"""
FSx Filesystem Storage Verification Script
------------------------------------------

Purpose:
- Fetch ONLY the main FSx filesystem storage details
- Show:
    - Total SSD storage
    - Used SSD storage
    - Free SSD storage
    - Utilization %

Safe Usage:
- Completely readonly
- No update/modify/delete operations
- Safe for AWS CloudShell

Required IAM Permissions:
- fsx:DescribeFileSystems
- cloudwatch:GetMetricStatistics
"""

import boto3
from datetime import datetime, timedelta

# ============================================================
# INPUT
# ============================================================

filesystem_id = input(
    "Enter FSx FileSystem ID: "
).strip()

# ============================================================
# AWS CLIENTS
# ============================================================

fsx = boto3.client("fsx")
cloudwatch = boto3.client("cloudwatch")

# ============================================================
# FETCH FILESYSTEM DETAILS
# ============================================================

response = fsx.describe_file_systems(
    FileSystemIds=[filesystem_id]
)

filesystem = response["FileSystems"][0]

total_storage_gib = filesystem["StorageCapacity"]

# ============================================================
# FETCH CLOUDWATCH METRICS
# ============================================================

end_time = datetime.utcnow()
start_time = end_time - timedelta(hours=1)

metric_response = cloudwatch.get_metric_statistics(
    Namespace="AWS/FSx",
    MetricName="UsedStorageCapacity",
    Dimensions=[
        {
            "Name": "FileSystemId",
            "Value": filesystem_id
        }
    ],
    StartTime=start_time,
    EndTime=end_time,
    Period=300,
    Statistics=["Average"]
)

datapoints = metric_response.get("Datapoints", [])

used_storage_gib = 0

if datapoints:

    latest_datapoint = sorted(
        datapoints,
        key=lambda x: x["Timestamp"],
        reverse=True
    )[0]

    # CloudWatch metric returns bytes
    used_storage_bytes = latest_datapoint["Average"]

    used_storage_gib = round(
        used_storage_bytes / (1024 ** 3),
        2
    )

# ============================================================
# CALCULATIONS
# ============================================================

free_storage_gib = round(
    total_storage_gib - used_storage_gib,
    2
)

utilization_percent = round(
    (used_storage_gib / total_storage_gib) * 100,
    2
)

# ============================================================
# OUTPUT
# ============================================================

print("\n" + "=" * 80)
print(f"FSx FileSystem ID : {filesystem_id}")
print("=" * 80)

print("\nFilesystem Storage")
print("-" * 40)

print(f"Total SSD Storage : {total_storage_gib:,.2f} GiB")
print(f"Used SSD Storage  : {used_storage_gib:,.2f} GiB")
print(f"Free SSD Storage  : {free_storage_gib:,.2f} GiB")
print(f"Utilization       : {utilization_percent}%")

print("\nReadonly verification completed successfully.")
print("=" * 80)
