#!/usr/bin/env python3

"""
FSx Volume Storage Verification Script
--------------------------------------

Purpose:
- Fetch ONE FSx volume details using Volume ID
- Show:
    - Volume Name
    - Provisioned Size
    - Free Storage
    - Used Storage
    - Utilization %

Safe Usage:
- Completely readonly
- No update/modify/delete operations
- Safe for AWS CloudShell

Required IAM Permissions:
- fsx:DescribeVolumes
- cloudwatch:GetMetricStatistics
"""

import boto3
from datetime import datetime, timedelta

# ============================================================
# INPUT
# ============================================================

volume_id = input(
    "Enter FSx Volume ID: "
).strip()

# ============================================================
# AWS CLIENTS
# ============================================================

fsx = boto3.client("fsx")
cloudwatch = boto3.client("cloudwatch")

# ============================================================
# FETCH VOLUME DETAILS
# ============================================================

response = fsx.describe_volumes(
    VolumeIds=[volume_id]
)

volume = response["Volumes"][0]

volume_name = volume.get(
    "Name",
    "Unknown"
)

# ------------------------------------------------------------
# FETCH SIZE SAFELY
# ------------------------------------------------------------

volume_size_gib = 0

ontap = volume.get("OntapConfiguration")

if ontap:

    size_mb = ontap.get(
        "SizeInMegabytes",
        0
    )

    volume_size_gib = round(
        size_mb / 1024,
        2
    )

# ============================================================
# FETCH CLOUDWATCH METRICS
# ============================================================

end_time = datetime.utcnow()
start_time = end_time - timedelta(hours=1)

metric_response = cloudwatch.get_metric_statistics(
    Namespace="AWS/FSx",
    MetricName="VolumeAvailableStorageCapacity",
    Dimensions=[
        {
            "Name": "VolumeId",
            "Value": volume_id
        }
    ],
    StartTime=start_time,
    EndTime=end_time,
    Period=300,
    Statistics=["Average"]
)

datapoints = metric_response.get(
    "Datapoints",
    []
)

free_storage_gib = 0

if datapoints:

    latest_datapoint = sorted(
        datapoints,
        key=lambda x: x["Timestamp"],
        reverse=True
    )[0]

    # CloudWatch metric returns bytes
    free_storage_bytes = latest_datapoint["Average"]

    free_storage_gib = round(
        free_storage_bytes / (1024 ** 3),
        2
    )

# ============================================================
# CALCULATIONS
# ============================================================

used_storage_gib = round(
    volume_size_gib - free_storage_gib,
    2
)

utilization_percent = 0

if volume_size_gib > 0:

    utilization_percent = round(
        (used_storage_gib / volume_size_gib) * 100,
        2
    )

# ============================================================
# OUTPUT
# ============================================================

print("\n" + "=" * 80)
print(f"Volume ID   : {volume_id}")
print(f"Volume Name : {volume_name}")
print("=" * 80)

print("\nVolume Storage")
print("-" * 40)

print(f"Provisioned Size : {volume_size_gib:,.2f} GiB")
print(f"Used Storage     : {used_storage_gib:,.2f} GiB")
print(f"Free Storage     : {free_storage_gib:,.2f} GiB")
print(f"Utilization      : {utilization_percent}%")

print("\nReadonly verification completed successfully.")
print("=" * 80)
