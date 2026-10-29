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

"""Integration test: max template size with 3 targets using ec2+lambda."""

from collections.abc import Generator
from typing import Any

import boto3
import pytest

from cats.cfn.deploy import get_stack_name
from cats.resources.account_under_test import AccountUnderTest
from cats.version import __version__
from tests.integration.constants import REGION, skip_wrong_account
from tests.utils import deploy_and_destroy

ACCOUNT_KEYNAME = "maxsize"


@pytest.fixture(scope="module")
def deployed_max_size_stack(
    tmp_path_factory: pytest.TempPathFactory,
    max_size_config: dict[str, Any],
) -> Generator[AccountUnderTest, None, None]:
    """Deploy stack with 3 targets using both ec2+lambda handlers."""
    for _ in deploy_and_destroy(tmp_path_factory, max_size_config, ACCOUNT_KEYNAME, REGION, "maxsize"):
        account = AccountUnderTest(ACCOUNT_KEYNAME)
        yield account


@skip_wrong_account
def test_max_size_stack_deploys_successfully(deployed_max_size_stack: AccountUnderTest) -> None:
    """Stack with 3 targets and ec2+lambda deploys without exceeding 51KB limit."""
    # act
    targets = deployed_max_size_stack.available_network_targets

    # assert
    expected_targets = ["target0", "target1", "target2"]
    assert targets == expected_targets
    for target_name in expected_targets:
        handler = getattr(deployed_max_size_stack, target_name)
        assert handler._deployed.enabled_handlers == ["ec2", "lambda"]
        assert handler._deployed.lambda_ is not None
        assert isinstance(handler._deployed.ec2.instance_id, str)
        assert len(handler._deployed.ec2.instance_id) > 0


@skip_wrong_account
def test_max_size_stack_has_cats_tags(deployed_max_size_stack: AccountUnderTest) -> None:
    """Stack with 3 targets has correct CATS tags."""
    # arrange
    session = boto3.Session(region_name=REGION)
    cfn = session.client("cloudformation", region_name=REGION)
    stack_name = get_stack_name(deployed_max_size_stack.cfn_stack_prefix, deployed_max_size_stack.name)
    expected_tags = {
        "cats:managed": "true",
        "cats:account-keyname": ACCOUNT_KEYNAME,
        "cats:version": __version__,
    }

    # act
    response = cfn.describe_stacks(StackName=stack_name)
    tags = {t["Key"]: t["Value"] for t in response["Stacks"][0].get("Tags", [])}

    # assert
    assert tags == expected_tags
