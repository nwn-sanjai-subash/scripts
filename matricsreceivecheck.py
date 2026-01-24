import boto3
from datetime import datetime, timedelta

INSTANCE_ID = "id"  # Replace with your test instance

cloudwatch = boto3.client('cloudwatch')

def has_recent_datapoints(metric):
    end_time = datetime.utcnow()
    start_time = end_time - timedelta(minutes=15)
    response = cloudwatch.get_metric_statistics(
        Namespace=metric['Namespace'],
        MetricName=metric['MetricName'],
        Dimensions=metric['Dimensions'],
        StartTime=start_time,
        EndTime=end_time,
        Period=60,
        Statistics=['Average']
    )
    return len(response['Datapoints']) > 0

def check_instance_metrics(instance_id):
    print(f"🔍 Checking memory-related metrics for instance: {instance_id}")
    metrics = cloudwatch.list_metrics(Dimensions=[{'Name': 'InstanceId', 'Value': instance_id}])
    found = False
    for m in metrics['Metrics']:
        name = m['MetricName'].lower()
        if 'mem' in name or 'memory' in name:
            print(f"  📊 Found metric: {m['MetricName']} (Namespace: {m['Namespace']})")
            if has_recent_datapoints(m):
                print("    ✅ Receiving data.")
                return True
            else:
                print("    ❌ No recent data.")
                found = True
    if not found:
        print("  ⚠️ No memory-related metrics found.")
    return False

# Run the check
if check_instance_metrics(INSTANCE_ID):
    print("\n✅ Instance has working memory metrics.\n")
else:
    print("\n❌ Instance is missing memory metrics or no recent data.\n")
