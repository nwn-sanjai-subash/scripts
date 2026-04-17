import boto3
import json
from datetime import datetime, timedelta, timezone

# Set your instance ID here
INSTANCE_ID = "i-xxxxxxxxxxxxxxxxx"

# Time range: last 48 hours
end_time = datetime.now(timezone.utc)
start_time = end_time - timedelta(hours=48)

client = boto3.client("cloudtrail")

print(f"\nChecking failed EC2 start attempts for {INSTANCE_ID}")
print(f"From: {start_time} To: {end_time}")
print("-" * 80)

next_token = None
found = False

while True:
    params = {
        "LookupAttributes": [
            {
                "AttributeKey": "EventSource",
                "AttributeValue": "ec2.amazonaws.com"
            }
        ],
        "StartTime": start_time,
        "EndTime": end_time,
        "MaxResults": 50
    }

    if next_token:
        params["NextToken"] = next_token

    response = client.lookup_events(**params)

    for event in response.get("Events", []):
        try:
            cloudtrail_event = json.loads(event["CloudTrailEvent"])
        except Exception:
            continue

        event_name = cloudtrail_event.get("eventName")

        # Only EC2 start-related events
        if event_name not in ["StartInstances", "RunInstances"]:
            continue

        # Extract instance ID safely
        try:
            instance_id = cloudtrail_event["requestParameters"]["instancesSet"]["items"][0]["instanceId"]
        except Exception:
            continue

        if instance_id != INSTANCE_ID:
            continue

        error_code = cloudtrail_event.get("errorCode")

        # Only failed attempts
        if error_code:
            found = True
            print(f"\nTime        : {cloudtrail_event.get('eventTime')}")
            print(f"Event       : {event_name}")
            print(f"Instance    : {instance_id}")
            print(f"User        : {cloudtrail_event.get('userIdentity', {}).get('arn')}")
            print(f"Error Code  : {error_code}")
            print(f"Error Msg   : {cloudtrail_event.get('errorMessage')}")
            print("-" * 80)

    next_token = response.get("NextToken")
    if not next_token:
        break

if not found:
    print("\nNo failed start attempts found in the last 48 hours.")
