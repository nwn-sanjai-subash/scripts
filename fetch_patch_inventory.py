#!/usr/bin/env python3
"""
AWS Patch Inventory Fetcher - READ ONLY
Fetches EC2 instance details + patch availability from AWS-RunPatchBaseline
Designed to run in AWS CloudShell (region should match where instances live)

Maintenance Windows: List1, List2 (concurrent), List3
Output: patch_inventory_<timestamp>.csv
"""

import boto3
import csv
import json
import sys
from datetime import datetime, timezone


# ─── CONFIG ────────────────────────────────────────────────────────────────────
# Maintenance Window names — adjust if yours differ slightly
MW_NAMES = ["mw-automated-patch-tech-edit", "mw-automated-patch-aspera", "mw-automated-patch"]

OUTPUT_FILE = f"patch_inventory_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

CSV_HEADERS = [
    "InstanceName",
    "InstanceId",
    "InstanceType",
    "InstanceState",
    "OSType",
    "OSVersion",
    "Hostname",
    "MaintenanceWindow",
    "Patch-Available-Under-AWS-RunPatchBaseline",
]
# ───────────────────────────────────────────────────────────────────────────────


def get_mw_instance_map(ssm):
    """
    Returns a dict: { instance_id: [mw_name, ...] }
    by reading registered targets from each Maintenance Window.
    """
    mw_instance_map = {}

    # List all MWs and filter by our names
    paginator = ssm.get_paginator("describe_maintenance_windows")
    mw_ids = {}
    for page in paginator.paginate(Filters=[{"Key": "Enabled", "Values": ["true"]}]):
        for mw in page.get("WindowIdentities", []):
            if mw["Name"] in MW_NAMES:
                mw_ids[mw["WindowId"]] = mw["Name"]

    if not mw_ids:
        print(f"[WARN] No Maintenance Windows found matching: {MW_NAMES}")
        print("       Check MW_NAMES in the config section — names are case-sensitive.")

    for window_id, window_name in mw_ids.items():
        target_paginator = ssm.get_paginator("describe_maintenance_window_targets")
        for page in target_paginator.paginate(WindowId=window_id):
            for target in page.get("Targets", []):
                for t in target.get("Targets", []):
                    if t["Key"] == "InstanceIds":
                        for iid in t["Values"]:
                            mw_instance_map.setdefault(iid, []).append(window_name)
                    # Also handle tag-based targets (best effort label)
                    elif t["Key"].startswith("tag:"):
                        # Tag-based targets — can't resolve individual IDs here
                        # Mark as tag-targeted for manual review
                        pass

    return mw_instance_map


def get_patch_compliance(ssm, instance_id):
    """
    Returns True/False/None for patch availability:
      True  → patches available (NON_COMPLIANT)
      False → fully patched (COMPLIANT)
      None  → no SSM compliance data found
    """
    try:
        resp = ssm.list_compliance_items(
            Filters=[
                {"Key": "ComplianceType", "Values": ["Patch"]},
                {"Key": "Status", "Values": ["NON_COMPLIANT"]},
            ],
            ResourceIds=[instance_id],
            ResourceTypes=["ManagedInstance"],
            MaxResults=1,
        )
        items = resp.get("ComplianceItems", [])
        if items:
            return "Yes"  # At least one non-compliant patch found

        # Double-check: confirm COMPLIANT data exists at all
        resp2 = ssm.list_compliance_items(
            Filters=[{"Key": "ComplianceType", "Values": ["Patch"]}],
            ResourceIds=[instance_id],
            ResourceTypes=["ManagedInstance"],
            MaxResults=1,
        )
        if resp2.get("ComplianceItems"):
            return "No"  # Data exists, all compliant

        return "Unknown (No SSM data)"

    except ssm.exceptions.InvalidResourceId:
        return "Not Managed by SSM"
    except Exception as e:
        return f"Error: {str(e)[:60]}"


def get_ssm_inventory(ssm, instance_id):
    """
    Returns (hostname, os_type, os_version) from SSM inventory.
    Falls back to 'N/A' if not available.
    """
    hostname = os_type = os_version = "N/A"
    try:
        resp = ssm.get_inventory(
            Filters=[
                {"Key": "AWS:InstanceInformation.InstanceId", "Values": [instance_id]}
            ],
            ResultAttributes=[{"TypeName": "AWS:InstanceInformation"}],
            MaxResults=1,
        )
        entities = resp.get("Entities", [])
        if entities:
            data = entities[0].get("Data", {})
            info = data.get("AWS:InstanceInformation", {})
            content = info.get("Content", [])
            if content:
                c = content[0]
                hostname = c.get("ComputerName", "N/A")
                os_type = c.get("PlatformType", "N/A")
                os_version = c.get("PlatformName", "N/A")
                if c.get("PlatformVersion"):
                    os_version += f" {c['PlatformVersion']}"
    except Exception:
        pass
    return hostname, os_type, os_version


def main():
    print("=" * 60)
    print("AWS Patch Inventory Fetcher (READ-ONLY)")
    print(f"Run at: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 60)

    ec2 = boto3.client("ec2")
    ssm = boto3.client("ssm")

    # ── Step 1: Fetch all running/stopped EC2 instances ──────────────────────
    print("\n[1/4] Fetching EC2 instances...")
    instances = []
    paginator = ec2.get_paginator("describe_instances")
    for page in paginator.paginate(
        Filters=[{"Name": "instance-state-name", "Values": ["running", "stopped", "stopping", "pending"]}]
    ):
        for reservation in page["Reservations"]:
            for inst in reservation["Instances"]:
                instances.append(inst)

    print(f"      Found {len(instances)} instances.")

    # ── Step 2: Build instance-name lookup from tags ──────────────────────────
    def get_tag(tags, key):
        for t in tags or []:
            if t["Key"] == key:
                return t["Value"]
        return "N/A"

    # ── Step 3: Resolve Maintenance Window membership ─────────────────────────
    print("[2/4] Resolving Maintenance Window memberships...")
    mw_instance_map = get_mw_instance_map(ssm)
    print(f"      Mapped {len(mw_instance_map)} instances to MWs.")

    # ── Step 4: Collect data per instance ─────────────────────────────────────
    print(f"[3/4] Collecting SSM inventory + patch compliance for {len(instances)} instances...")
    print("      (This may take a moment...)\n")

    rows = []
    for idx, inst in enumerate(instances, 1):
        iid = inst["InstanceId"]
        name = get_tag(inst.get("Tags"), "Name")
        itype = inst.get("InstanceType", "N/A")
        state = inst["State"]["Name"]

        # SSM inventory for OS details + hostname
        hostname, os_type, os_version = get_ssm_inventory(ssm, iid)

        # Maintenance window membership
        mw_names = mw_instance_map.get(iid, ["Not in any configured MW"])
        mw_label = " | ".join(mw_names)

        # Patch compliance
        patch_available = get_patch_compliance(ssm, iid)

        rows.append({
            "InstanceName": name,
            "InstanceId": iid,
            "InstanceType": itype,
            "InstanceState": state,
            "OSType": os_type,
            "OSVersion": os_version,
            "Hostname": hostname,
            "MaintenanceWindow": mw_label,
            "Patch-Available-Under-AWS-RunPatchBaseline": patch_available,
        })

        # Progress indicator
        print(f"      [{idx:>3}/{len(instances)}] {iid}  {name:<30}  Patch: {patch_available}")

    # ── Step 5: Write CSV ─────────────────────────────────────────────────────
    print(f"\n[4/4] Writing output to {OUTPUT_FILE}...")
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        writer.writeheader()
        writer.writerows(rows)

    print("\n" + "=" * 60)
    print(f"Done! Output: {OUTPUT_FILE}")
    print(f"Total instances exported: {len(rows)}")

    # Summary
    yes_count = sum(1 for r in rows if r["Patch-Available-Under-AWS-RunPatchBaseline"] == "Yes")
    no_count = sum(1 for r in rows if r["Patch-Available-Under-AWS-RunPatchBaseline"] == "No")
    unknown_count = len(rows) - yes_count - no_count
    print(f"\nPatch Status Summary:")
    print(f"  Patches Available (needs patching) : {yes_count}")
    print(f"  Fully Compliant (up to date)       : {no_count}")
    print(f"  Unknown / Not SSM Managed          : {unknown_count}")

    mw_counts = {}
    for r in rows:
        for mw in r["MaintenanceWindow"].split(" | "):
            mw_counts[mw] = mw_counts.get(mw, 0) + 1
    print(f"\nMaintenance Window Breakdown:")
    for mw, count in sorted(mw_counts.items()):
        print(f"  {mw:<35} : {count} instances")
    print("=" * 60)


if __name__ == "__main__":
    main()
