#!/usr/bin/env python3

import boto3
import json
from botocore.exceptions import ClientError

iam = boto3.client("iam")


def get_managed_policy_actions(policy_arn):
    actions = set()

    try:
        policy = iam.get_policy(PolicyArn=policy_arn)
        version_id = policy["Policy"]["DefaultVersionId"]

        version = iam.get_policy_version(
            PolicyArn=policy_arn,
            VersionId=version_id
        )

        document = version["PolicyVersion"]["Document"]

        statements = document.get("Statement", [])

        if not isinstance(statements, list):
            statements = [statements]

        for stmt in statements:
            if stmt.get("Effect") != "Allow":
                continue

            action = stmt.get("Action", [])

            if isinstance(action, str):
                actions.add(action)
            else:
                actions.update(action)

    except ClientError as e:
        print(f"Error reading managed policy {policy_arn}: {e}")

    return actions


def get_inline_policy_actions(role_name, policy_name):
    actions = set()

    try:
        response = iam.get_role_policy(
            RoleName=role_name,
            PolicyName=policy_name
        )

        document = response["PolicyDocument"]

        statements = document.get("Statement", [])

        if not isinstance(statements, list):
            statements = [statements]

        for stmt in statements:
            if stmt.get("Effect") != "Allow":
                continue

            action = stmt.get("Action", [])

            if isinstance(action, str):
                actions.add(action)
            else:
                actions.update(action)

    except ClientError as e:
        print(f"Error reading inline policy {policy_name}: {e}")

    return actions


def get_role_permissions(role_name):
    permissions = set()

    # Managed Policies
    paginator = iam.get_paginator("list_attached_role_policies")

    for page in paginator.paginate(RoleName=role_name):
        for policy in page["AttachedPolicies"]:
            permissions.update(
                get_managed_policy_actions(policy["PolicyArn"])
            )

    # Inline Policies
    paginator = iam.get_paginator("list_role_policies")

    for page in paginator.paginate(RoleName=role_name):
        for policy_name in page["PolicyNames"]:
            permissions.update(
                get_inline_policy_actions(role_name, policy_name)
            )

    return permissions


def compare_roles(role1, role2):
    perms1 = get_role_permissions(role1)
    perms2 = get_role_permissions(role2)

    only_role1 = sorted(perms1 - perms2)
    only_role2 = sorted(perms2 - perms1)
    common = sorted(perms1 & perms2)

    print("=" * 50)
    print("Comparing IAM Roles")
    print("=" * 50)
    print()

    print(f"Role 1 : {role1}")
    print(f"Role 2 : {role2}")
    print()

    print("=" * 50)
    print("SUMMARY")
    print("=" * 50)
    print()

    print(f"Total permissions in {role1}  : {len(perms1)}")
    print(f"Total permissions in {role2} : {len(perms2)}")
    print()

    print(f"Common permissions            : {len(common)}")
    print(f"Only in {role1}               : {len(only_role1)}")
    print(f"Only in {role2}               : {len(only_role2)}")
    print()

    print("=" * 50)
    print(f"ONLY IN {role1}")
    print("=" * 50)
    print()

    for action in only_role1:
        print(action)

    print()
    print("=" * 50)
    print(f"ONLY IN {role2}")
    print("=" * 50)
    print()

    for action in only_role2:
        print(action)

    print()
    print("=" * 50)
    print("COMMON PERMISSIONS")
    print("=" * 50)
    print()

    for action in common:
        print(action)


if __name__ == "__main__":

    role1 = input("Enter first IAM role name: ").strip()
    role2 = input("Enter second IAM role name: ").strip()

    compare_roles(role1, role2)
