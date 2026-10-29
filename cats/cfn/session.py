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

"""Session management for CloudFormation operations."""

import logging
from functools import cache

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from cats.config import Settings
from cats.exceptions import CatsConfigError, CatsInfrastructureError
from cats.user_agent import attributed_session

logger = logging.getLogger(__name__)

# The RoleSessionName of every role `cats infra` assumes: the deploy role, and the test roles its
# readiness gate probes (cats/cfn/readiness.py). Test runs use their own (cats/resources/session_interact.py),
# so CloudTrail tells a deploy's calls from a test's.
DEPLOY_ROLE_SESSION_NAME = "cats-infra"

BOTO_RETRY_CONFIG = Config(retries={"max_attempts": 10, "mode": "adaptive"})


@cache
def get_current_account_id() -> str:
    """Get the account ID from the initial credential chain.

    Use this to check if we're running in the same account we started.
    Cached to avoid repeated STS calls.

    Raises:
        CatsInfrastructureError: The environment holds no usable AWS identity (no credentials,
            no Region, no endpoint). Every `cats infra` command reaches here first, so this
            is where a missing identity becomes a diagnostic rather than a traceback.
    """
    try:
        return attributed_session().client("sts").get_caller_identity()["Account"]
    except BotoCoreError as e:
        raise CatsInfrastructureError(
            f"Cannot reach AWS to read the caller identity: {str(e).rstrip('.')}. "
            + "The environment CATS runs in needs a working AWS identity (a profile, SSO, or environment variables) and a Region."
        ) from e


def get_deploy_session(account_name: str) -> boto3.Session:
    """Get boto3 session for an account, assuming role if needed.

    If the target account is the starting account and
    cats_deployment_settings.allow_deploy_to_current_account is True,
    returns current session from credential chain.

    Otherwise, assumes the deploy_role_name in the target account.
    """
    account_id = Settings().accounts_under_test[account_name].account_id

    if account_id == get_current_account_id():
        if Settings().cats_deployment_settings.allow_deploy_to_current_account is False:
            raise CatsConfigError(
                f"Deploying to current account {account_id} blocked. " + "Set allow_deploy_to_current_account: true in cats_deployment_settings."
            )
        logger.info("Session used in initial account")
        return attributed_session(region_name=Settings().require_aws_region())

    role_name = Settings().cats_deployment_settings.deploy_role_name
    if role_name is None:
        raise CatsConfigError(f"Target account {account_id} requires role assumption, " + "but no deploy_role_name configured")

    role_arn = f"arn:{Settings().cats_deployment_settings.partition}:iam::{account_id}:role/{role_name}"

    sts = attributed_session().client("sts")
    try:
        response = sts.assume_role(RoleArn=role_arn, RoleSessionName=DEPLOY_ROLE_SESSION_NAME)
    except ClientError as e:
        raise CatsInfrastructureError(
            f"Failed to assume deploy role '{role_name}' in account {account_id} ({account_name}). "
            + "This role (set via deploy_role_name) lets CATS create test infrastructure. "
            + "Check: 1) You have valid AWS credentials (run 'aws sts get-caller-identity'), "
            + f"2) Role '{role_name}' exists in account {account_id}, "
            + "3) Its trust policy allows your identity to assume it. "
            + f"AWS error: {e.response['Error']['Code']}"
        ) from e
    creds = response["Credentials"]

    return attributed_session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=Settings().require_aws_region(),
    )
