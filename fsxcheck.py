#!/usr/bin/env python3

"""
FSx ONTAP Readonly Storage Verification Script

Purpose
-------
Fetch accurate:
- Filesystem SSD storage usage
- Volume quota size
- Volume used/free storage
- Current utilization %
- Additional storage required to reduce utilization to 85%

Safe Usage
----------
- Completely readonly
- No modification APIs used
- No SSH/ONTAP write operations
- Safe for AWS CloudShell

Required IAM Permissions
------------------------
- fsx:DescribeFileSystems
- fsx:DescribeVolumes
- cloudwatch:GetMetricData
"""

import boto3
from datetime import datetime, timedelta

TARGET_UTILIZATION = 85

# ============================================================
# INPUTS
# ============================================================

filesystem_id = input("Enter FSx FileSystem ID: ").strip()

volume_ids_input = input(
    "Enter Volume IDs separated by comma: "
).strip()

volume_ids = [
    x.strip()
    for x in volume_ids_input.split(",")
    if x.strip()
]

# ============================================================
# HELPERS
# ============================================================

def gib_to_tb(value):
    return round(value / 1024, 2)


def calculate_utilization(used, total):

    if total <= 0:
        return 0

    return round((used / total) * 100, 2)


def required_size_for_target(used, target):

    return round(used / (target / 100), 2)


def get_metric(
    cw,
    metric_name,
    dimensions
):

    end_time = datetime.utcnow()
    start_time = end_time - timedelta(hours=1)

    response = cw.get_metric_data(
        MetricDataQueries=[
            {
                "Id": "metricquery",
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/FSx",
                        "MetricName": metric_name,
                        "Dimensions": dimensions
                    },
                    "Period": 300,
                    "Stat": "Average"
                },
                "ReturnData": True
            }
        ],
        StartTime=start_time,
        EndTime=end_time
    )

    values = response["MetricDataResults"][0]["Values"]

    if not values:
        return None

    return round(values[0], 2)

# ============================================================
# AWS CLIENTS
# ============================================================

fsx = boto3.client("fsx")
cw = boto3.client("cloudwatch")

# ============================================================
# FILESYSTEM DETAILS
# ============================================================

fs_response = fsx.describe_file_systems(
    FileSystemIds=[filesystem_id]
)

filesystem = fs_response["FileSystems"][0]

total_ssd_gib = filesystem["StorageCapacity"]

used_ssd_gib = get_metric(
    cw,
    "UsedStorageCapacity",
    [
        {
            "Name": "FileSystemId",
            "Value": filesystem_id
        }
    ]
)

if used_ssd_gib is None:
    used_ssd_gib = 0

free_ssd_gib = round(
    total_ssd_gib - used_ssd_gib,
    2
)

filesystem_utilization = calculate_utilization(
    used_ssd_gib,
    total_ssd_gib
)

required_fs_size = required_size_for_target(
    used_ssd_gib,
    TARGET_UTILIZATION
)

additional_fs_needed = round(
    required_fs_size - total_ssd_gib,
    2
)

# ============================================================
# OUTPUT - FILESYSTEM
# ============================================================

print("\n" + "=" * 100)
print(f"FSx FileSystem ID : {filesystem_id}")
print("=" * 100)

print("\nFilesystem Storage")
print("-" * 50)

print(f"Total SSD Storage : {total_ssd_gib:,.2f} GiB ({gib_to_tb(total_ssd_gib)} TB)")
print(f"Used SSD Storage  : {used_ssd_gib:,.2f} GiB ({gib_to_tb(used_ssd_gib)} TB)")
print(f"Free SSD Storage  : {free_ssd_gib:,.2f} GiB ({gib_to_tb(free_ssd_gib)} TB)")
print(f"Utilization       : {filesystem_utilization}%")

print("\nRequired Capacity For 85% Utilization")
print("-" * 50)

if filesystem_utilization > TARGET_UTILIZATION:

    print(f"Recommended Total Size   : {required_fs_size:,.2f} GiB ({gib_to_tb(required_fs_size)} TB)")
    print(f"Additional Storage Needed: {additional_fs_needed:,.2f} GiB ({gib_to_tb(additional_fs_needed)} TB)")

else:
    print("No increase required")

# ============================================================
# VOLUME DETAILS
# ============================================================

print("\n" + "=" * 100)
print("VOLUME DETAILS")
print("=" * 100)

volume_response = fsx.describe_volumes(
    VolumeIds=volume_ids
)

for volume in volume_response["Volumes"]:

    volume_id = volume["VolumeId"]
    volume_name = volume["Name"]

    ontap = volume["OntapConfiguration"]

    volume_size_gib = round(
        ontap["SizeInMegabytes"] / 1024,
        2
    )

    volume_type = ontap.get(
        "OntapVolumeType",
        "RW"
    )

    # --------------------------------------------------------
    # CloudWatch Metric
    # --------------------------------------------------------

    available_storage_gib = get_metric(
        cw,
        "VolumeAvailableStorageCapacity",
        [
            {
                "Name": "VolumeId",
                "Value": volume_id
            }
        ]
    )

    if available_storage_gib is None:
        available_storage_gib = 0

    used_storage_gib = round(
        volume_size_gib - available_storage_gib,
        2
    )

    utilization = calculate_utilization(
        used_storage_gib,
        volume_size_gib
    )

    required_volume_size = required_size_for_target(
        used_storage_gib,
        TARGET_UTILIZATION
    )

    additional_needed = round(
        required_volume_size - volume_size_gib,
        2
    )

    # --------------------------------------------------------
    # OUTPUT
    # --------------------------------------------------------

    print("\n" + "-" * 100)

    print(f"Volume Name : {volume_name}")
    print(f"Volume ID   : {volume_id}")
    print(f"Volume Type : {volume_type}")

    print("\nVolume Storage")
    print("-" * 50)

    print(f"Provisioned Size : {volume_size_gib:,.2f} GiB ({gib_to_tb(volume_size_gib)} TB)")
    print(f"Used Storage     : {used_storage_gib:,.2f} GiB ({gib_to_tb(used_storage_gib)} TB)")
    print(f"Free Storage     : {available_storage_gib:,.2f} GiB ({gib_to_tb(available_storage_gib)} TB)")
    print(f"Utilization      : {utilization}%")

    print("\nRequired Capacity For 85% Utilization")
    print("-" * 50)

    if utilization > TARGET_UTILIZATION:

        print(f"Recommended Volume Size : {required_volume_size:,.2f} GiB ({gib_to_tb(required_volume_size)} TB)")
        print(f"Additional Storage Needed : {additional_needed:,.2f} GiB ({gib_to_tb(additional_needed)} TB)")

    else:
        print("No increase required")

    print("\nAlert Status")
    print("-" * 50)

    if utilization >= 90:
        print("CRITICAL (>90%)")

    elif utilization >= 85:
        print("WARNING (>85%)")

    else:
        print("OK")

print("\n" + "=" * 100)
print("Readonly verification completed successfully.")
print("=" * 100)
