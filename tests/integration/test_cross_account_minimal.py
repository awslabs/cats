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

"""Integration tests: cross-account minimal config."""

from collections.abc import Generator
from typing import Any

import pytest

from cats.cfn.deploy import get_stack_name
from cats.resources.account_under_test import AccountUnderTest
from tests.integration.constants import CROSS_ACCOUNT_ID, REGION, skip_no_cross_account, skip_wrong_account
from tests.utils import deploy_and_destroy

ACCOUNT_KEYNAME = "xacct_minimal"


@pytest.fixture(scope="module")
def deployed_cross_account_stack(
    tmp_path_factory: pytest.TempPathFactory,
    cross_account_minimal_config: dict[str, Any],
) -> Generator[AccountUnderTest, None, None]:
    """Deploy cross-account stack once for all tests in this module."""
    for _ in deploy_and_destroy(tmp_path_factory, cross_account_minimal_config, ACCOUNT_KEYNAME, REGION, "xacct-minimal"):
        account = AccountUnderTest(ACCOUNT_KEYNAME)
        yield account


@skip_wrong_account
@skip_no_cross_account
def test_cross_account_role_assumed(deployed_cross_account_stack: AccountUnderTest) -> None:
    """Cross-account test role is assumed successfully."""
    # act
    assumed_identity = deployed_cross_account_stack.session.client("sts").get_caller_identity()

    # assert
    assert assumed_identity["Account"] == CROSS_ACCOUNT_ID


@skip_wrong_account
@skip_no_cross_account
def test_cross_account_session_can_describe_stack(deployed_cross_account_stack: AccountUnderTest) -> None:
    """Cross-account session can describe its own CATS stack."""
    # arrange
    stack_name = get_stack_name(deployed_cross_account_stack.cfn_stack_prefix, deployed_cross_account_stack.name)

    # act
    response = deployed_cross_account_stack.session.client("cloudformation").describe_stacks(StackName=stack_name)

    # assert
    assert len(response["Stacks"]) == 1
    assert response["Stacks"][0]["StackStatus"] == "CREATE_COMPLETE"


@skip_wrong_account
@skip_no_cross_account
def test_cross_account_has_no_network_targets(deployed_cross_account_stack: AccountUnderTest) -> None:
    """Cross-account with no network config has no network targets."""
    # assert
    assert deployed_cross_account_stack.available_network_targets == []
