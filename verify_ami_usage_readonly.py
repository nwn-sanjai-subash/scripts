import boto3
from collections import defaultdict

KEYWORDS = ["iv", "mcs", "intervision"]

ec2 = boto3.client("ec2")
asg = boto3.client("autoscaling")
eks = boto3.client("eks")
cfn = boto3.client("cloudformation")

# -------------------------
# Fetch AMIs (keyword only)
# -------------------------
images = ec2.describe_images(Owners=["self"])["Images"]

amis = {}
for i in images:
    name = i.get("Name", "")
    if name and any(k in name.lower() for k in KEYWORDS):
        amis[i["ImageId"]] = name

# -------------------------
# EC2 Instances usage
# -------------------------
ec2_usage = defaultdict(list)
for r in ec2.describe_instances()["Reservations"]:
    for inst in r["Instances"]:
        ami = inst["ImageId"]
        if ami in amis:
            ec2_usage[ami].append(inst["InstanceId"])

# -------------------------
# Launch Templates usage
# -------------------------
lt_usage = defaultdict(list)
lts = ec2.describe_launch_templates()["LaunchTemplates"]

for lt in lts:
    versions = ec2.describe_launch_template_versions(
        LaunchTemplateId=lt["LaunchTemplateId"]
    )["LaunchTemplateVersions"]

    for v in versions:
        img = v["LaunchTemplateData"].get("ImageId")
        if img in amis:
            lt_usage[img].append(
                f"{lt['LaunchTemplateName']} (v{v['VersionNumber']})"
            )

# -------------------------
# Auto Scaling Groups usage
# -------------------------
asg_usage = defaultdict(list)
asgs = asg.describe_auto_scaling_groups()["AutoScalingGroups"]

for g in asgs:
    if "LaunchTemplate" in g:
        lt_id = g["LaunchTemplate"]["LaunchTemplateId"]
        lt_ver = g["LaunchTemplate"]["Version"]

        lt_data = ec2.describe_launch_template_versions(
            LaunchTemplateId=lt_id,
            Versions=[lt_ver]
        )["LaunchTemplateVersions"][0]["LaunchTemplateData"]

        img = lt_data.get("ImageId")
        if img in amis:
            asg_usage[img].append(g["AutoScalingGroupName"])

# -------------------------
# EKS Node Groups usage
# -------------------------
eks_usage = defaultdict(list)
clusters = eks.list_clusters()["clusters"]

for c in clusters:
    nodegroups = eks.list_nodegroups(clusterName=c)["nodegroups"]
    for ng in nodegroups:
        ngd = eks.describe_nodegroup(
            clusterName=c,
            nodegroupName=ng
        )["nodegroup"]

        lt = ngd.get("launchTemplate")
        if lt:
            lt_id = lt["id"]
            ver = lt["version"]

            lt_data = ec2.describe_launch_template_versions(
                LaunchTemplateId=lt_id,
                Versions=[ver]
            )["LaunchTemplateVersions"][0]["LaunchTemplateData"]

            img = lt_data.get("ImageId")
            if img in amis:
                eks_usage[img].append(f"{c}/{ng}")

# -------------------------
# CloudFormation usage (template scan)
# -------------------------
cfn_usage = defaultdict(list)

valid_states = [
    "CREATE_COMPLETE",
    "UPDATE_COMPLETE",
    "UPDATE_ROLLBACK_COMPLETE"
]

stacks = cfn.list_stacks()["StackSummaries"]

for s in stacks:
    if s["StackStatus"] not in valid_states:
        continue

    stack_name = s["StackName"]
    template = cfn.get_template(StackName=stack_name)["TemplateBody"]

    template_text = str(template)

    for ami in amis:
        if ami in template_text:
            cfn_usage[ami].append(stack_name)

# -------------------------
# Output table
# -------------------------
print("\nAMI USAGE VERIFICATION REPORT (READONLY)\n")
print(f"{'AMI ID':<18} {'EC2':<6} {'LT':<6} {'ASG':<6} {'EKS':<6} {'CFN':<6} Name")
print("-" * 110)

for ami, name in amis.items():
    print(
        f"{ami:<18} "
        f"{'YES' if ami in ec2_usage else 'NO':<6} "
        f"{'YES' if ami in lt_usage else 'NO':<6} "
        f"{'YES' if ami in asg_usage else 'NO':<6} "
        f"{'YES' if ami in eks_usage else 'NO':<6} "
        f"{'YES' if ami in cfn_usage else 'NO':<6} "
        f"{name[:40]}"
    )
