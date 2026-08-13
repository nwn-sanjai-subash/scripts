import boto3

ssm = boto3.client('ssm')

WINDOW_NAMES = [
    'mw-automated-patch',
    'mw-automated-patch-aspera',
    'mw-automated-patch-tech-edit',
    'mw-tech-edit-gpu-sequential-patch-mw'
]

def get_window_by_name(name):
    paginator = ssm.get_paginator('describe_maintenance_windows')
    for page in paginator.paginate():
        for w in page['WindowIdentities']:
            if w['Name'] == name:
                return w
    return None

for name in WINDOW_NAMES:
    print('=' * 100)
    print(f'MAINTENANCE WINDOW: {name}')
    print('=' * 100)

    window = get_window_by_name(name)

    if not window:
        print('Not found')
        continue

    window_id = window['WindowId']

    details = ssm.get_maintenance_window(WindowId=window_id)

    print(f'Window ID                 : {window_id}')
    print(f'Schedule                  : {details.get("Schedule")}')
    print(f'Duration                  : {details.get("Duration")}')
    print(f'Cutoff                    : {details.get("Cutoff")}')
    print(f'Allow Unassociated Targets: {details.get("AllowUnassociatedTargets")}')

    targets = ssm.describe_maintenance_window_targets(WindowId=window_id)['Targets']
    print(f'Target Groups             : {len(targets)}')

    for t in targets:
        count = 0
        for item in t.get('Targets', []):
            count += len(item.get('Values', []))
        print(f'  - {t["Name"]} ({count} instances)')

    tasks = ssm.describe_maintenance_window_tasks(WindowId=window_id)['Tasks']
    print(f'Tasks                     : {len(tasks)}')

    for task in sorted(tasks, key=lambda x: x.get('Priority', 0)):
        print(f'  - Name           : {task.get("Name")}')
        print(f'    Type           : {task.get("Type")}')
        print(f'    Task ARN       : {task.get("TaskArn")}')
        print(f'    Priority       : {task.get("Priority")}')
        print(f'    Max Concurrency: {task.get("MaxConcurrency")}')
        print(f'    Max Errors     : {task.get("MaxErrors")}')

    print()
