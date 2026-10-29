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

"""Integration tests: the VPC endpoints CATS creates, in a VPC with no route anywhere.

Every other module deploys into the base stack's VPC and uses the endpoints it has, so none of
them renders an AWS::EC2::VPCEndpoint. This module creates a VPC per run (infra/endpoint-test-vpc.yaml:
one subnet, a route table with no routes, a group admitting 443 from the VPC) and has CATS create
the EC2 handler's endpoints in it: ssm, ssmmessages and ec2messages as interface endpoints on that
group, S3 as a gateway endpoint. The listener's only path to any AWS service runs through them, so
one probe proves the endpoints, their account-scoped policy and the `interface_endpoint_security_group_ids`
override together, and an anonymous S3 request from the same listener proves the policy denies
what it should.

Per run rather than a second persistent VPC because private DNS allows one interface endpoint per
service per VPC: two pipelines' CATS stacks could not share one. The account's VPC quota (five per
Region by default) bounds how many pipelines can run this module at once; the failure is an
explicit VpcLimitExceeded when the per-run stack is created.
"""

import json
from collections.abc import Generator
from contextlib import closing
from typing import TYPE_CHECKING, Any

import boto3
import pytest

from cats.cfn.deploy import get_stack_name, get_stack_status
from cats.constants import CatsNetworkResponse
from cats.resources.account_under_test import AccountUnderTest
from tests.integration.constants import ACCOUNT_ID, ENDPOINT_TEST_PARAMETER_PREFIX, REGION, skip_wrong_account, stack_prefix
from tests.integration.vpc_helpers import per_run_vpc
from tests.utils import deploy_and_destroy

if TYPE_CHECKING:
    from mypy_boto3_ec2.type_defs import VpcEndpointTypeDef

ACCOUNT_KEYNAME = "catsendpoints"
STACK_PREFIX = stack_prefix("vpce")
VPC_STACK_NAME = f"{STACK_PREFIX}network"
PORT = 8080
UDP_PORT = 5000

# The logical ids stack.yaml.j2 gives the endpoints of the config's one VPC.
GATEWAY_ENDPOINT = "GatewayEndpointS3Vpc1"
INTERFACE_ENDPOINTS = ("InterfaceEndpointEc2messagesVpc1", "InterfaceEndpointSsmVpc1", "InterfaceEndpointSsmmessagesVpc1")


@pytest.fixture(scope="module")
def endpoint_test_vpc() -> Generator[dict[str, str], None, None]:
    """The per-run VPC, created before the CATS stack and deleted after it: its stack outputs by key."""
    with per_run_vpc(VPC_STACK_NAME, ENDPOINT_TEST_PARAMETER_PREFIX, REGION) as outputs:
        yield outputs


@pytest.fixture(scope="module")
def deployed_endpoint_stack(
    tmp_path_factory: pytest.TempPathFactory,
    endpoint_test_vpc: dict[str, str],
    cats_endpoints_config: dict[str, Any],
) -> Generator[AccountUnderTest, None, None]:
    """Deploy CATS into the per-run VPC. The deploy is the first assertion: every endpoint accepted the account-scoped policy."""
    # closing() destroys the CATS stack the moment this fixture tears down, before the VPC fixture deletes the VPC
    with closing(deploy_and_destroy(tmp_path_factory, cats_endpoints_config, ACCOUNT_KEYNAME, REGION, "catsendpoints")) as deployed:
        next(deployed)
        account = AccountUnderTest(ACCOUNT_KEYNAME)
        print(f"[fixture] EC2 DNS: {account.private.ec2_dns_name}")
        yield account


def _cats_endpoints() -> dict[str, "VpcEndpointTypeDef"]:
    """The CATS stack's VPC endpoints as EC2 describes them, keyed by CloudFormation logical id."""
    session = boto3.Session(region_name=REGION)
    stack_name = get_stack_name(STACK_PREFIX, ACCOUNT_KEYNAME)
    resources = session.client("cloudformation").describe_stack_resources(StackName=stack_name)["StackResources"]
    logical_ids = {r["PhysicalResourceId"]: r["LogicalResourceId"] for r in resources if r["ResourceType"] == "AWS::EC2::VPCEndpoint"}
    endpoints = session.client("ec2").describe_vpc_endpoints(VpcEndpointIds=list(logical_ids))["VpcEndpoints"]
    return {logical_ids[endpoint["VpcEndpointId"]]: endpoint for endpoint in endpoints}


@skip_wrong_account
def test_deploy_completes_with_the_four_endpoints(deployed_endpoint_stack: AccountUnderTest) -> None:
    """CloudFormation accepted the policy on every endpoint the EC2 handler needs: the stack is complete and all four exist."""
    # arrange
    session = boto3.Session(region_name=REGION)
    stack_name = get_stack_name(STACK_PREFIX, ACCOUNT_KEYNAME)
    expected_logical_ids = [
        "GatewayEndpointS3Vpc1",
        "InterfaceEndpointEc2messagesVpc1",
        "InterfaceEndpointSsmVpc1",
        "InterfaceEndpointSsmmessagesVpc1",
    ]

    # act
    status = get_stack_status(session, stack_name, REGION)
    endpoints = _cats_endpoints()

    # assert
    assert ("CREATE_COMPLETE", expected_logical_ids) == (status, sorted(endpoints))


@skip_wrong_account
def test_every_endpoint_carries_the_account_scoped_policy(deployed_endpoint_stack: AccountUnderTest) -> None:
    """Each deployed endpoint's policy is the one the template renders: any action by any principal in the account under test, nothing else."""
    # arrange
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": "*",
                "Action": "*",
                "Resource": "*",
                "Condition": {"StringEquals": {"aws:PrincipalAccount": ACCOUNT_ID}},
            }
        ],
    }
    expected = {logical_id: policy for logical_id in (GATEWAY_ENDPOINT, *INTERFACE_ENDPOINTS)}

    # act
    policies = {logical_id: json.loads(endpoint["PolicyDocument"]) for logical_id, endpoint in _cats_endpoints().items()}

    # assert
    assert expected == policies


@skip_wrong_account
def test_interface_endpoints_carry_the_group_named_by_ssm_parameter(
    deployed_endpoint_stack: AccountUnderTest,
    endpoint_test_vpc: dict[str, str],
) -> None:
    """The group `interface_endpoint_security_group_ids` names by SSM parameter is on every interface endpoint; the gateway endpoint has none."""
    # arrange
    endpoint_group = endpoint_test_vpc["EndpointSecurityGroupId"]
    expected: dict[str, list[str]] = {GATEWAY_ENDPOINT: []} | {logical_id: [endpoint_group] for logical_id in INTERFACE_ENDPOINTS}

    # act
    groups = {logical_id: [group["GroupId"] for group in endpoint.get("Groups", [])] for logical_id, endpoint in _cats_endpoints().items()}

    # assert
    assert expected == groups


@skip_wrong_account
def test_ec2_probe_runs_through_the_cats_created_ssm_endpoints(deployed_endpoint_stack: AccountUnderTest) -> None:
    """The listener registered with SSM and ran a probe, and its subnet's only path to SSM is the three endpoints CATS created.

    So the policy admits the instance role, and the override put the endpoints on a group the
    listener can reach. Unset, the endpoints would carry the VPC's default group, whose only
    inbound rule admits members of that group; the listener is in CATS's own group, so the agent
    would never register and this probe would raise CatsSSMError.
    """
    # act
    result = deployed_endpoint_stack.private.ec2_tcp_call(deployed_endpoint_stack.private.ec2_dns_name, PORT)

    # assert
    assert result == CatsNetworkResponse.SUCCESS


@skip_wrong_account
def test_udp_receipt_is_written_through_the_cats_created_gateway_endpoint(deployed_endpoint_stack: AccountUnderTest) -> None:
    """The listener's receipt write (`aws s3api put-object`, signed with the instance role) passed the gateway endpoint's policy."""
    # act
    sent = deployed_endpoint_stack.private.ec2_udp_call(deployed_endpoint_stack.private.ec2_dns_name, UDP_PORT)
    arrived = deployed_endpoint_stack.private.receive_s3(sent, timeout=15)

    # assert
    assert sent == CatsNetworkResponse.SENT
    assert arrived is True


@skip_wrong_account
def test_anonymous_s3_request_through_the_gateway_endpoint_is_denied(deployed_endpoint_stack: AccountUnderTest) -> None:
    """An unsigned request carries no aws:PrincipalAccount, so the gateway endpoint's policy denies it: S3 answers 403.

    The object is the public one the Systems Manager docs have you fetch to read the agent
    version; through an endpoint with AWS's default policy it is 200. The probes above prove the
    policy admits CATS; this is the assertion that it refuses anything.
    """
    # arrange
    url = f"https://amazon-ssm-{REGION}.s3.{REGION}.amazonaws.com/latest/VERSION"
    command = [
        "python3 - <<'CATSENDPOINTS'",
        "import urllib.error, urllib.request",
        "try:",
        f"    print(urllib.request.urlopen('{url}', timeout=20).status)",
        "except urllib.error.HTTPError as error:",
        "    print(error.code)",
        "CATSENDPOINTS",
    ]

    # act
    result = deployed_endpoint_stack.private.shell(command)

    # assert
    assert (result.exit_code, result.stdout) == (0, "403"), result.stderr
