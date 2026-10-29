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

"""Tests for _reject_case_only_collision as applied to keynames, network target names and role attribute names.

IAM role names are case-insensitive, so `prod` and `Prod` render role names IAM treats as equal.
"""

import pytest
from pydantic import ValidationError

from cats.config import AccountSettings, CatsAccountUnderTest, CatsConfig
from tests.utils import make_config


def _error_message(exc_info: pytest.ExceptionInfo[ValidationError]) -> str:
    return str(exc_info.value.errors()[0]["msg"])


def test_keynames_differing_only_by_case_are_rejected() -> None:
    # arrange
    config = make_config(accounts={"prod": "111111111111", "Prod": "222222222222"})
    expected_message = (
        "Value error, accounts_under_test keynames 'prod' and 'Prod' differ only by case; "
        + "IAM role names are case-insensitive, so they would collide at deploy."
    )

    # act
    with pytest.raises(ValidationError) as exc_info:
        CatsConfig.model_validate(config)

    # assert
    assert _error_message(exc_info) == expected_message


def test_distinct_keynames_are_accepted() -> None:
    # arrange
    config = make_config(accounts={"prod": "111111111111", "dev": "222222222222"})

    # act
    loaded = CatsConfig.model_validate(config)

    # assert
    assert sorted(loaded.accounts_under_test) == ["dev", "prod"]


def test_target_names_differing_only_by_case_are_rejected() -> None:
    # arrange
    targets = {
        "private": {"vpc_id": "vpc-1", "subnet_ids": ["subnet-1"]},
        "Private": {"vpc_id": "vpc-1", "subnet_ids": ["subnet-2"]},
    }
    expected_message = (
        "Value error, network target names 'private' and 'Private' differ only by case; "
        + "IAM role names are case-insensitive, so they would collide at deploy."
    )

    # act
    with pytest.raises(ValidationError) as exc_info:
        CatsAccountUnderTest.model_validate({"account_id": "111111111111", "account_network_under_test": targets})

    # assert
    assert _error_message(exc_info) == expected_message


def test_role_names_differing_only_by_case_are_rejected() -> None:
    # arrange
    roles = {
        "readonly": {"managed_policies": ["ReadOnlyAccess"]},
        "ReadOnly": {"managed_policies": ["ReadOnlyAccess"]},
    }
    expected_message = (
        "Value error, additional role names 'readonly' and 'ReadOnly' would collide at deploy: IAM role names are case-insensitive "
        + "and the CloudFormation logical ID drops '_', so two names must differ by more than case and underscores."
    )

    # act
    with pytest.raises(ValidationError) as exc_info:
        AccountSettings(additional_roles=roles)  # type: ignore[arg-type]

    # assert
    assert _error_message(exc_info) == expected_message


def test_role_names_differing_only_by_underscores_are_rejected() -> None:
    # arrange
    roles = {
        "read_only": {"managed_policies": ["ReadOnlyAccess"]},
        "readonly": {"managed_policies": ["ReadOnlyAccess"]},
    }
    expected_message = (
        "Value error, additional role names 'read_only' and 'readonly' would collide at deploy: IAM role names are case-insensitive "
        + "and the CloudFormation logical ID drops '_', so two names must differ by more than case and underscores."
    )

    # act
    with pytest.raises(ValidationError) as exc_info:
        AccountSettings(additional_roles=roles)  # type: ignore[arg-type]

    # assert
    assert _error_message(exc_info) == expected_message
