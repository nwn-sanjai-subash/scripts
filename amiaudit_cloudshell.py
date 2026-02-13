import boto3
import csv
import os
from datetime import datetime, timezone

CACHE_FILE = "ami_cache.txt"


# ---------------------------
# AWS Clients (CloudShell)
# ---------------------------

ec2 = boto3.client("ec2")


# ---------------------------
# Utility Functions
# ---------------------------

def get_amis():
    response = ec2.describe_images(Owners=['self'])
    return response['Images']


def calculate_age(creation_date):
    created = datetime.fromisoformat(creation_date.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    return (now - created).days


def get_snapshots_from_ami(ami):
    snaps = []
    for mapping in ami.get("BlockDeviceMappings", []):
        ebs = mapping.get("Ebs")
        if ebs and "SnapshotId" in ebs:
            snaps.append(ebs["SnapshotId"])
    return snaps


def get_tag_value(tags, key):
    if not tags:
        return None
    for tag in tags:
        if tag["Key"].lower() == key.lower():
            return tag["Value"]
    return None


def is_expired(expiry_date):
    try:
        expiry = datetime.strptime(expiry_date, "%Y-%m-%d").date()
        today = datetime.utcnow().date()
        return today > expiry
    except:
        return "InvalidDate"


# ---------------------------
# Cache (Drift Detection)
# ---------------------------

def load_cache():
    if not os.path.exists(CACHE_FILE):
        return set()
    with open(CACHE_FILE) as f:
        return set(line.strip() for line in f)


def save_cache(ami_ids):
    with open(CACHE_FILE, "w") as f:
        for ami in ami_ids:
            f.write(f"{ami}\n")


# ---------------------------
# Processing Logic
# ---------------------------

def process_amis(amis, mode):
    rows = []

    for ami in amis:
        expiry = get_tag_value(ami.get("Tags"), "expiry")

        record = {
            "ImageId": ami['ImageId'],
            "Name": ami.get("Name"),
            "AgeDays": calculate_age(ami['CreationDate']),
            "Expiry": expiry,
            "Expired": is_expired(expiry) if expiry else None,
            "Snapshots": get_snapshots_from_ami(ami)
        }

        if mode == "all":
            rows.append(record)

        elif mode == "expiry" and expiry:
            rows.append(record)

        elif mode == "unmanaged" and not expiry:
            rows.append(record)

    return rows


# ---------------------------
# Output Functions
# ---------------------------

def print_table(rows):
    print("\n{:<20} {:<8} {:<20} {:<12} {}".format(
        "AMI ID", "Age", "Name", "Expiry", "Snapshots"))
    print("-" * 90)

    for r in rows:
        print("{:<20} {:<8} {:<20} {:<12} {}".format(
            r['ImageId'],
            r['AgeDays'],
            (r['Name'] or "N/A")[:19],
            r['Expiry'] or "N/A",
            ",".join(r['Snapshots']) if r['Snapshots'] else "None"
        ))


def write_csv(rows):
    filename = f"ami_audit_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"

    with open(filename, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n✅ CSV generated: {filename}")


# ---------------------------
# Main Menu
# ---------------------------

def main():
    amis = get_amis()

    while True:
        print("\nAMI Audit Menu (CloudShell)")
        print("1. Drift Check (new AMIs)")
        print("2. All AMIs")
        print("3. AMIs with Expiry Tag")
        print("4. Unmanaged AMIs")
        print("0. Exit")

        choice = input("Choice: ").strip()

        if choice == "0":
            print("Exiting...")
            break

        print("\nOutput Format")
        print("1. Table")
        print("2. CSV")

        output_choice = input("Choice: ").strip()

        cache = load_cache()
        current_ami_ids = {ami['ImageId'] for ami in amis}

        if choice == "1":
            new_amis = current_ami_ids - cache
            rows = [r for r in process_amis(amis, "all")
                    if r['ImageId'] in new_amis]
            save_cache(current_ami_ids)

        elif choice == "2":
            rows = process_amis(amis, "all")

        elif choice == "3":
            rows = process_amis(amis, "expiry")

        elif choice == "4":
            rows = process_amis(amis, "unmanaged")

        else:
            print("Invalid choice")
            continue

        if not rows:
            print("\nNo data found.")
            continue

        if output_choice == "1":
            print_table(rows)

        elif output_choice == "2":
            write_csv(rows)

        else:
            print("Invalid output choice")


if __name__ == "__main__":
    main()
