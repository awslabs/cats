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

"""Integration tests: true cross-account networking (EC2 in account A calls EC2 in account B).

The two listeners are in the home and cross base stacks' VPCs, peered by those stacks
(infra/integ-test-vpc.yaml owns the peering, infra/integ-test-cross-vpc.yaml routes back) and
converged by infra/deploy_base_stack.py before this suite runs. Every probe here crosses that
peering by the target's private DNS name, so a failure is the peering, its routes or its DNS
resolution option, not CATS's own stack.
"""

from collections.abc import Generator
from contextlib import closing
from typing import Any

import pytest

from cats.constants import CatsNetworkResponse
from cats.resources.account_under_test import AccountUnderTest
from tests.integration.constants import REGION, skip_no_cross_account, skip_wrong_account
from tests.integration.transport_helpers import assert_cli_transport, assert_mcp_transport
from tests.utils import deploy_and_destroy


@pytest.fixture(scope="module")
def deployed_both_accounts(
    tmp_path_factory: pytest.TempPathFactory,
    cross_account_networking_config: dict[str, Any],
) -> Generator[tuple[AccountUnderTest, AccountUnderTest], None, None]:
    """Deploy EC2 to both accounts, yield both AccountUnderTest objects."""
    # closing() destroys the stacks the moment this fixture tears down, even if the body raises
    with closing(
        deploy_and_destroy(
            tmp_path_factory,
            cross_account_networking_config,
            ["home", "foreign"],
            REGION,
            "xnet",
            deploy_all=True,
        )
    ) as deployed:
        next(deployed)
        home = AccountUnderTest("home")
        foreign = AccountUnderTest("foreign")
        yield home, foreign


@skip_wrong_account
@skip_no_cross_account
def test_home_ec2_can_reach_foreign_ec2_tcp(
    deployed_both_accounts: tuple[AccountUnderTest, AccountUnderTest],
) -> None:
    """EC2 in home account can make TCP call to EC2 in foreign account."""
    # arrange
    home, foreign = deployed_both_accounts
    foreign_ec2_dns = foreign.subnet.ec2_dns_name

    # act
    result = home.subnet.ec2_tcp_call(foreign_ec2_dns, 8080)

    # assert
    assert result == CatsNetworkResponse.SUCCESS


@skip_wrong_account
@skip_no_cross_account
def test_foreign_ec2_can_reach_home_ec2_tcp(
    deployed_both_accounts: tuple[AccountUnderTest, AccountUnderTest],
) -> None:
    """EC2 in foreign account can make TCP call to EC2 in home account."""
    # arrange
    home, foreign = deployed_both_accounts
    home_ec2_dns = home.subnet.ec2_dns_name

    # act
    result = foreign.subnet.ec2_tcp_call(home_ec2_dns, 8080)

    # assert
    assert result == CatsNetworkResponse.SUCCESS


@skip_wrong_account
@skip_no_cross_account
def test_home_ec2_can_reach_foreign_ec2_http(
    deployed_both_accounts: tuple[AccountUnderTest, AccountUnderTest],
) -> None:
    """EC2 in home account can make HTTP call to EC2 in foreign account."""
    # arrange
    home, foreign = deployed_both_accounts
    foreign_ec2_dns = foreign.subnet.ec2_dns_name

    # act
    result = home.subnet.ec2_http_call(foreign_ec2_dns, 8080)

    # assert
    assert result == CatsNetworkResponse.SUCCESS


@skip_wrong_account
@skip_no_cross_account
def test_cross_account_udp_with_s3_receipt(
    deployed_both_accounts: tuple[AccountUnderTest, AccountUnderTest],
) -> None:
    """UDP from home EC2 to foreign EC2 creates S3 receipt in foreign account."""
    # arrange
    home, foreign = deployed_both_accounts
    foreign_ec2_dns = foreign.subnet.ec2_dns_name

    # act
    sent = home.subnet.ec2_udp_call(foreign_ec2_dns, 5000)
    arrived = foreign.subnet.receive_s3(sent, timeout=15)

    # assert
    assert sent == CatsNetworkResponse.SENT
    assert arrived is True


@skip_wrong_account
@skip_no_cross_account
def test_cli_run_transport_reaches_foreign_ec2(
    deployed_both_accounts: tuple[AccountUnderTest, AccountUnderTest],
) -> None:
    """`cats run` http-call from home's subnet reaches foreign's EC2 listener."""
    home, foreign = deployed_both_accounts
    assert_cli_transport(home, target="subnet", open_port=8080, host=foreign.subnet.ec2_dns_name)


@skip_wrong_account
@skip_no_cross_account
def test_mcp_tools_reach_foreign_ec2(
    deployed_both_accounts: tuple[AccountUnderTest, AccountUnderTest],
) -> None:
    """MCP http_call from home's subnet reaches foreign's EC2 listener."""
    home, foreign = deployed_both_accounts
    assert_mcp_transport(home, target="subnet", open_port=8080, host=foreign.subnet.ec2_dns_name)
