#!/usr/bin/env python3

"""
Readonly FSx for ONTAP Storage Report

Purpose
-------
Fetch:
- Filesystem total SSD storage
- Filesystem utilization
- All volume details including ROOT volumes
- Volume utilization
- Required additional size to reduce utilization to 85%

Safe Usage
----------
- Completely readonly
- No update/modify/delete operations
- Safe for production verification

Required IAM Permissions
------------------------
- fsx:DescribeFileSystems
- fsx:DescribeVolumes
- cloudwatch:GetMetricStatistics
"""

import boto3
from datetime import datetime, timedelta

# ============================================================
# INPUT
# ============================================================

filesystem_id = input("Enter FSx FileSystem ID: ").strip()

TARGET_UTILIZATION = 85

# ============================================================
# HELPERS
# ============================================================

def gib_to_tb(value_gib):
    return round(value_gib / 1024, 2)


def calculate_utilization(used, total):
    if total == 0:
        return 0
    return round((used / total) * 100, 2)


def calculate_required_size(used_gib, target_percent):
    return round(used_gib / (target_percent / 100), 2)


def get_latest_metric(cw, namespace, metric_name, dimensions):

    end_time = datetime.utcnow()
    start_time = end_time - timedelta(hours=1)

    response = cw.get_metric_statistics(
        Namespace=namespace,
        MetricName=metric_name,
        Dimensions=dimensions,
        StartTime=start_time,
        EndTime=end_time,
        Period=300,
        Statistics=["Average"]
    )

    datapoints = response.get("Datapoints", [])

    if not datapoints:
        return None

    latest = sorted(
        datapoints,
        key=lambda x: x["Timestamp"],
        reverse=True
    )[0]

    return round(latest["Average"], 2)

# ============================================================
# AWS CLIENTS
# ============================================================

fsx = boto3.client("fsx")
cw = boto3.client("cloudwatch")

# ============================================================
# FETCH FILESYSTEM
# ============================================================

try:

    fs_response = fsx.describe_file_systems(
        FileSystemIds=[filesystem_id]
    )

    filesystem = fs_response["FileSystems"][0]

    print("\n" + "=" * 90)
    print(f"FSx FileSystem ID : {filesystem_id}")
    print("=" * 90)

    total_storage_gib = filesystem["StorageCapacity"]

    used_storage_gib = get_latest_metric(
        cw,
        "AWS/FSx",
        "UsedStorageCapacity",
        [
            {
                "Name": "FileSystemId",
                "Value": filesystem_id
            }
        ]
    )

    if used_storage_gib is None:
        used_storage_gib = 0

    free_storage_gib = round(
        total_storage_gib - used_storage_gib,
        2
    )

    filesystem_utilization = calculate_utilization(
        used_storage_gib,
        total_storage_gib
    )

    required_fs_size_gib = calculate_required_size(
        used_storage_gib,
        TARGET_UTILIZATION
    )

    additional_fs_needed_gib = round(
        required_fs_size_gib - total_storage_gib,
        2
    )

    # ========================================================
    # FILESYSTEM OUTPUT
    # ========================================================

    print("\nFilesystem Storage")
    print("-" * 40)

    print(f"Total SSD Storage : {total_storage_gib:,} GiB ({gib_to_tb(total_storage_gib)} TB)")
    print(f"Used Storage      : {used_storage_gib:,} GiB ({gib_to_tb(used_storage_gib)} TB)")
    print(f"Free Storage      : {free_storage_gib:,} GiB ({gib_to_tb(free_storage_gib)} TB)")
    print(f"Utilization       : {filesystem_utilization}%")

    print("\nRequired Capacity For 85% Utilization")
    print("-" * 40)

    if filesystem_utilization > TARGET_UTILIZATION:

        print(f"Recommended Total Size : {round(required_fs_size_gib, 2):,} GiB ({gib_to_tb(required_fs_size_gib)} TB)")
        print(f"Additional Capacity Needed : {round(additional_fs_needed_gib, 2):,} GiB ({gib_to_tb(additional_fs_needed_gib)} TB)")

    else:
        print("No increase required")

    # ========================================================
    # VOLUMES
    # ========================================================

    print("\n")
    print("=" * 90)
    print("VOLUME DETAILS")
    print("=" * 90)

    paginator = fsx.get_paginator("describe_volumes")

    for page in paginator.paginate():

        for volume in page["Volumes"]:

            if volume["FileSystemId"] != filesystem_id:
                continue

            volume_name = volume["Name"]
            volume_id = volume["VolumeId"]

            ontap = volume["OntapConfiguration"]

            volume_size_gib = round(
                ontap["SizeInMegabytes"] / 1024,
                2
            )

            volume_type = ontap.get(
                "OntapVolumeType",
                "RW"
            )

            junction_path = ontap.get(
                "JunctionPath",
                "N/A"
            )

            volume_free_gib = get_latest_metric(
                cw,
                "AWS/FSx",
                "VolumeAvailableStorageCapacity",
                [
                    {
                        "Name": "VolumeId",
                        "Value": volume_id
                    }
                ]
            )

            if volume_free_gib is None:
                volume_free_gib = 0

            volume_used_gib = round(
                volume_size_gib - volume_free_gib,
                2
            )

            volume_utilization = calculate_utilization(
                volume_used_gib,
                volume_size_gib
            )

            required_volume_size_gib = calculate_required_size(
                volume_used_gib,
                TARGET_UTILIZATION
            )

            additional_needed_gib = round(
                required_volume_size_gib - volume_size_gib,
                2
            )

            print("\n" + "=" * 90)

            print(f"Volume Name : {volume_name}")
            print(f"Volume Type : {volume_type}")
            print(f"Junction Path : {junction_path}")

            print("\nVolume Storage")
            print("-" * 40)

            print(f"Provisioned Size : {volume_size_gib:,} GiB ({gib_to_tb(volume_size_gib)} TB)")
            print(f"Used Storage     : {volume_used_gib:,} GiB ({gib_to_tb(volume_used_gib)} TB)")
            print(f"Free Storage     : {volume_free_gib:,} GiB ({gib_to_tb(volume_free_gib)} TB)")
            print(f"Utilization      : {volume_utilization}%")

            print("\nRequired Capacity For 85% Utilization")
            print("-" * 40)

            if volume_utilization > TARGET_UTILIZATION:

                print(f"Recommended Volume Size : {round(required_volume_size_gib, 2):,} GiB ({gib_to_tb(required_volume_size_gib)} TB)")

                print(f"Additional Capacity Needed : {round(additional_needed_gib, 2):,} GiB ({gib_to_tb(additional_needed_gib)} TB)")

            else:
                print("No increase required")

            print("\nAlert Status")
            print("-" * 40)

            if volume_utilization >= 90:
                print("CRITICAL (>90%)")

            elif volume_utilization >= 85:
                print("WARNING (>85%)")

            else:
                print("OK")

    print("\n" + "=" * 90)

except Exception as e:
    print(f"\nError: {e}")
