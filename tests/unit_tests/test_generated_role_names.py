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

"""Tests for generated IAM RoleName preflight validation."""

import re

import pytest
from pydantic import ValidationError

from cats.config import CatsConfig
from tests.utils import make_config

ACCOUNT_ID = "111111111111"


def make_role_name_config(
    *,
    prefix: str,
    account_name: str,
    target_name: str | None = None,
    enabled_handlers: list[str] | None = None,
    existing_ec2_instance_profile_name: str | None = None,
    additional_roles: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    """Build a config whose generated role-name components are controlled by the test."""
    network_targets = (
        None
        if target_name is None
        else {
            target_name: {
                "vpc_id": "vpc-123",
                "subnet_ids": ["subnet-123"],
            }
        }
    )
    return make_config(
        account_name=account_name,
        account_id=ACCOUNT_ID,
        network_targets=network_targets,
        enabled_network_handlers=enabled_handlers,
        additional_roles=additional_roles,
        cfn_stack_prefix=prefix,
        existing_ec2_instance_profile_name=existing_ec2_instance_profile_name,
    )


def assert_role_name_rejected(config: dict[str, object], expected_role_name: str) -> None:
    """Assert validation reports the exact generated name and measured length."""
    expected_message = (
        f"Generated IAM role name '{expected_role_name}' is {len(expected_role_name)} characters; " + "IAM RoleName has a maximum of 64"
    )

    with pytest.raises(ValidationError, match=re.escape(expected_message)):
        CatsConfig.model_validate(config)


def test_default_test_role_over_64_characters_rejected() -> None:
    # arrange
    prefix = "p" * 30
    account_name = "a" * 20
    config = make_role_name_config(prefix=prefix, account_name=account_name)
    expected_role_name = f"{prefix}{account_name}-default-test-role"

    # act / assert
    assert_role_name_rejected(config, expected_role_name)


def test_additional_role_over_64_characters_rejected_after_global_inheritance() -> None:
    # arrange
    prefix = "p" * 10
    account_name = "account"
    role_name = "r" * 48
    config = make_role_name_config(
        prefix=prefix,
        account_name=account_name,
        additional_roles={role_name: {"managed_policies": ["ReadOnlyAccess"]}},
    )
    expected_role_name = f"{prefix}{account_name}-{role_name}"

    # act / assert
    assert_role_name_rejected(config, expected_role_name)


def test_generated_role_names_at_64_characters_accepted() -> None:
    """The default test role is the longest generated role name; at exactly 64 the stack's every role, the shared
    `-ec2` and `-lambda` roles included, is creatable."""
    # arrange
    prefix = "p" * 25
    account_name = "a" * 21
    config = make_role_name_config(
        prefix=prefix,
        account_name=account_name,
        target_name="t",
        enabled_handlers=["ec2", "lambda"],
    )
    assert len(f"{prefix}{account_name}-default-test-role") == 64

    # act
    result = CatsConfig.model_validate(config)

    # assert
    assert result.accounts_under_test.keys() == {account_name}


def test_lambda_function_name_over_64_characters_rejected() -> None:
    # arrange
    prefix = "p" * 23
    account_name = "a" * 20
    target_name = "t" * 15
    config = make_role_name_config(
        prefix=prefix,
        account_name=account_name,
        target_name=target_name,
        enabled_handlers=["lambda"],
        existing_ec2_instance_profile_name="existing-profile",
    )
    expected_function_name = f"{prefix}{account_name}-probe-{target_name}"
    expected_message = (
        f"Generated Lambda function name '{expected_function_name}' is {len(expected_function_name)} characters; "
        + "Lambda FunctionName has a maximum of 64"
    )

    # act / assert
    with pytest.raises(ValidationError, match=re.escape(expected_message)):
        CatsConfig.model_validate(config)
