# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Integration test constants and markers.

Values come from CATS_INTEG_* environment variables so no real account, VPC or subnet
identifier lives in the tree. CI sets them; export them yourself for a local run.

The account ID is declared rather than read from STS on purpose: tests skip when it is unset
instead of deploying into whatever account the current credentials happen to point at.
"""

import os

import boto3
import pytest

# Primary test account (where tests run from)
REGION = os.environ.get("CATS_INTEG_REGION", "eu-west-1")
ACCOUNT_ID = os.environ.get("CATS_INTEG_ACCOUNT_ID", "")
RUN_ID = os.environ.get("CATS_INTEG_RUN_ID", "local")
VPC_SSM_PARAM = "/cats/integ-test/vpc-id"
SUBNET_SSM_PARAM = "/cats/integ-test/subnet-id"
# The base stack's subnet with no route to the SSM endpoints (integ-test-vpc.yaml, v5+).
ISOLATED_SUBNET_SSM_PARAM = "/cats/integ-test/isolated-subnet-id"
# EC2 AMI for listener instances, as an SSM parameter path so the suite exercises
# existing_ec2_ami_id's SSM resolution. CI may substitute its own image parameter.
AMI_OVERRIDE_SSM_PARAM = os.environ.get("CATS_INTEG_AMI_OVERRIDE", "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64")
# Provisioned by infra/integ-test-vpc.yaml, which infra/deploy_base_stack.py deploys before the
# suite runs. Targets using the lambda handler point existing_security_group_ids at it so no CATS
# stack owns a security group that CloudFormation must wait on Lambda's Hyperplane ENI release
# to delete.
HANDLER_SG_SSM_PARAM = "/cats/integ-test/handler-sg-id"


def stack_prefix(namespace: str) -> str:
    """Return a short stack prefix unique to this CI pipeline and integration module."""
    return f"C-i{RUN_ID}-{namespace}-"


# The per-run VPC for the CATS-created endpoint tests (test_cats_endpoints.py). The stack
# infra/endpoint-test-vpc.yaml writes these parameters and the config reads them, so the test
# consumes its network the way a user names their own, by SSM parameter name. Per run, because private
# DNS allows one interface endpoint per service per VPC and the test has CATS create them.
ENDPOINT_TEST_PARAMETER_PREFIX = f"/cats/integ-test/vpce-{RUN_ID}"
ENDPOINT_TEST_VPC_SSM_PARAM = f"{ENDPOINT_TEST_PARAMETER_PREFIX}/vpc-id"
ENDPOINT_TEST_SUBNET_SSM_PARAM = f"{ENDPOINT_TEST_PARAMETER_PREFIX}/subnet-id"
ENDPOINT_TEST_SG_SSM_PARAM = f"{ENDPOINT_TEST_PARAMETER_PREFIX}/endpoint-sg-id"


# Cross-account test target. Its network is the cross base stack (infra/integ-test-cross-vpc.yaml,
# converged by infra/deploy_base_stack.py alongside the home one and peered to it), read by SSM
# parameter name and resolved in the cross account at deploy time, as the home targets are.
CROSS_ACCOUNT_ID = os.environ.get("CATS_INTEG_CROSS_ACCOUNT_ID", "")
CROSS_ACCOUNT_DEPLOY_ROLE = "CATS-integ-deploy-role"
CROSS_VPC_SSM_PARAM = "/cats/integ-test/cross-vpc-id"
CROSS_SUBNET_SSM_PARAM = "/cats/integ-test/cross-subnet-id"


def _get_current_account() -> str | None:
    try:
        return str(boto3.client("sts", region_name=REGION).get_caller_identity()["Account"])
    except Exception:
        return None


# STS is called only when the operator has opted in by setting CATS_INTEG_ACCOUNT_ID:
# collecting the unit suite must make no AWS call and never walk the IMDS chain.
_CURRENT_ACCOUNT = _get_current_account() if ACCOUNT_ID != "" else None


def _wrong_account_reason() -> str:
    """Name the missing variable when unset, and both accounts when they disagree."""
    if ACCOUNT_ID == "":
        return "Set CATS_INTEG_ACCOUNT_ID to the 12-digit account these tests deploy into"
    return f"Requires AWS credentials for account {ACCOUNT_ID} in {REGION}, currently {_CURRENT_ACCOUNT}"


skip_wrong_account = pytest.mark.skipif(
    ACCOUNT_ID == "" or _CURRENT_ACCOUNT != ACCOUNT_ID,
    reason=_wrong_account_reason(),
)

skip_no_cross_account = pytest.mark.skipif(
    CROSS_ACCOUNT_ID == "",
    reason="Set CATS_INTEG_CROSS_ACCOUNT_ID for cross-account tests",
)

# The cross account's Security Hub delegated administrator, and the Region its hub is enabled in.
# The cross account is a member of an organization whose Security Hub is administered by another
# account; a finding CATS imports into the member must appear in the administrator's hub. That
# hub need not be in REGION: reporting.securityhub.aws_region exists for exactly this case.
SECURITYHUB_ADMIN_ACCOUNT_ID = os.environ.get("CATS_INTEG_SECURITYHUB_ADMIN_ACCOUNT_ID", "")
CROSS_SECURITYHUB_REGION = os.environ.get("CATS_INTEG_CROSS_SECURITYHUB_REGION", "us-east-1")
# The role the cross account may assume in the administrator to read its findings. Purpose-built
# (infra/securityhub-admin-read-role.yaml, securityhub:GetFindings only) rather than
# AWSControlTowerExecution: that role's trust allows sts:AssumeRole alone, and a CI session
# carrying transitive session tags needs sts:TagSession on every hop of a role chain.
SECURITYHUB_ADMIN_ROLE = "CATS-integ-securityhub-read-role"

skip_no_securityhub_admin = pytest.mark.skipif(
    CROSS_ACCOUNT_ID == "" or SECURITYHUB_ADMIN_ACCOUNT_ID == "",
    reason="Set CATS_INTEG_CROSS_ACCOUNT_ID and CATS_INTEG_SECURITYHUB_ADMIN_ACCOUNT_ID for the cross-account Security Hub test",
)
