#!/usr/bin/env python3

import boto3
import csv
from botocore.exceptions import ClientError

# Fixed region as requested
REGION = "us-east-1"

# Initialize AWS clients
ec2 = boto3.client("ec2", region_name=REGION)
sts = boto3.client("sts", region_name=REGION)

try:
    # Get AWS Account ID (Read-only)
    account_id = sts.get_caller_identity()["Account"]

    # Get EBS Encryption by Default status (Read-only)
    ebs_encryption_default = ec2.get_ebs_encryption_by_default()[
        "EbsEncryptionByDefault"
    ]

    # Get Default KMS Key ID for EBS encryption (Read-only)
    try:
        default_kms_key_id = ec2.get_ebs_default_kms_key_id()["KmsKeyId"]
    except ClientError:
        default_kms_key_id = ""

    # Fetch all EBS volumes (Read-only)
    volumes = ec2.describe_volumes()["Volumes"]

    # Output CSV file
    output_file = f"ebs_encryption_report_{account_id}.csv"

    rows = []

    for volume in volumes:

        volume_id = volume.get("VolumeId", "")
        encrypted = volume.get("Encrypted", False)
        kms_key_id = volume.get("KmsKeyId", "")

        # Fetch Name tag if available
        name = ""

        for tag in volume.get("Tags", []):
            if tag.get("Key") == "Name":
                name = tag.get("Value", "")
                break

        rows.append({
            "AccountId": account_id,
            "EbsEncryptionByDefault": ebs_encryption_default,
            "DefaultKmsKeyId": default_kms_key_id,
            "VolumeId": volume_id,
            "Name": name,
            "Encrypted": encrypted,
            "KmsKeyId": kms_key_id
        })

    # Write CSV output
    with open(output_file, "w", newline="") as csvfile:

        fieldnames = [
            "AccountId",
            "EbsEncryptionByDefault",
            "DefaultKmsKeyId",
            "VolumeId",
            "Name",
            "Encrypted",
            "KmsKeyId"
        ]

        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)

        writer.writeheader()

        for row in rows:
            writer.writerow(row)

    print("\n========================================")
    print("EBS Encryption Audit Report Generated")
    print("========================================")
    print(f"Account ID                 : {account_id}")
    print(f"Region                     : {REGION}")
    print(f"EBS Encryption by Default  : {ebs_encryption_default}")
    print(f"Default KMS Key ID         : {default_kms_key_id}")
    print(f"Total Volumes Found        : {len(rows)}")
    print(f"CSV Output File            : {output_file}")
    print("========================================\n")

except ClientError as error:
    print(f"AWS API Error: {error}")

except Exception as error:
    print(f"Unexpected Error: {error}")
