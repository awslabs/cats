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

"""Integration tests: Lambda handler and per-target network settings override.

These tests cover same-account Lambda scenarios. Cross-account Lambda invocation has
additional IAM trust requirements and is not exercised here.
"""

from collections.abc import Generator
from contextlib import closing
from typing import Any

import pytest

from cats.constants import CatsNetworkResponse
from cats.resources.account_under_test import AccountUnderTest
from tests.integration.constants import REGION, skip_wrong_account
from tests.integration.transport_helpers import assert_cli_transport, assert_mcp_transport
from tests.utils import deploy_and_destroy

ACCOUNT_KEYNAME = "lambdahandler"


@pytest.fixture(scope="module")
def deployed_lambda_stack(
    tmp_path_factory: pytest.TempPathFactory,
    lambda_config: dict[str, Any],
) -> Generator[AccountUnderTest, None, None]:
    """Deploy stack with per-target handler overrides."""
    # closing() destroys the stack the moment this fixture tears down, even if the body raises
    with closing(deploy_and_destroy(tmp_path_factory, lambda_config, ACCOUNT_KEYNAME, REGION, "lambdahandler")) as deployed:
        next(deployed)
        account = AccountUnderTest(ACCOUNT_KEYNAME)
        yield account


# --- Override verification tests ---


@skip_wrong_account
def test_ec2target_has_ec2_handler_only(deployed_lambda_stack: AccountUnderTest) -> None:
    """EC2 target override removes Lambda handler despite global setting."""
    # act
    metadata = deployed_lambda_stack.ec2target._deployed

    # assert
    assert metadata.enabled_handlers == ["ec2"]
    assert metadata.lambda_ is None


@skip_wrong_account
def test_lambdatarget_has_lambda_handler(deployed_lambda_stack: AccountUnderTest) -> None:
    """Lambda target override sets Lambda handler with the single probe function."""
    # act
    metadata = deployed_lambda_stack.lambdatarget._deployed

    # assert
    assert metadata.enabled_handlers == ["lambda"]
    assert metadata.lambda_ is not None
    assert metadata.lambda_.probe_lambda_name.endswith("-probe-lambdatarget")


# --- Lambda handler functional tests ---


@skip_wrong_account
def test_lambda_tcp_call_succeeds(deployed_lambda_stack: AccountUnderTest) -> None:
    """Lambda TCP call to EC2 listener returns SUCCESS."""
    # arrange
    ec2_dns = deployed_lambda_stack.ec2target.ec2_dns_name

    # act
    result = deployed_lambda_stack.lambdatarget.lambda_tcp_call(ec2_dns, 8080)

    # assert
    assert result == CatsNetworkResponse.SUCCESS


@skip_wrong_account
def test_lambda_http_call_succeeds(deployed_lambda_stack: AccountUnderTest) -> None:
    """Lambda HTTP call to EC2 listener returns SUCCESS with the listener's 200 on the result."""
    # arrange
    ec2_dns = deployed_lambda_stack.ec2target.ec2_dns_name

    # act
    result = deployed_lambda_stack.lambdatarget.lambda_http_call(ec2_dns, 8080)

    # assert
    assert result == CatsNetworkResponse.SUCCESS
    assert result.http_status == 200


@skip_wrong_account
def test_lambda_https_call_to_plaintext_listener_is_tls_failure(deployed_lambda_stack: AccountUnderTest) -> None:
    """HTTPS to the plain-HTTP listener: TCP connects, the handshake fails deterministically."""
    # arrange
    ec2_dns = deployed_lambda_stack.ec2target.ec2_dns_name

    # act
    result = deployed_lambda_stack.lambdatarget.lambda_https_call(ec2_dns, 8080)

    # assert
    assert result == CatsNetworkResponse.TLS_FAILURE


@skip_wrong_account
def test_lambda_tcp_call_to_nonexistent_name_is_dns_failure(deployed_lambda_stack: AccountUnderTest) -> None:
    """A name under the RFC 2606 reserved .invalid TLD is guaranteed NXDOMAIN."""
    # act
    result = deployed_lambda_stack.lambdatarget.lambda_tcp_call("cats-nonexistent.invalid", 8080)

    # assert
    assert result == CatsNetworkResponse.DNS_FAILURE


@skip_wrong_account
def test_lambda_http_call_to_nonexistent_name_is_dns_failure(deployed_lambda_stack: AccountUnderTest) -> None:
    """HTTP resolves the same way as TCP, so the same target classifies the same."""
    # act
    result = deployed_lambda_stack.lambdatarget.lambda_http_call("cats-nonexistent.invalid", 8080)

    # assert
    assert result == CatsNetworkResponse.DNS_FAILURE


@skip_wrong_account
def test_lambda_tcp_call_to_unlisted_port_times_out(deployed_lambda_stack: AccountUnderTest) -> None:
    """Lambda TCP call to an unlisted port returns TIMEOUT - the listener's group drops it.

    ec2target uses the CATS-managed security group, whose ingress is scoped to the
    configured listen ports. 9999 is not one, so the packet is dropped before the
    instance sees it. This is the end-to-end proof of the scoped-ingress rendering.
    """
    # arrange
    ec2_dns = deployed_lambda_stack.ec2target.ec2_dns_name

    # act
    result = deployed_lambda_stack.lambdatarget.lambda_tcp_call(ec2_dns, 9999)

    # assert
    assert result == CatsNetworkResponse.TIMEOUT


@skip_wrong_account
def test_lambda_tcp_call_to_blackhole_times_out(deployed_lambda_stack: AccountUnderTest) -> None:
    """Lambda TCP call to an unroutable address returns TIMEOUT."""
    # act - 192.0.2.1 is TEST-NET-1 (RFC 5737): never routable, packets are dropped
    result = deployed_lambda_stack.lambdatarget.lambda_tcp_call("192.0.2.1", 8080)

    # assert
    assert result == CatsNetworkResponse.TIMEOUT


@skip_wrong_account
def test_lambda_tcp_call_to_shared_listener_succeeds(deployed_lambda_stack: AccountUnderTest) -> None:
    """Positive control for the REFUSED tests below: the shared-group listener is up and reachable."""
    # arrange
    shared_dns = deployed_lambda_stack.sharedtarget.ec2_dns_name

    # act
    result = deployed_lambda_stack.lambdatarget.lambda_tcp_call(shared_dns, 8080)

    # assert
    assert result == CatsNetworkResponse.SUCCESS


@skip_wrong_account
def test_lambda_tcp_call_to_admitted_closed_port_refused(deployed_lambda_stack: AccountUnderTest) -> None:
    """Lambda TCP to a port the group admits but nothing listens on returns REFUSED: the path is open, RST came back.

    A Lambda has no self-call path, so REFUSED needs a security group that lets the SYN reach
    the host. The shared handler group admits 9999 from itself for exactly this; the same port
    on ec2target (CATS-managed group) is TIMEOUT, see test_lambda_tcp_call_to_unlisted_port_times_out.
    """
    # arrange
    shared_dns = deployed_lambda_stack.sharedtarget.ec2_dns_name

    # act
    result = deployed_lambda_stack.lambdatarget.lambda_tcp_call(shared_dns, 9999)

    # assert
    assert result == CatsNetworkResponse.REFUSED


@skip_wrong_account
def test_lambda_http_call_to_admitted_closed_port_refused(deployed_lambda_stack: AccountUnderTest) -> None:
    """Lambda HTTP to an admitted closed port returns REFUSED, same as TCP: urllib's wrapper is unwrapped by the probe."""
    # arrange
    shared_dns = deployed_lambda_stack.sharedtarget.ec2_dns_name

    # act
    result = deployed_lambda_stack.lambdatarget.lambda_http_call(shared_dns, 9999)

    # assert
    assert result == CatsNetworkResponse.REFUSED


@skip_wrong_account
def test_lambda_http_call_to_unlisted_port_times_out(deployed_lambda_stack: AccountUnderTest) -> None:
    """Lambda HTTP call to an unlisted port returns TIMEOUT - the listener's group drops it."""
    # arrange
    ec2_dns = deployed_lambda_stack.ec2target.ec2_dns_name

    # act
    result = deployed_lambda_stack.lambdatarget.lambda_http_call(ec2_dns, 9999)

    # assert
    assert result == CatsNetworkResponse.TIMEOUT


@skip_wrong_account
def test_lambda_udp_call_receipt_arrives(deployed_lambda_stack: AccountUnderTest) -> None:
    """Lambda UDP call is SENT and creates an S3 receipt."""
    # arrange
    ec2_dns = deployed_lambda_stack.ec2target.ec2_dns_name

    # act
    sent = deployed_lambda_stack.lambdatarget.lambda_udp_call(ec2_dns, 5000)
    arrived = deployed_lambda_stack.ec2target.receive_s3(sent, timeout=15)

    # assert
    assert sent == CatsNetworkResponse.SENT
    assert arrived is True


@skip_wrong_account
def test_lambda_udp_call_to_nonexistent_name_is_dns_failure(deployed_lambda_stack: AccountUnderTest) -> None:
    """UDP classifies resolver failures on Lambda too; receive_s3 refuses a datagram that never left."""
    # act
    sent = deployed_lambda_stack.lambdatarget.lambda_udp_call("cats-nonexistent.invalid", 5000)

    # assert
    assert sent == CatsNetworkResponse.DNS_FAILURE
    with pytest.raises(ValueError, match="the datagram never left the host"):
        deployed_lambda_stack.ec2target.receive_s3(sent, timeout=1)


# --- EC2 target still works ---


@skip_wrong_account
def test_ec2_tcp_call_succeeds(deployed_lambda_stack: AccountUnderTest) -> None:
    """EC2 target can still make TCP calls."""
    # arrange
    ec2_dns = deployed_lambda_stack.ec2target.ec2_dns_name

    # act
    result = deployed_lambda_stack.ec2target.ec2_tcp_call(ec2_dns, 8080)

    # assert
    assert result == CatsNetworkResponse.SUCCESS


@skip_wrong_account
def test_cli_run_transport_dispatches_lambda(deployed_lambda_stack: AccountUnderTest) -> None:
    """`cats run` http-call against lambdatarget dispatches via Lambda to the EC2 listener."""
    # lambdatarget has lambda-only handlers; ec2_dns_name attr still resolves because
    # both targets deploy an EC2 listener. assert_cli_transport uses getattr(account, target).ec2_dns_name.
    assert_cli_transport(deployed_lambda_stack, target="lambdatarget", open_port=8080)


@skip_wrong_account
def test_mcp_tools_dispatch_lambda(deployed_lambda_stack: AccountUnderTest) -> None:
    """MCP http_call against lambdatarget dispatches via Lambda to the EC2 listener."""
    assert_mcp_transport(deployed_lambda_stack, target="lambdatarget", open_port=8080)
