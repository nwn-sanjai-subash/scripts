#!/usr/bin/env python3

"""
FSx for ONTAP - Readonly Storage Utilization Report

Purpose
-------
Fetch:
- Filesystem SSD storage details
- All volume details including ROOT volumes
- Volume utilization
- Free space
- Additional storage required to reduce utilization to 85%

Safe Usage
----------
- Readonly only
- No modification APIs used
- Intended for AWS CloudShell

Requirements
------------
- boto3
- AWS credentials configured

Usage
-----
python3 fsx_ontap_storage_report.py
"""

import boto3
from botocore.exceptions import ClientError

# ============================================================
# CONFIGURATION
# ============================================================

AWS_REGION = "us-east-1"

# Target utilization threshold
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


def print_separator():
    print("=" * 90)


# ============================================================
# AWS CLIENTS
# ============================================================

fsx = boto3.client("fsx", region_name=AWS_REGION)
cw = boto3.client("cloudwatch", region_name=AWS_REGION)

# ============================================================
# MAIN
# ============================================================

try:

    filesystems = fsx.describe_file_systems()["FileSystems"]

    for filesystem in filesystems:

        if filesystem["FileSystemType"] != "ONTAP":
            continue

        fs_id = filesystem["FileSystemId"]

        print_separator()
        print(f"FSx FileSystem ID : {fs_id}")
        print_separator()

        # ====================================================
        # FILESYSTEM STORAGE
        # ====================================================

        total_storage_gib = filesystem["StorageCapacity"]

        # ----------------------------------------------------
        # Fetch CloudWatch metrics
        # ----------------------------------------------------

        metrics = cw.get_metric_data(
            MetricDataQueries=[
                {
                    "Id": "usedstorage",
                    "MetricStat": {
                        "Metric": {
                            "Namespace": "AWS/FSx",
                            "MetricName": "UsedStorageCapacity",
                            "Dimensions": [
                                {
                                    "Name": "FileSystemId",
                                    "Value": fs_id
                                }
                            ]
                        },
                        "Period": 300,
                        "Stat": "Average"
                    },
                    "ReturnData": True
                }
            ],
            StartTime=datetime.utcnow() - timedelta(minutes=30),
            EndTime=datetime.utcnow()
        )

        used_storage_gib = 0

        try:
            values = metrics["MetricDataResults"][0]["Values"]

            if values:
                used_storage_gib = round(values[0], 2)

        except Exception:
            pass

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

        # ====================================================
        # VOLUMES
        # ====================================================

        print("\n")
        print_separator()
        print("VOLUME DETAILS")
        print_separator()

        volume_paginator = fsx.get_paginator("describe_volumes")

        for page in volume_paginator.paginate():

            for volume in page["Volumes"]:

                if volume["FileSystemId"] != fs_id:
                    continue

                volume_name = volume["Name"]
                volume_id = volume["VolumeId"]

                ontap_config = volume["OntapConfiguration"]

                volume_size_gib = round(
                    ontap_config["SizeInMegabytes"] / 1024,
                    2
                )

                volume_type = ontap_config.get(
                    "OntapVolumeType",
                    "RW"
                )

                junction_path = ontap_config.get(
                    "JunctionPath",
                    "N/A"
                )

                # ------------------------------------------------
                # CloudWatch volume metrics
                # ------------------------------------------------

                volume_metrics = cw.get_metric_data(
                    MetricDataQueries=[
                        {
                            "Id": "freecapacity",
                            "MetricStat": {
                                "Metric": {
                                    "Namespace": "AWS/FSx",
                                    "MetricName": "VolumeAvailableStorageCapacity",
                                    "Dimensions": [
                                        {
                                            "Name": "VolumeId",
                                            "Value": volume_id
                                        }
                                    ]
                                },
                                "Period": 300,
                                "Stat": "Average"
                            },
                            "ReturnData": True
                        }
                    ],
                    StartTime=datetime.utcnow() - timedelta(minutes=30),
                    EndTime=datetime.utcnow()
                )

                free_values = (
                    volume_metrics["MetricDataResults"][0]["Values"]
                )

                volume_free_gib = 0

                if free_values:
                    volume_free_gib = round(free_values[0], 2)

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

                print("\n")
                print_separator()

                print(f"Volume Name : {volume_name}")
                print(f"Volume Type : {volume_type}")

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

        print("\n")
        print_separator()

except ClientError as e:
    print(f"AWS API Error: {e}")

except Exception as e:
    print(f"Unexpected Error: {e}")
