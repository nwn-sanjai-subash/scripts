import boto3
import json
from datetime import datetime, timedelta, timezone

# Set your instance ID here
INSTANCE_ID = "i-xxxxxxxxxxxxxxxxx"

# Output file
OUTPUT_FILE = "ec2_start_failures.txt"

# Time range: last 48 hours
end_time = datetime.now(timezone.utc)
start_time = end_time - timedelta(hours=48)

client = boto3.client("cloudtrail")

with open(OUTPUT_FILE, "w") as f:

    header = (
        f"Checking failed EC2 start attempts for {INSTANCE_ID}\n"
        f"From: {start_time} To: {end_time}\n"
        + "-" * 80 + "\n"
    )
    print(header)
    f.write(header)

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

            if event_name not in ["StartInstances", "RunInstances"]:
                continue

            try:
                instance_id = cloudtrail_event["requestParameters"]["instancesSet"]["items"][0]["instanceId"]
            except Exception:
                continue

            if instance_id != INSTANCE_ID:
                continue

            error_code = cloudtrail_event.get("errorCode")

            if error_code:
                found = True
                output = (
                    f"\nTime        : {cloudtrail_event.get('eventTime')}\n"
                    f"Event       : {event_name}\n"
                    f"Instance    : {instance_id}\n"
                    f"User        : {cloudtrail_event.get('userIdentity', {}).get('arn')}\n"
                    f"Error Code  : {error_code}\n"
                    f"Error Msg   : {cloudtrail_event.get('errorMessage')}\n"
                    + "-" * 80 + "\n"
                )
                print(output)
                f.write(output)

        next_token = response.get("NextToken")
        if not next_token:
            break

    if not found:
        msg = "\nNo failed start attempts found in the last 48 hours.\n"
        print(msg)
        f.write(msg)

print(f"\nOutput saved to: {OUTPUT_FILE}")
