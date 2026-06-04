#!/usr/bin/env python3
"""
MW Grouping Report - READ ONLY
Reads the patch_inventory CSV and prints servers grouped by Maintenance Window.
No AWS calls. No file writes. Pure read + display.

Usage:
    python3 mw_grouping_report.py                          # auto-picks latest CSV
    python3 mw_grouping_report.py patch_inventory_XYZ.csv  # use specific file
"""

import csv
import sys
import os
import glob
from collections import defaultdict
from datetime import datetime


# ─── CONFIG ────────────────────────────────────────────────────────────────────
NOT_IN_MW_LABEL = "Not in any configured MW"
# ───────────────────────────────────────────────────────────────────────────────


def pick_csv_file():
    """Auto-select the most recent patch_inventory_*.csv in current directory."""
    if len(sys.argv) > 1:
        path = sys.argv[1]
        if not os.path.isfile(path):
            print(f"[ERROR] File not found: {path}")
            sys.exit(1)
        return path

    matches = sorted(glob.glob("patch_inventory_*.csv"), reverse=True)
    if not matches:
        print("[ERROR] No patch_inventory_*.csv file found in current directory.")
        print("        Run fetch_patch_inventory.py first, or pass the filename as an argument:")
        print("        python3 mw_grouping_report.py <filename.csv>")
        sys.exit(1)

    print(f"[INFO] Auto-selected: {matches[0]}")
    if len(matches) > 1:
        print(f"       (Other available files: {', '.join(matches[1:])})")
    return matches[0]


def load_csv(filepath):
    rows = []
    with open(filepath, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def group_by_mw(rows):
    """
    Returns:
        ordered dict: { mw_name: [row, ...] }  — deduplicated per MW by InstanceId
        total_unique: int                       — unique instances across the whole CSV

    Instances listed under multiple MWs (e.g. "List1 | List2") appear under
    each relevant MW section once, but are counted only once in the total.
    """
    groups = defaultdict(list)
    seen_per_mw = defaultdict(set)   # tracks instance IDs already added to each MW

    for row in rows:
        mw_field = row.get("MaintenanceWindow", "").strip()
        iid = row.get("InstanceId", "")

        if not mw_field or mw_field == NOT_IN_MW_LABEL:
            if iid not in seen_per_mw[NOT_IN_MW_LABEL]:
                groups[NOT_IN_MW_LABEL].append(row)
                seen_per_mw[NOT_IN_MW_LABEL].add(iid)
        else:
            # Split "List1 | List2" style entries into individual MW names
            mw_list = [m.strip() for m in mw_field.split("|")]
            for mw in mw_list:
                if iid not in seen_per_mw[mw]:
                    groups[mw].append(row)
                    seen_per_mw[mw].add(iid)

    # Total unique instances across the entire CSV (not sum of per-MW counts)
    all_seen_ids = {iid for ids in seen_per_mw.values() for iid in ids}
    total_unique = len(all_seen_ids)

    # Sort: configured MWs alphabetically, then "Not in any configured MW" last
    ordered = {}
    for key in sorted(k for k in groups if k != NOT_IN_MW_LABEL):
        ordered[key] = groups[key]
    if NOT_IN_MW_LABEL in groups:
        ordered[NOT_IN_MW_LABEL] = groups[NOT_IN_MW_LABEL]

    return ordered, total_unique


def print_report(groups, source_file, total_unique):
    # Dynamically size the InstanceName column to the longest name in the entire dataset
    all_names = [
        row.get("InstanceName", "N/A")
        for instances in groups.values()
        for row in instances
    ]
    name_col_width = max(len(n) for n in all_names + ["InstanceName"])

    print()
    print("=" * 80)
    print("  MAINTENANCE WINDOW — SERVER GROUPING REPORT")
    print(f"  Source   : {source_file}")
    print(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Total unique instances: {total_unique}")
    print("=" * 80)

    for mw_name, instances in groups.items():
        is_unassigned = (mw_name == NOT_IN_MW_LABEL)

        print()
        if is_unassigned:
            print(f"  ⚠  {mw_name}  ({len(instances)} instances)")
            print("  " + "─" * 75)
            print("  ACTION NEEDED: Add these servers to the correct MW manually")
        else:
            print(f"  ▸  {mw_name}  ({len(instances)} instances)")
            print("  " + "─" * 75)

        # Header row — InstanceName column width is dynamic, no truncation
        print(f"  {'#':<4} {'InstanceName':<{name_col_width}} {'InstanceId':<20} {'OS':<8} {'State':<10} {'Patch Available'}")
        print(f"  {'─'*4} {'─'*name_col_width} {'─'*20} {'─'*8} {'─'*10} {'─'*16}")

        for idx, row in enumerate(instances, 1):
            name    = row.get("InstanceName", "N/A")        # full name, no truncation
            iid     = row.get("InstanceId", "N/A")
            os_type = row.get("OSType", "N/A")[:7]
            state   = row.get("InstanceState", "N/A")[:9]
            patch   = row.get("Patch-Available-Under-AWS-RunPatchBaseline", "N/A")

            print(f"  {idx:<4} {name:<{name_col_width}} {iid:<20} {os_type:<8} {state:<10} {patch}")

    # ── Summary footer ─────────────────────────────────────────────────────────
    print()
    print("=" * 80)
    print("  SUMMARY")
    print("  " + "─" * 75)
    for mw_name, instances in groups.items():
        label = f"  {mw_name}"
        print(f"{label:<50} {len(instances):>4} instances")

    print()
    print(f"  {'Total unique instances':<48} {total_unique:>4}")

    unassigned_count = len(groups.get(NOT_IN_MW_LABEL, []))
    if unassigned_count:
        print()
        print(f"  ⚠  {unassigned_count} instance(s) not assigned to any MW — add manually.")

    print("=" * 80)
    print()


def main():
    csv_file = pick_csv_file()
    rows = load_csv(csv_file)

    if not rows:
        print("[ERROR] CSV file is empty.")
        sys.exit(1)

    groups, total_unique = group_by_mw(rows)
    print_report(groups, os.path.basename(csv_file), total_unique)


if __name__ == "__main__":
    main()
