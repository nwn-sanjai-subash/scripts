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
    Returns an ordered dict: { mw_name: [row, ...] }
    Instances in multiple MWs (separated by ' | ') appear under each MW.
    'Not in any configured MW' entries go last.
    """
    groups = defaultdict(list)

    for row in rows:
        mw_field = row.get("MaintenanceWindow", "").strip()

        if not mw_field or mw_field == NOT_IN_MW_LABEL:
            groups[NOT_IN_MW_LABEL].append(row)
        else:
            # Handle instances listed under multiple MWs (e.g. "List1 | List2")
            mw_list = [m.strip() for m in mw_field.split("|")]
            for mw in mw_list:
                groups[mw].append(row)

    # Sort: configured MWs alphabetically, then "Not in any configured MW" last
    ordered = {}
    for key in sorted(k for k in groups if k != NOT_IN_MW_LABEL):
        ordered[key] = groups[key]
    if NOT_IN_MW_LABEL in groups:
        ordered[NOT_IN_MW_LABEL] = groups[NOT_IN_MW_LABEL]

    return ordered


def print_report(groups, source_file):
    total_instances = sum(len(v) for v in groups.values())

    print()
    print("=" * 65)
    print("  MAINTENANCE WINDOW — SERVER GROUPING REPORT")
    print(f"  Source  : {source_file}")
    print(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Total instances (incl. multi-MW): {total_instances}")
    print("=" * 65)

    for mw_name, instances in groups.items():
        is_unassigned = (mw_name == NOT_IN_MW_LABEL)

        print()
        if is_unassigned:
            print(f"  ⚠  {mw_name}  ({len(instances)} instances)")
            print("  " + "─" * 60)
            print("  ACTION NEEDED: Add these servers to the correct MW manually")
        else:
            print(f"  ▸  {mw_name}  ({len(instances)} instances)")
            print("  " + "─" * 60)

        # Header row
        print(f"  {'#':<4} {'InstanceName':<32} {'InstanceId':<20} {'OS':<8} {'State':<10} {'Patch Available'}")
        print(f"  {'─'*4} {'─'*32} {'─'*20} {'─'*8} {'─'*10} {'─'*16}")

        for idx, row in enumerate(instances, 1):
            name      = row.get("InstanceName", "N/A")[:31]
            iid       = row.get("InstanceId", "N/A")
            os_type   = row.get("OSType", "N/A")[:7]
            state     = row.get("InstanceState", "N/A")[:9]
            patch     = row.get("Patch-Available-Under-AWS-RunPatchBaseline", "N/A")

            print(f"  {idx:<4} {name:<32} {iid:<20} {os_type:<8} {state:<10} {patch}")

    # ── Summary footer ─────────────────────────────────────────────────────────
    print()
    print("=" * 65)
    print("  SUMMARY")
    print("  " + "─" * 60)
    for mw_name, instances in groups.items():
        label = f"  {mw_name}"
        print(f"{label:<45} {len(instances):>4} instances")

    unassigned_count = len(groups.get(NOT_IN_MW_LABEL, []))
    if unassigned_count:
        print()
        print(f"  ⚠  {unassigned_count} instance(s) not assigned to any MW — add manually.")

    print("=" * 65)
    print()


def main():
    csv_file = pick_csv_file()
    rows = load_csv(csv_file)

    if not rows:
        print("[ERROR] CSV file is empty.")
        sys.exit(1)

    groups = group_by_mw(rows)
    print_report(groups, os.path.basename(csv_file))


if __name__ == "__main__":
    main()
