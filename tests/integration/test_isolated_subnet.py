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

"""Integration tests: a listener in a subnet with no route to the SSM endpoints.

An isolated data-tier subnet has an empty route table, so its CATS listener never
registers with SSM. It is still a target for probes from routed subnets, and the Lambda handler is
the probe from it. Deploy must gate that listener's readiness without SSM, and the first probe to
it must find the listener bound. The base stack's isolated subnet models this with a network ACL
that drops TCP 443 to the endpoint subnet (tests/integration/infra/integ-test-vpc.yaml).
"""

from collections.abc import Generator
from contextlib import closing
from typing import Any

import boto3
import pytest

from cats.cfn.deploy import get_stack_name, get_stack_status
from cats.constants import CatsNetworkResponse
from cats.exceptions import CatsSSMError
from cats.resources.account_under_test import AccountUnderTest
from cats.resources.network_interact import SSM_REGISTRATION_TIMEOUT_SECONDS
from tests.integration.constants import REGION, skip_wrong_account, stack_prefix
from tests.utils import deploy_and_destroy

ACCOUNT_KEYNAME = "isolatedsubnet"
PORT = 8080


@pytest.fixture(scope="module")
def deployed_isolated_stack(
    tmp_path_factory: pytest.TempPathFactory,
    isolated_config: dict[str, Any],
) -> Generator[AccountUnderTest, None, None]:
    """Deploy one routed and one isolated listener. The deploy itself is the first assertion: it fails if the gate needs SSM."""
    with closing(deploy_and_destroy(tmp_path_factory, isolated_config, ACCOUNT_KEYNAME, REGION, "isolatedsubnet")) as deployed:
        next(deployed)
        yield AccountUnderTest(ACCOUNT_KEYNAME)


@skip_wrong_account
def test_deploy_completes_with_a_listener_that_cannot_reach_ssm(deployed_isolated_stack: AccountUnderTest) -> None:
    """The readiness gate reads the isolated listener's serial console, so deploy returns success for it."""
    # arrange
    session = boto3.Session(region_name=REGION)
    stack_name = get_stack_name(stack_prefix("iso"), ACCOUNT_KEYNAME)

    # act
    status = get_stack_status(session, stack_name, REGION)

    # assert
    assert (status, sorted(deployed_isolated_stack.available_network_targets)) == ("CREATE_COMPLETE", ["isolated", "routed"])


@skip_wrong_account
def test_routed_ec2_probe_reaches_the_isolated_listener(deployed_isolated_stack: AccountUnderTest) -> None:
    """The isolated listener is bound when deploy returns: a probe to it succeeds over the VPC's local route.

    Test order is not guaranteed, so this may or may not be the first probe after deploy; the
    deploy gate is what makes either case safe.
    """
    # act
    result = deployed_isolated_stack.routed.ec2_tcp_call(deployed_isolated_stack.isolated.ec2_dns_name, PORT)

    # assert
    assert result == CatsNetworkResponse.SUCCESS


@skip_wrong_account
def test_lambda_probe_from_the_isolated_subnet_reaches_the_routed_listener(deployed_isolated_stack: AccountUnderTest) -> None:
    """Probing from an isolated target works through Lambda, whose invocation does not travel the subnet's routes."""
    # act
    result = deployed_isolated_stack.isolated.lambda_tcp_call(deployed_isolated_stack.routed.ec2_dns_name, PORT)

    # assert
    assert result == CatsNetworkResponse.SUCCESS


@skip_wrong_account
def test_ec2_probe_from_the_isolated_subnet_raises_the_registration_error(deployed_isolated_stack: AccountUnderTest) -> None:
    """The one thing an isolated listener cannot be is the EC2 handler's source; the error says so and names the Lambda handler."""
    # arrange
    instance_id = deployed_isolated_stack.isolated._deployed.ec2.instance_id
    expected_message = (
        f"EC2 instance {instance_id} not registered with SSM after {SSM_REGISTRATION_TIMEOUT_SECONDS}s. "
        + "Either the instance is stopped or terminated, or its subnet gives it no path to the SSM services "
        + "(ssm, ssmmessages, ec2messages): the listener has no public IPv4 address, so an internet gateway carries no IPv4 "
        + "traffic for it, and it needs NAT, a route to your organization's endpoints, or interface endpoints in its VPC. "
        + "Without that path the EC2 handler cannot be the source for this target; use the Lambda handler as the source: "
        + "enable it (enabled_network_handlers: [lambda, ec2]) or, where it is enabled, call lambda_<protocol>_call. "
        + "The listener remains a target."
    )

    # act
    with pytest.raises(CatsSSMError) as excinfo:
        deployed_isolated_stack.isolated.ec2_tcp_call(deployed_isolated_stack.routed.ec2_dns_name, PORT)

    # assert
    assert str(excinfo.value) == expected_message
