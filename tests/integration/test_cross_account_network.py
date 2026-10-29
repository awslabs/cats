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

"""Integration tests: cross-account network config.

One listener in the cross base stack's VPC (infra/integ-test-cross-vpc.yaml), probing itself: the
deploy role, SSM parameter resolution in the cross account and the EC2 handler there, with
nothing crossing the peering. test_cross_account_networking.py is the one that crosses it.
"""

from collections.abc import Generator
from typing import Any

import pytest

from cats.constants import CatsNetworkResponse
from cats.resources.account_under_test import AccountUnderTest
from tests.integration.constants import REGION, skip_no_cross_account, skip_wrong_account
from tests.utils import deploy_and_destroy

ACCOUNT_KEYNAME = "xacct_network"


@pytest.fixture(scope="module")
def deployed_cross_account_network_stack(
    tmp_path_factory: pytest.TempPathFactory,
    cross_account_network_config: dict[str, Any],
) -> Generator[AccountUnderTest, None, None]:
    """Deploy cross-account network stack once for all tests in this module."""
    for _ in deploy_and_destroy(tmp_path_factory, cross_account_network_config, ACCOUNT_KEYNAME, REGION, "xacct-network"):
        account = AccountUnderTest(ACCOUNT_KEYNAME)
        yield account


@skip_wrong_account
@skip_no_cross_account
def test_cross_account_tcp_call_succeeds(deployed_cross_account_network_stack: AccountUnderTest) -> None:
    """TCP call to cross-account EC2 returns SUCCESS."""
    # arrange
    account = deployed_cross_account_network_stack
    ec2_dns = account.private.ec2_dns_name

    # act
    result = account.private.ec2_tcp_call(ec2_dns, 8080)

    # assert
    assert result == CatsNetworkResponse.SUCCESS


@skip_wrong_account
@skip_no_cross_account
def test_cross_account_http_call_succeeds(deployed_cross_account_network_stack: AccountUnderTest) -> None:
    """HTTP call to cross-account EC2 returns SUCCESS."""
    # arrange
    account = deployed_cross_account_network_stack
    ec2_dns = account.private.ec2_dns_name

    # act
    result = account.private.ec2_http_call(ec2_dns, 8080)

    # assert
    assert result == CatsNetworkResponse.SUCCESS


@skip_wrong_account
@skip_no_cross_account
def test_cross_account_ec2_dns_name_is_valid(deployed_cross_account_network_stack: AccountUnderTest) -> None:
    """Cross-account EC2 DNS name is populated from CFN outputs."""
    # arrange
    account = deployed_cross_account_network_stack

    # act
    dns_name = account.private.ec2_dns_name

    # assert
    assert dns_name != ""
    assert "." in dns_name
