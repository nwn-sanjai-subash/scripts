#!/usr/bin/env python3
'''
ssm_tomcat_report.py

Run this from AWS CloudShell (or any environment with AWS credentials configured).
It lists SSM-managed instances, prompts you to choose one, executes a read-only inspection
script on the selected instance via SSM (AWS-RunShellScript), captures the CSV output and
saves it locally.

IMPORTANT: The remote script executed on the instance is read-only. It only reads files
(e.g., /home/*/server.xml), parses them in memory, and prints CSV to stdout. It does not
write or modify files on the instance. On the AWS side this uses SSM SendCommand/GetCommandInvocation;
these API calls create SSM command invocations but do not modify instance configuration.

Required IAM permissions (minimum):
 - ssm:DescribeInstanceInformation
 - ssm:SendCommand
 - ssm:GetCommandInvocation
 - ec2:DescribeInstances

Usage:
  python3 ssm_tomcat_report.py
'''
from __future__ import print_function
import boto3
import botocore
import time
import sys
import os
from datetime import datetime

SSM_POLL_INTERVAL = 3  # seconds
SSM_MAX_WAIT = 600     # seconds

def list_ssm_instances(ssm_client, ec2_client):
    """Return list of dicts: [{'InstanceId': id, 'Name': name, 'PlatformName': p, 'PingStatus': s}, ...]"""
    instances = []
    paginator = ssm_client.get_paginator('describe_instance_information')
    try:
        for page in paginator.paginate():
            for info in page.get('InstanceInformationList', []):
                iid = info.get('InstanceId')
                # Try to get Name tag using EC2 describe (may fail for non-EC2 managed instances)
                name = None
                try:
                    resp = ec2_client.describe_instances(InstanceIds=[iid])
                    for r in resp.get('Reservations', []):
                        for inst in r.get('Instances', []):
                            tags = inst.get('Tags', [])
                            for t in tags:
                                if t.get('Key') == 'Name':
                                    name = t.get('Value')
                                    break
                            # stop after first match
                            if name:
                                break
                        if name:
                            break
                except botocore.exceptions.ClientError:
                    name = None
                instances.append({
                    'InstanceId': iid,
                    'Name': name or '',
                    'PlatformName': info.get('PlatformName', ''),
                    'PingStatus': info.get('PingStatus', '')
                })
    except botocore.exceptions.ClientError as e:
        print("Error listing SSM instances:", e, file=sys.stderr)
        sys.exit(1)
    return instances

def prompt_user_choice(instances):
    if not instances:
        print("No SSM-managed instances found in this account/region.", file=sys.stderr)
        sys.exit(1)
    print("SSM-managed instances found:")
    for i, inst in enumerate(instances, start=1):
        display = f"{inst['InstanceId']}"
        if inst['Name']:
            display += f"  (Name: {inst['Name']})"
        display += f"  Platform: {inst['PlatformName']}  Ping: {inst['PingStatus']}"
        print(f"[{i}] {display}")
    print()
    while True:
        choice = input("Pick instance number to inspect (or 'q' to quit): ").strip()
        if choice.lower() in ('q', 'quit', 'exit'):
            print("Aborted by user.")
            sys.exit(0)
        if not choice.isdigit():
            print("Please enter a number corresponding to the instance.")
            continue
        idx = int(choice)
        if 1 <= idx <= len(instances):
            return instances[idx - 1]
        print("Out of range. Try again.")

def send_readonly_script(ssm_client, instance_id):
    """
    The remote 'readonly' script:
    - Enumerates /home/* directories
    - Picks the latest server.xml per user (by mtime)
    - Extracts redirectPort for executor='tomcatThreadPool' (comment-safe using perl)
    - Falls back to detecting SSL connector port if redirectPort absent
    - Prints CSV to stdout (username,tomcat_home,redirect_port,status,picked_serverxml)
    """
    remote_shell_script = r'''#!/usr/bin/env bash
set -euo pipefail
# Read-only remote inspection script — prints CSV to stdout

csv_quote() {
  local val="$1"
  if [[ -z "$val" ]]; then
    printf ''
    return
  fi
  val="${val//"/""}"
  printf '"%s"' "$val"
}

printf '%s
' "username,tomcat_home,redirect_port,status,picked_serverxml"

# Build listening ports map
declare -A LISTEN
while IFS= read -r port; do
  [[ -n "$port" ]] && LISTEN["$port"]=1
done < <(ss -ltn 2>/dev/null | awk 'NR>1 {print $4}' | awk -F: '{print $NF}' | sort -u || true)

for userdir in /home/*; do
  [ -d "$userdir" ] || continue
  username=$(basename "$userdir")

  latest_entry=$(find "$userdir" -type f -name server.xml 2>/dev/null -exec stat -c '%Y %n' {} \; | sort -nr | head -n1 || true)
  if [[ -z "$latest_entry" ]]; then
    printf '%s,%s,%s,%s,%s
' "$(csv_quote "$username")" "" "" "no-serverxml" ""
    continue
  fi

  latest_file="${latest_entry#* }"
  tomcat_home=$(dirname "$(dirname "$latest_file")")

  # Prefer Perl extraction (multi-line, comment-safe) if perl exists
  redirect_port=""
  if command -v perl >/dev/null 2>&1; then
    redirect_port=$(perl -0777 -ne "s/<!--.*?-->//gs; while (/<Connector\b([^>]*?)\/?\>/gis) { \$a=\$1; if (\$a =~ /executor\s*=\s*['\"]tomcatThreadPool['\"]/i) { if (\$a =~ /redirectPort\s*=\s*['\"](\d+)['\"]/) { print "\$1\n"; exit } } }" "$latest_file" 2>/dev/null || true)
  fi

  # Fallback: awk-based detection of redirectPort (less robust but available)
  if [[ -z "${redirect_port:-}" ]]; then
    redirect_port=$(awk 'BEGIN{in_comment=0; in_conn=0; buf=""}
      /<!--/ { in_comment=1 }
      /-->/  { if(in_comment){ sub(/^.*-->/,""); in_comment=0 } }
      in_comment==1 { next }
      /<Connector/ { in_conn=1; buf=$0; if(/>/){ print buf; in_conn=0; buf="" } ; next }
      in_conn==1 { buf=buf " " $0; if(/>/){ print buf; in_conn=0; buf="" } ; next }
      { next }
    ' "$latest_file" 2>/dev/null || true | grep -i 'executor=["'"'"']tomcatThreadPool["'"'"']' | sed -n 's/.*redirectPort=["'"'"']\([0-9]\+\)["'"'"'].*/\1/p' || true)
  fi

  # Secondary fallback: detect HTTPS/SSL connector port if no redirectPort
  if [[ -z "${redirect_port:-}" ]]; then
    if command -v perl >/dev/null 2>&1; then
      redirect_port=$(perl -0777 -ne "s/<!--.*?-->//gs; while (/<Connector\b([^>]*?)\/?\>/gis) { \$a=\$1; if (\$a =~ /scheme\s*=\s*['\"]https['\"]/i || \$a =~ /SSLEnabled\b/i || \$a =~ /secure\s*=\s*['\"]true['\"]/i || \$a =~ /keystoreFile\s*=\s*['\"]/i) { if (\$a =~ /port\s*=\s*['\"](\d+)['\"]/) { print "\$1\n"; exit } } }" "$latest_file" 2>/dev/null || true)
    else
      redirect_port=$(awk 'BEGIN{in_comment=0; in_conn=0; buf=""}
        /<!--/ { in_comment=1 }
        /-->/  { if(in_comment){ sub(/^.*-->/,""); in_comment=0 } }
        in_comment==1 { next }
        /<Connector/ { in_conn=1; buf=$0; if(/>/){ print buf; in_conn=0; buf="" } ; next }
        in_conn==1 { buf=buf " " $0; if(/>/){ print buf; in_conn=0; buf="" } ; next }
        { next }
      ' "$latest_file" 2>/dev/null || true | awk '/scheme=["'"'"']https["'"'"']|SSLEnabled|secure=["'"'"']true["'"'"']|keystoreFile/ { if (match($0,/port=["'"'"']([0-9]+)["'"'"']/,m)) print m[1] }' | head -n1 || true)
    fi
  fi

  redirect_port=${redirect_port:-}

  if [[ -n "$redirect_port" ]]; then
    if [[ -n "${LISTEN[$redirect_port]:-}" ]]; then
      status="running"
    else
      status="stopped"
    fi
  else
    status="no-redirectport"
  fi

  printf '%s,%s,%s,%s,%s
' "$(csv_quote "$username")" "$(csv_quote "$tomcat_home")" "$(csv_quote "$redirect_port")" "$(csv_quote "$status")" "$(csv_quote "$latest_file")"
done
'''
    # Return the script as the first (and only) command to run
    return remote_shell_script

def run_ssm_command_and_wait(ssm_client, instance_id, command_string):
    try:
        resp = ssm_client.send_command(
            InstanceIds=[instance_id],
            DocumentName='AWS-RunShellScript',
            Parameters={'commands': [command_string]},
            TimeoutSeconds=SSM_MAX_WAIT
        )
    except botocore.exceptions.ClientError as e:
        print("Failed to send SSM command:", e, file=sys.stderr)
        sys.exit(1)

    cmd_id = resp['Command']['CommandId']
    print(f"Sent SSM command {cmd_id} to instance {instance_id}. Waiting for completion...")

    elapsed = 0
    while True:
        try:
            inv = ssm_client.get_command_invocation(CommandId=cmd_id, InstanceId=instance_id)
        except botocore.exceptions.ClientError as e:
            # Sometimes AWS returns InvocationDoesNotExist briefly; keep waiting a bit
            if 'InvocationDoesNotExist' in str(e):
                time.sleep(1)
                elapsed += 1
                if elapsed > SSM_MAX_WAIT:
                    print("Timeout waiting for command invocation to appear.", file=sys.stderr)
                    sys.exit(1)
                continue
            print("Error fetching command invocation:", e, file=sys.stderr)
            sys.exit(1)

        status = inv.get('Status')
        if status in ('Pending', 'InProgress', 'Delayed', 'Cancelling'):
            if elapsed >= SSM_MAX_WAIT:
                print("Timed out waiting for SSM command to finish.", file=sys.stderr)
                sys.exit(1)
            time.sleep(SSM_POLL_INTERVAL)
            elapsed += SSM_POLL_INTERVAL
            continue

        # Finished one way or another
        stdout = inv.get('StandardOutputContent', '') or ''
        stderr = inv.get('StandardErrorContent', '') or ''
        if status == 'Success':
            return stdout
        else:
            print(f"SSM command finished with status: {status}", file=sys.stderr)
            if stderr:
                print("Standard error from remote command:", file=sys.stderr)
                print(stderr, file=sys.stderr)
            # Still return stdout if present (could contain partial results)
            return stdout

def safe_filename(name):
    # produce a filesystem-safe filename
    keep = (' ', '.', '_', '-')
    return "".join(c for c in name if c.isalnum() or c in keep).rstrip()

def main():
    session = boto3.Session()
    ssm = session.client('ssm')
    ec2 = session.client('ec2')

    instances = list_ssm_instances(ssm, ec2)
    chosen = prompt_user_choice(instances)
    instance_id = chosen['InstanceId']
    inst_name = chosen.get('Name') or instance_id

    print(f"Selected instance: {instance_id} (Name: {inst_name})")

    remote_cmd = send_readonly_script(ssm, instance_id)
    output = run_ssm_command_and_wait(ssm, instance_id, remote_cmd)

    if not output:
        print("No output returned from remote inspection. The instance may not have any /home/*/server.xml files, or the command failed.", file=sys.stderr)
        sys.exit(1)

    timestamp = datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
    safe_name = safe_filename(inst_name)
    local_filename = f"{safe_name}_tomcat_redirects_{timestamp}.csv"
    with open(local_filename, 'w', encoding='utf-8') as fh:
        fh.write(output)

    print(f"CSV saved to: {os.path.abspath(local_filename)}")
    print("Done. The script was read-only on AWS and on the instance (only read operations were performed).")

if __name__ == '__main__':
    main()
