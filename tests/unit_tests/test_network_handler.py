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

"""Tests for cats.resources.network_interact module - NetworkHandler methods."""

import json
import logging
import re
import uuid
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest
from botocore.exceptions import ClientError
from pytest_mock import MockerFixture

from cats.config import Settings
from cats.constants import CatsNetworkResponse, CatsNetworkResult, DeployedTargetResources, ShellResult
from cats.exceptions import CatsConfigError, CatsLambdaInvocationError, CatsSSMError, CatsUsageError
from cats.resources.network_interact import (
    LAMBDA_ROLE_PROPAGATION_RETRY_INTERVAL_SECONDS,
    SSM_REGISTRATION_POLL_SECONDS,
    SSM_STDERR_LIMIT,
    SSM_STDOUT_LIMIT,
    NetworkHandler,
    register_udp_receipt_token,
)
from tests.utils import (
    make_deployed_ec2,
    make_deployed_lambda,
    make_lambda_error_response,
    make_lambda_response,
    make_ssm_response,
    write_config,
)

PROBE_DOCUMENT_NAME = "CATS-test-infra-test_account-Probe"
PROBE_LAMBDA_NAME = "CATS-probe-lambda"
GENERATED_UUID = uuid.UUID("3f2a1b4c-5d6e-4f70-8192-a3b4c5d6e7f8")
RECEIPT_TOKEN = "0123456789abcdef0123456789abcdef"  # noqa: S105
# Lambda's wording when it cannot yet assume a freshly created execution role; the probe retries it.
LAMBDA_CANNOT_BE_ASSUMED = ClientError(
    {"Error": {"Code": "AccessDeniedException", "Message": "The role defined for the function cannot be assumed by Lambda."}}, "Invoke"
)


def make_handler(
    cats_yaml: Path,
    mock_client: MagicMock,
    enabled_handlers: list[str],
    include_lambda: bool = False,
    allow_shell: bool = False,
    account_id: str = "123456789012",
    response_bucket_name: str = "cats-responses-123456789012",
) -> NetworkHandler:
    """Create NetworkHandler with mocked session.

    `account_id` is the account under test, in the config and on the host; the parent account stays
    123456789012, so pass a different value to tell the two apart.
    """
    write_config(
        cats_yaml,
        {
            "cats_deployment_settings": {
                "parent_account_id": "123456789012",
                "cfn_stack_prefix": "CATS-test-infra-",
            },
            "global_network_settings": {
                "network_call_timeout_seconds": 30,
                "enabled_network_handlers": enabled_handlers,
            },
            "global_account_settings": {
                "allow_shell": allow_shell,
            },
            "accounts_under_test": {
                "test_account": {
                    "account_id": account_id,
                    "account_network_under_test": {
                        "private": {
                            "vpc_id": "vpc-12345",
                            "subnet_ids": ["subnet-111"],
                        }
                    },
                }
            },
        },
    )
    mock_session = MagicMock()
    mock_session.client.return_value = mock_client
    mock_host = MagicMock()
    mock_host.session = mock_session
    mock_host.cfn_stack_prefix = "CATS-test-infra-"
    mock_host.name = "test_account"
    mock_host.account_id = account_id
    account_config = Settings().accounts_under_test["test_account"]
    mock_host._account_config = account_config
    target_config = account_config.account_network_under_test["private"]
    deployed = DeployedTargetResources(
        response_bucket_name=response_bucket_name,
        vpc_id="vpc-12345",
        subnet_id_list=["subnet-111"],
        account_name="test_account",
        target_name="private",
        enabled_handlers=enabled_handlers,
        ec2=make_deployed_ec2(),
        lambda_=make_deployed_lambda() if include_lambda else None,
    )
    return NetworkHandler(mock_host, deployed, target_config)


@pytest.fixture
def mock_ssm() -> MagicMock:
    """Mock SSM client whose send_command succeeds and whose inventory lists no instance.

    Set get_command_invocation per test; set describe_instance_information with _listed() for
    a registered instance.
    """
    mock = MagicMock()
    mock.send_command.return_value = {"Command": {"CommandId": "cmd-123"}}
    mock.describe_instance_information.return_value = {"InstanceInformationList": []}
    return mock


@pytest.fixture
def mock_lambda() -> MagicMock:
    """Mock Lambda client."""
    return MagicMock()


@pytest.fixture
def handler(cats_yaml: Path, mock_ssm: MagicMock) -> NetworkHandler:
    """Create NetworkHandler with EC2 as default handler."""
    return make_handler(cats_yaml, mock_ssm, ["ec2"])


@pytest.fixture
def handler_lambda_first(cats_yaml: Path, mock_lambda: MagicMock) -> NetworkHandler:
    """Create NetworkHandler with Lambda as first handler."""
    return make_handler(cats_yaml, mock_lambda, ["lambda", "ec2"], include_lambda=True)


@pytest.fixture
def fixed_uuid(mocker: MockerFixture) -> uuid.UUID:
    """Pin the generated UDP correlation ID."""
    mocker.patch("uuid.uuid4", return_value=GENERATED_UUID)
    return GENERATED_UUID


@pytest.fixture
def no_sleep(mocker: MockerFixture) -> MagicMock:
    mock: MagicMock = mocker.patch("cats.resources.network_interact.time.sleep")
    return mock


@pytest.fixture
def sleep_advances_clock(mocker: MockerFixture) -> MagicMock:
    """time.time() starts at 0 and moves only when the code under test sleeps."""
    clock = {"now": 0.0}
    mocker.patch("cats.resources.network_interact.time.time", side_effect=lambda: clock["now"])

    def advance(seconds: float) -> None:
        clock["now"] += seconds

    mock: MagicMock = mocker.patch("cats.resources.network_interact.time.sleep", side_effect=advance)
    return mock


def _not_registered() -> ClientError:
    return ClientError({"Error": {"Code": "InvalidInstanceId", "Message": "Instances not in a valid state for account"}}, "SendCommand")


def _listed(ping_status: str) -> dict[str, list[dict[str, str]]]:
    """An SSM inventory response listing the handler's instance with the given PingStatus."""
    return {"InstanceInformationList": [{"InstanceId": "i-1234567890abcdef0", "PingStatus": ping_status}]}


ONLINE_REFUSED_MESSAGE = (
    "EC2 instance i-1234567890abcdef0 is listed in SSM as Online yet refused SendCommand. Consistent causes: "
    + "the caller may not target this instance (the test role's grant requires the instance tag cats:stack to name this stack; "
    + "a policy denying ssm:SendCommand on the instance looks the same), the instance is shutting down, "
    + "or its agent registered moments ago and SendCommand has not caught up yet (retry). "
    + "Recreate it: cats infra destroy test_account && cats infra deploy test_account"
)


def _ssm_params(mock_ssm: MagicMock) -> dict[str, list[str]]:
    params: dict[str, list[str]] = mock_ssm.send_command.call_args.kwargs["Parameters"]
    return params


def _lambda_event(mock_lambda: MagicMock) -> dict[str, object]:
    event: dict[str, object] = json.loads(mock_lambda.invoke.call_args.kwargs["Payload"])
    return event


# =============================================================================
# EC2 transport: protocol per public method, single document
# =============================================================================


@pytest.mark.parametrize(
    ("method_name", "protocol"),
    [("ec2_http_call", "http"), ("ec2_https_call", "https"), ("ec2_tcp_call", "tcp")],
)
def test_ec2_call_sends_protocol_url_and_port_to_the_probe_document(
    handler: NetworkHandler, mock_ssm: MagicMock, method_name: str, protocol: str
) -> None:
    # arrange
    mock_ssm.get_command_invocation.return_value = make_ssm_response("Success", "SUCCESS")
    expected_params = {"Protocol": [protocol], "TargetUrl": ["target.internal"], "TargetPort": ["8080"]}

    # act
    getattr(handler, method_name)("target.internal", 8080)

    # assert
    call_args = mock_ssm.send_command.call_args
    assert (call_args.kwargs["DocumentName"], call_args.kwargs["Parameters"]) == (PROBE_DOCUMENT_NAME, expected_params)


def test_ec2_udp_call_sends_udp_protocol_and_message(handler: NetworkHandler, mock_ssm: MagicMock, fixed_uuid: uuid.UUID) -> None:
    # arrange
    mock_ssm.get_command_invocation.return_value = make_ssm_response("Success", "SENT")
    expected_params = {"Protocol": ["udp"], "TargetUrl": ["target.internal"], "TargetPort": ["5000"], "TargetMessage": [str(fixed_uuid)]}

    # act
    handler.ec2_udp_call("target.internal", 5000)

    # assert
    assert _ssm_params(mock_ssm) == expected_params


def test_ec2_call_returns_network_result(handler: NetworkHandler, mock_ssm: MagicMock) -> None:
    # arrange
    mock_ssm.get_command_invocation.return_value = make_ssm_response("Success", "SUCCESS")

    # act
    result = handler.ec2_tcp_call("target.internal", 443)

    # assert
    assert isinstance(result, CatsNetworkResult)


@pytest.mark.parametrize("response_value", ["REFUSED", "TIMEOUT", "DNS_FAILURE", "TLS_FAILURE", "FAILURE"])
def test_ec2_call_classifies_every_taxonomy_member(handler: NetworkHandler, mock_ssm: MagicMock, response_value: str) -> None:
    # arrange
    mock_ssm.get_command_invocation.return_value = make_ssm_response("Success", response_value, reason="classified by the probe")

    # act
    result = handler.ec2_https_call("target.internal", 443)

    # assert
    assert result == CatsNetworkResponse[response_value]


def test_ec2_call_propagates_reason(handler: NetworkHandler, mock_ssm: MagicMock) -> None:
    # arrange
    mock_ssm.get_command_invocation.return_value = make_ssm_response("Success", "DNS_FAILURE", reason="[Errno -2] Name or service not known")

    # act
    result = handler.ec2_tcp_call("nonexistent.invalid", 443)

    # assert
    assert result.reason == "[Errno -2] Name or service not known"


def test_ec2_http_success_carries_http_status(handler: NetworkHandler, mock_ssm: MagicMock) -> None:
    # arrange
    mock_ssm.get_command_invocation.return_value = make_ssm_response("Success", "SUCCESS", http_status=403)

    # act
    result = handler.ec2_http_call("target.internal", 8080)

    # assert
    assert (result.response, result.reason, result.http_status, result.correlation_id) == (CatsNetworkResponse.SUCCESS, None, 403, None)


def test_ec2_tcp_success_has_no_http_status_or_correlation_id(handler: NetworkHandler, mock_ssm: MagicMock) -> None:
    # arrange
    mock_ssm.get_command_invocation.return_value = make_ssm_response("Success", "SUCCESS")

    # act
    result = handler.ec2_tcp_call("target.internal", 443)

    # assert
    assert (result.reason, result.http_status, result.correlation_id) == (None, None, None)


def test_ec2_error_result_raises_ssm_error_with_reason(handler: NetworkHandler, mock_ssm: MagicMock) -> None:
    # arrange
    mock_ssm.get_command_invocation.return_value = make_ssm_response("Success", "ERROR", reason="No module named socket")
    expected_message = f"Infrastructure error in {PROBE_DOCUMENT_NAME}: No module named socket"

    # act & assert
    with pytest.raises(CatsSSMError) as exc_info:
        handler.ec2_http_call("target.internal", 8080)
    assert str(exc_info.value) == expected_message


def test_ec2_unparseable_stdout_raises_ssm_error(handler: NetworkHandler, mock_ssm: MagicMock) -> None:
    # arrange
    mock_ssm.get_command_invocation.return_value = {
        "Status": "Success",
        "StatusDetails": "Success details",
        "StandardOutputContent": "python3: can't open file\n",
        "StandardErrorContent": "",
    }

    # act & assert
    with pytest.raises(CatsSSMError, match=f"Invalid probe output from {PROBE_DOCUMENT_NAME}"):
        handler.ec2_http_call("target.internal", 8080)


def test_ssm_command_failed_raises_error(handler: NetworkHandler, mock_ssm: MagicMock) -> None:
    # arrange
    mock_ssm.get_command_invocation.return_value = {
        "Status": "Failed",
        "StatusDetails": "connection refused",
        "StandardOutputContent": "",
        "StandardErrorContent": "connection refused",
        "ResponseCode": 2,
    }
    expected_message = f"SSM command Failed: {PROBE_DOCUMENT_NAME} exited 2. stderr: connection refused"

    # act & assert
    with pytest.raises(CatsSSMError) as exc_info:
        handler.ec2_http_call("target.internal", 8080)
    assert str(exc_info.value) == expected_message


def test_ssm_registered_instance_sends_command_once_without_waiting(handler: NetworkHandler, mock_ssm: MagicMock, no_sleep: MagicMock) -> None:
    # arrange
    mock_ssm.get_command_invocation.return_value = make_ssm_response("Success", "SUCCESS")

    # act
    handler.ec2_http_call("target.internal", 8080)

    # assert
    assert (mock_ssm.send_command.call_count, mock_ssm.describe_instance_information.call_count, no_sleep.call_count) == (1, 0, 0)


def test_ssm_instance_registering_during_window_returns_result(handler: NetworkHandler, mock_ssm: MagicMock, no_sleep: MagicMock) -> None:
    # arrange - CREATE_COMPLETE fires at launch; the agent registers a few seconds later
    mock_ssm.send_command.side_effect = [_not_registered(), _not_registered(), {"Command": {"CommandId": "cmd-123"}}]
    mock_ssm.get_command_invocation.return_value = make_ssm_response("Success", "SUCCESS")
    expected_sleeps = [call(SSM_REGISTRATION_POLL_SECONDS), call(SSM_REGISTRATION_POLL_SECONDS)]

    # act
    result = handler.ec2_http_call("target.internal", 8080)

    # assert
    assert (result.response, no_sleep.call_args_list) == (CatsNetworkResponse.SUCCESS, expected_sleeps)


def test_ssm_instance_unregistered_past_window_raises_helpful_error(
    handler: NetworkHandler, mock_ssm: MagicMock, sleep_advances_clock: MagicMock
) -> None:
    # arrange
    mock_ssm.send_command.side_effect = _not_registered()
    expected_message = (
        "EC2 instance i-1234567890abcdef0 not registered with SSM after 90s. "
        + "Either the instance is stopped or terminated, or its subnet gives it no path to the SSM services "
        + "(ssm, ssmmessages, ec2messages): the listener has no public IPv4 address, so an internet gateway carries no IPv4 "
        + "traffic for it, and it needs NAT, a route to your organization's endpoints, or interface endpoints in its VPC. "
        + "Without that path the EC2 handler cannot be the source for this target; use the Lambda handler as the source: "
        + "enable it (enabled_network_handlers: [lambda, ec2]) or, where it is enabled, call lambda_<protocol>_call. "
        + "The listener remains a target."
    )

    # act & assert
    with pytest.raises(CatsSSMError) as exc_info:
        handler.ec2_http_call("target.internal", 8080)
    assert str(exc_info.value) == expected_message


def test_ssm_instance_unregistered_past_window_polls_until_the_deadline(
    handler: NetworkHandler, mock_ssm: MagicMock, sleep_advances_clock: MagicMock
) -> None:
    # arrange - attempts at 0s, 3s, ..., 90s: the attempt at the deadline is the last
    mock_ssm.send_command.side_effect = _not_registered()

    # act
    with pytest.raises(CatsSSMError):
        handler.ec2_http_call("target.internal", 8080)

    # assert
    assert mock_ssm.send_command.call_count == 31


def test_ssm_registration_window_is_paid_once_per_instance(handler: NetworkHandler, mock_ssm: MagicMock, sleep_advances_clock: MagicMock) -> None:
    # arrange - a listener that can never register: the first call spends the window, the second must not
    mock_ssm.send_command.side_effect = _not_registered()
    with pytest.raises(CatsSSMError):
        handler.ec2_http_call("target.internal", 8080)
    attempts_after_first_call = mock_ssm.send_command.call_count
    sleeps_after_first_call = sleep_advances_clock.call_count

    # act
    with pytest.raises(CatsSSMError):
        handler.ec2_http_call("target.internal", 8080)

    # assert
    expected = (attempts_after_first_call + 1, sleeps_after_first_call)
    assert (mock_ssm.send_command.call_count, sleep_advances_clock.call_count) == expected


# =============================================================================
# EC2 Handler - polling the invocation until it is terminal
# =============================================================================


def _invocation_does_not_exist() -> ClientError:
    return ClientError(
        {"Error": {"Code": "InvocationDoesNotExist", "Message": "The command ID and instance ID you specified did not match"}}, "GetCommandInvocation"
    )


@pytest.mark.parametrize("status", ["Pending", "InProgress", "Delayed"])
def test_ssm_non_terminal_status_polls_again_after_one_second(
    handler: NetworkHandler, mock_ssm: MagicMock, sleep_advances_clock: MagicMock, status: str
) -> None:
    # arrange
    mock_ssm.get_command_invocation.side_effect = [make_ssm_response(status, "SUCCESS"), make_ssm_response("Success", "SUCCESS")]

    # act
    result = handler.ec2_http_call("target.internal", 8080)

    # assert
    assert (result.response, mock_ssm.get_command_invocation.call_count, sleep_advances_clock.call_args_list) == (
        CatsNetworkResponse.SUCCESS,
        2,
        [call(1)],
    )


def test_ssm_invocation_not_yet_visible_polls_again(handler: NetworkHandler, mock_ssm: MagicMock, sleep_advances_clock: MagicMock) -> None:
    # arrange - GetCommandInvocation lags SendCommand; the invocation appears on the next poll
    mock_ssm.get_command_invocation.side_effect = [_invocation_does_not_exist(), make_ssm_response("Success", "SUCCESS")]

    # act
    result = handler.ec2_http_call("target.internal", 8080)

    # assert
    assert (result.response, mock_ssm.get_command_invocation.call_count, sleep_advances_clock.call_args_list) == (
        CatsNetworkResponse.SUCCESS,
        2,
        [call(1)],
    )


def test_ssm_other_get_invocation_client_error_propagates(handler: NetworkHandler, mock_ssm: MagicMock, no_sleep: MagicMock) -> None:
    # arrange
    mock_ssm.get_command_invocation.side_effect = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "GetCommandInvocation")

    # act & assert
    with pytest.raises(ClientError):
        handler.ec2_http_call("target.internal", 8080)


def test_ssm_invocation_still_running_at_the_deadline_says_so(handler: NetworkHandler, mock_ssm: MagicMock, sleep_advances_clock: MagicMock) -> None:
    """A command that is InProgress at the deadline is a slow command, not a connectivity failure."""
    # arrange - network_call_timeout_seconds is 30 in the config; the poll gives up 5 s after it
    mock_ssm.get_command_invocation.return_value = make_ssm_response("InProgress", "SUCCESS")
    expected_message = (
        "SSM command was still InProgress after 30s and CATS stopped waiting; it may still be running on the instance. "
        + "Raise network_call_timeout_seconds for commands that legitimately take longer"
    )

    # act
    with pytest.raises(CatsSSMError) as exc_info:
        handler.ec2_http_call("target.internal", 8080)

    # assert - polls at 0s..34s, one per second, then the deadline at 35s ends the loop
    assert (str(exc_info.value), mock_ssm.get_command_invocation.call_count) == (expected_message, 35)


def test_ssm_invocation_never_visible_at_the_deadline_names_connectivity(
    handler: NetworkHandler, mock_ssm: MagicMock, sleep_advances_clock: MagicMock
) -> None:
    """An invocation SSM never created means the command never reached the instance."""
    # arrange
    mock_ssm.get_command_invocation.side_effect = ClientError({"Error": {"Code": "InvocationDoesNotExist", "Message": ""}}, "GetCommandInvocation")
    expected_message = (
        "SSM command was not delivered to the instance within 30s: SSM accepted it, so the instance is registered, "
        + "but its agent did not pick it up. The instance is stopping, or its agent has lost its path to the SSM services "
        + "(NAT, a route to your organization's endpoints, or interface endpoints in its VPC)"
    )

    # act
    with pytest.raises(CatsSSMError) as exc_info:
        handler.ec2_http_call("target.internal", 8080)

    # assert
    assert str(exc_info.value) == expected_message


def test_ssm_invocation_pending_at_the_deadline_names_connectivity(
    handler: NetworkHandler, mock_ssm: MagicMock, sleep_advances_clock: MagicMock
) -> None:
    """Pending for the whole window means the agent never picked the command up."""
    # arrange
    mock_ssm.get_command_invocation.return_value = make_ssm_response("Pending", "")
    expected_message = (
        "SSM command was not delivered to the instance within 30s: SSM accepted it, so the instance is registered, "
        + "but its agent did not pick it up. The instance is stopping, or its agent has lost its path to the SSM services "
        + "(NAT, a route to your organization's endpoints, or interface endpoints in its VPC)"
    )

    # act
    with pytest.raises(CatsSSMError) as exc_info:
        handler.ec2_http_call("target.internal", 8080)

    # assert
    assert str(exc_info.value) == expected_message


def test_ssm_online_instance_refusing_command_raises_permission_error_without_waiting(
    handler: NetworkHandler, mock_ssm: MagicMock, sleep_advances_clock: MagicMock
) -> None:
    # arrange - listed and reachable, so the refusal is not registration
    mock_ssm.send_command.side_effect = _not_registered()
    mock_ssm.describe_instance_information.return_value = _listed("Online")

    # act
    with pytest.raises(CatsSSMError) as exc_info:
        handler.ec2_http_call("target.internal", 8080)

    # assert
    assert (str(exc_info.value), sleep_advances_clock.call_count) == (ONLINE_REFUSED_MESSAGE, 0)


def test_ssm_memoized_instance_now_listed_online_raises_permission_error_not_connectivity(
    handler: NetworkHandler, mock_ssm: MagicMock, sleep_advances_clock: MagicMock
) -> None:
    # arrange - the first call spends the window while unlisted; by the second the instance is listed Online
    mock_ssm.send_command.side_effect = _not_registered()
    with pytest.raises(CatsSSMError):
        handler.ec2_http_call("target.internal", 8080)
    mock_ssm.describe_instance_information.return_value = _listed("Online")

    # act
    with pytest.raises(CatsSSMError) as exc_info:
        handler.ec2_http_call("target.internal", 8080)

    # assert - the inventory is read before the spent window ends the call
    assert str(exc_info.value) == ONLINE_REFUSED_MESSAGE


def test_ssm_disconnected_agent_raises_agent_error_without_waiting(
    handler: NetworkHandler, mock_ssm: MagicMock, sleep_advances_clock: MagicMock
) -> None:
    # arrange
    mock_ssm.send_command.side_effect = _not_registered()
    mock_ssm.describe_instance_information.return_value = _listed("ConnectionLost")
    expected_message = (
        "EC2 instance i-1234567890abcdef0 is registered with SSM but its agent is not connected (PingStatus ConnectionLost): "
        + "SSM Agent has stopped or lost its route to the SSM endpoints. "
        + "Recreate it: cats infra destroy test_account && cats infra deploy test_account"
    )

    # act
    with pytest.raises(CatsSSMError) as exc_info:
        handler.ec2_http_call("target.internal", 8080)

    # assert
    assert (str(exc_info.value), sleep_advances_clock.call_count) == (expected_message, 0)


@pytest.mark.parametrize("inventory_error_code", ["AccessDeniedException", "InvalidInstanceId"])
def test_ssm_unreadable_inventory_falls_back_to_waiting_for_registration(
    handler: NetworkHandler, mock_ssm: MagicMock, no_sleep: MagicMock, inventory_error_code: str
) -> None:
    # arrange - a stack deployed before the inventory grant (AccessDenied), or SSM answering for an unmanaged ID
    mock_ssm.send_command.side_effect = [_not_registered(), {"Command": {"CommandId": "cmd-123"}}]
    mock_ssm.describe_instance_information.side_effect = ClientError(
        {"Error": {"Code": inventory_error_code, "Message": "cannot look"}}, "DescribeInstanceInformation"
    )
    mock_ssm.get_command_invocation.return_value = make_ssm_response("Success", "SUCCESS")

    # act
    result = handler.ec2_http_call("target.internal", 8080)

    # assert
    assert (result.response, no_sleep.call_args_list) == (CatsNetworkResponse.SUCCESS, [call(SSM_REGISTRATION_POLL_SECONDS)])


def test_ssm_other_inventory_error_propagates(handler: NetworkHandler, mock_ssm: MagicMock, sleep_advances_clock: MagicMock) -> None:
    # arrange
    mock_ssm.send_command.side_effect = _not_registered()
    mock_ssm.describe_instance_information.side_effect = ClientError(
        {"Error": {"Code": "InternalServerError", "Message": "boom"}}, "DescribeInstanceInformation"
    )

    # act
    with pytest.raises(ClientError):
        handler.ec2_http_call("target.internal", 8080)

    # assert
    assert sleep_advances_clock.call_count == 0


def test_ssm_other_send_command_error_propagates_without_retry(handler: NetworkHandler, mock_ssm: MagicMock, no_sleep: MagicMock) -> None:
    # arrange
    mock_ssm.send_command.side_effect = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "Denied"}}, "SendCommand")

    # act
    with pytest.raises(ClientError):
        handler.ec2_http_call("target.internal", 8080)

    # assert
    assert (mock_ssm.send_command.call_count, no_sleep.call_count) == (1, 0)


def test_ssm_document_missing_means_a_stack_from_another_release(handler: NetworkHandler, mock_ssm: MagicMock) -> None:
    """With the ec2 handler enabled in the outputs, a missing probe document has one cause left: another release's stack."""
    # arrange
    mock_ssm.send_command.side_effect = ClientError(
        {"Error": {"Code": "InvalidDocument", "Message": "Document not found"}},
        "SendCommand",
    )
    expected_message = (
        f"SSM document '{PROBE_DOCUMENT_NAME}' not found: the stack was deployed by another CATS release. "
        + "Recreate it: cats infra destroy test_account && cats infra deploy test_account"
    )

    # act & assert
    with pytest.raises(CatsSSMError) as exc_info:
        handler.ec2_http_call("target.internal", 8080)
    assert str(exc_info.value) == expected_message


@pytest.mark.parametrize("method_name", ["ec2_http_call", "ec2_https_call", "ec2_tcp_call", "ec2_udp_call"])
def test_ec2_call_raises_before_any_call_when_ec2_not_enabled(cats_yaml: Path, mock_ssm: MagicMock, method_name: str) -> None:
    """The outputs already say which handlers exist; ec2_* on a lambda-only target is a CatsConfigError before any SendCommand."""
    # arrange
    lambda_only = make_handler(cats_yaml, mock_ssm, ["lambda"], include_lambda=True)
    expected_message = "EC2 handler not available. Add 'ec2' to enabled_network_handlers in cats.yaml and redeploy."

    # act
    with pytest.raises(CatsConfigError) as exc_info:
        getattr(lambda_only, method_name)("target.internal", 8080)

    # assert
    assert (str(exc_info.value), mock_ssm.send_command.call_count) == (expected_message, 0)


# =============================================================================
# Lambda transport: protocol per public method, single function, no envelope
# =============================================================================


@pytest.mark.parametrize(
    ("method_name", "protocol"),
    [("lambda_http_call", "http"), ("lambda_https_call", "https"), ("lambda_tcp_call", "tcp")],
)
def test_lambda_call_sends_protocol_url_and_port_to_the_probe_function(
    handler_lambda_first: NetworkHandler, mock_lambda: MagicMock, method_name: str, protocol: str
) -> None:
    # arrange
    mock_lambda.invoke.return_value = make_lambda_response("SUCCESS")
    expected_event = {"PROTOCOL": protocol, "TARGET_URL": "target.internal", "TARGET_PORT": 8080}

    # act
    getattr(handler_lambda_first, method_name)("target.internal", 8080)

    # assert
    call_args = mock_lambda.invoke.call_args
    assert (call_args.kwargs["FunctionName"], call_args.kwargs["InvocationType"], _lambda_event(mock_lambda)) == (
        PROBE_LAMBDA_NAME,
        "RequestResponse",
        expected_event,
    )


def test_lambda_udp_call_sends_udp_protocol_and_message(handler_lambda_first: NetworkHandler, mock_lambda: MagicMock, fixed_uuid: uuid.UUID) -> None:
    # arrange
    mock_lambda.invoke.return_value = make_lambda_response("SENT")
    expected_event = {"PROTOCOL": "udp", "TARGET_URL": "target.internal", "TARGET_PORT": 5000, "TARGET_MESSAGE": str(fixed_uuid)}

    # act
    handler_lambda_first.lambda_udp_call("target.internal", 5000)

    # assert
    assert _lambda_event(mock_lambda) == expected_event


@pytest.mark.parametrize("response_value", ["REFUSED", "TIMEOUT", "DNS_FAILURE", "TLS_FAILURE", "FAILURE"])
def test_lambda_call_classifies_every_taxonomy_member(handler_lambda_first: NetworkHandler, mock_lambda: MagicMock, response_value: str) -> None:
    # arrange
    mock_lambda.invoke.return_value = make_lambda_response(response_value, reason="classified by the probe")

    # act
    result = handler_lambda_first.lambda_https_call("target.internal", 443)

    # assert
    assert result == CatsNetworkResponse[response_value]


def test_lambda_call_propagates_reason(handler_lambda_first: NetworkHandler, mock_lambda: MagicMock) -> None:
    # arrange
    mock_lambda.invoke.return_value = make_lambda_response("TIMEOUT", reason="timed out")

    # act
    result = handler_lambda_first.lambda_tcp_call("192.0.2.1", 443)

    # assert
    assert result.reason == "timed out"


def test_lambda_http_success_carries_http_status(handler_lambda_first: NetworkHandler, mock_lambda: MagicMock) -> None:
    # arrange
    mock_lambda.invoke.return_value = make_lambda_response("SUCCESS", http_status=200)

    # act
    result = handler_lambda_first.lambda_http_call("target.internal", 8080)

    # assert
    assert (result.response, result.reason, result.http_status, result.correlation_id) == (CatsNetworkResponse.SUCCESS, None, 200, None)


def test_lambda_error_result_raises_invocation_error_with_reason(handler_lambda_first: NetworkHandler, mock_lambda: MagicMock) -> None:
    # arrange
    mock_lambda.invoke.return_value = make_lambda_response("ERROR", reason="name 'json' is not defined")
    expected_message = f"Infrastructure error in {PROBE_LAMBDA_NAME}: name 'json' is not defined"

    # act & assert
    with pytest.raises(CatsLambdaInvocationError) as exc_info:
        handler_lambda_first.lambda_tcp_call("target.internal", 443)
    assert str(exc_info.value) == expected_message


def test_lambda_invalid_port_raises_before_any_invoke(handler_lambda_first: NetworkHandler, mock_lambda: MagicMock) -> None:
    # arrange - the deployed probe trusts its caller: the library stops a bad port before any transport
    expected_message = "port must be an integer in 1..65535, got 0"

    # act & assert
    with pytest.raises(CatsUsageError, match=re.escape(expected_message)):
        handler_lambda_first.lambda_tcp_call("target.internal", 0)
    assert mock_lambda.invoke.called is False


def test_ec2_invalid_port_raises_before_any_send_command(handler: NetworkHandler, mock_ssm: MagicMock) -> None:
    # arrange
    expected_message = "port must be an integer in 1..65535, got 65536"

    # act & assert
    with pytest.raises(CatsUsageError, match=re.escape(expected_message)):
        handler.ec2_tcp_call("target.internal", 65536)
    assert mock_ssm.send_command.called is False


def test_lambda_invalid_host_raises_before_any_invoke(handler_lambda_first: NetworkHandler, mock_lambda: MagicMock) -> None:
    # arrange - a URL where a hostname belongs would otherwise be probed and misreported as DNS_FAILURE
    expected_message = (
        "host must be a hostname or IP literal, not a URL or host:port, got 'http://target.internal' "
        + "(the method you call chooses the protocol; pass the port as the second argument)"
    )

    # act & assert
    with pytest.raises(CatsUsageError, match=f"^{re.escape(expected_message)}$"):
        handler_lambda_first.lambda_http_call("http://target.internal", 80)
    assert mock_lambda.invoke.called is False


def test_ec2_invalid_host_raises_before_any_send_command(handler: NetworkHandler, mock_ssm: MagicMock) -> None:
    # arrange
    expected_message = (
        "host must be a hostname or IP literal, not a URL or host:port, got 'target.internal:443' "
        + "(the method you call chooses the protocol; pass the port as the second argument)"
    )

    # act & assert
    with pytest.raises(CatsUsageError, match=f"^{re.escape(expected_message)}$"):
        handler.ec2_tcp_call("target.internal:443", 443)
    assert mock_ssm.send_command.called is False


def test_ec2_bracketed_ipv6_host_is_sent_to_the_probe_bare(handler: NetworkHandler, mock_ssm: MagicMock) -> None:
    # arrange - getaddrinfo and http.client take the literal without the URL brackets
    mock_ssm.get_command_invocation.return_value = make_ssm_response("Success", "SUCCESS")

    # act
    handler.ec2_tcp_call("[fd00::5]", 8080)

    # assert
    expected_params = {"Protocol": ["tcp"], "TargetUrl": ["fd00::5"], "TargetPort": ["8080"]}
    assert mock_ssm.send_command.call_args.kwargs["Parameters"] == expected_params


def test_lambda_runtime_error_payload_raises_invocation_error(handler_lambda_first: NetworkHandler, mock_lambda: MagicMock) -> None:
    # arrange - an unhandled exception outside the probe's try: the runtime reports FunctionError and its own payload
    mock_lambda.invoke.return_value = make_lambda_error_response("'PROTOCOL'", "KeyError")
    expected_message = f'Lambda {PROBE_LAMBDA_NAME} failed (Unhandled): {{"errorMessage": "\'PROTOCOL\'", "errorType": "KeyError", "stackTrace": []}}'

    # act & assert
    with pytest.raises(CatsLambdaInvocationError) as exc_info:
        handler_lambda_first.lambda_tcp_call("target.internal", 443)
    assert str(exc_info.value) == expected_message


def test_lambda_timeout_raises_invocation_error_naming_the_timeout(handler_lambda_first: NetworkHandler, mock_lambda: MagicMock) -> None:
    # arrange - the function timed out: the runtime's errorMessage is the diagnosis, not "Invalid probe output"
    mock_lambda.invoke.return_value = make_lambda_error_response(
        "2026-09-24T10:00:00.000Z abc Task timed out after 30.00 seconds", "Sandbox.Timedout"
    )
    expected_message = (
        f"Lambda {PROBE_LAMBDA_NAME} failed (Unhandled): "
        + '{"errorMessage": "2026-09-24T10:00:00.000Z abc Task timed out after 30.00 seconds", "errorType": "Sandbox.Timedout", "stackTrace": []}'
    )

    # act & assert
    with pytest.raises(CatsLambdaInvocationError) as exc_info:
        handler_lambda_first.lambda_tcp_call("target.internal", 443)
    assert str(exc_info.value) == expected_message


def test_lambda_payload_without_result_and_without_function_error_raises_invalid_output(
    handler_lambda_first: NetworkHandler, mock_lambda: MagicMock
) -> None:
    # arrange - a successful invocation whose payload is not the probe's document
    mock_lambda.invoke.return_value = {"Payload": MagicMock(read=lambda: b'{"unexpected": true}')}

    # act & assert
    with pytest.raises(CatsLambdaInvocationError, match=f"Invalid probe output from {PROBE_LAMBDA_NAME}"):
        handler_lambda_first.lambda_tcp_call("target.internal", 443)


def test_lambda_udp_non_sent_outcome_is_returned_not_raised(
    handler_lambda_first: NetworkHandler, mock_lambda: MagicMock, fixed_uuid: uuid.UUID
) -> None:
    # arrange
    mock_lambda.invoke.return_value = make_lambda_response("DNS_FAILURE", reason="[Errno -2] Name or service not known")

    # act
    result = handler_lambda_first.lambda_udp_call("nonexistent.invalid", 5000)

    # assert
    assert (result.response, result.reason, result.correlation_id) == (
        CatsNetworkResponse.DNS_FAILURE,
        "[Errno -2] Name or service not known",
        str(fixed_uuid),
    )


# =============================================================================
# Lambda transport: the execution-role propagation retry
# =============================================================================


def test_lambda_probe_retries_the_execution_role_propagation_then_succeeds(
    handler_lambda_first: NetworkHandler, mock_lambda: MagicMock, sleep_advances_clock: MagicMock
) -> None:
    # arrange - Lambda cannot assume the freshly created execution role on the first two invokes, then can
    mock_lambda.invoke.side_effect = [LAMBDA_CANNOT_BE_ASSUMED, LAMBDA_CANNOT_BE_ASSUMED, make_lambda_response("REFUSED")]
    expected_sleeps = [call(LAMBDA_ROLE_PROPAGATION_RETRY_INTERVAL_SECONDS), call(LAMBDA_ROLE_PROPAGATION_RETRY_INTERVAL_SECONDS)]

    # act
    result = handler_lambda_first.lambda_tcp_call("target.internal", 443)

    # assert
    assert (result.response, mock_lambda.invoke.call_count, sleep_advances_clock.call_args_list) == (
        CatsNetworkResponse.REFUSED,
        3,
        expected_sleeps,
    )


def test_lambda_probe_other_access_denied_raises_without_retry(
    handler_lambda_first: NetworkHandler, mock_lambda: MagicMock, no_sleep: MagicMock
) -> None:
    # arrange - the test role lacking lambda:InvokeFunction is a real denial, not the execution-role race
    mock_lambda.invoke.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "not authorized to perform: lambda:InvokeFunction"}}, "Invoke"
    )

    # act / assert
    with pytest.raises(ClientError, match="lambda:InvokeFunction"):
        handler_lambda_first.lambda_tcp_call("target.internal", 443)
    assert (mock_lambda.invoke.call_count, no_sleep.call_count) == (1, 0)


def test_lambda_probe_execution_role_never_assumable_exhausts_the_budget_and_raises(
    handler_lambda_first: NetworkHandler, mock_lambda: MagicMock, sleep_advances_clock: MagicMock
) -> None:
    # arrange - the race never clears within the window; invokes at 0s, 3s, ..., 30s, the one at the deadline being the last
    mock_lambda.invoke.side_effect = LAMBDA_CANNOT_BE_ASSUMED

    # act / assert
    with pytest.raises(ClientError, match="cannot be assumed by Lambda"):
        handler_lambda_first.lambda_tcp_call("target.internal", 443)
    assert mock_lambda.invoke.call_count == 11


# =============================================================================
# UDP: generated correlation ID and receipt token prefixing
# =============================================================================


def test_ec2_udp_call_result_carries_generated_correlation_id(handler: NetworkHandler, mock_ssm: MagicMock, fixed_uuid: uuid.UUID) -> None:
    # arrange
    mock_ssm.get_command_invocation.return_value = make_ssm_response("Success", "SENT")

    # act
    result = handler.ec2_udp_call("target.internal", 5000)

    # assert
    assert (result.response, result.reason, result.http_status, result.correlation_id) == (CatsNetworkResponse.SENT, None, None, str(fixed_uuid))


def test_lambda_udp_call_result_carries_generated_correlation_id(
    handler_lambda_first: NetworkHandler, mock_lambda: MagicMock, fixed_uuid: uuid.UUID
) -> None:
    # arrange
    mock_lambda.invoke.return_value = make_lambda_response("SENT")

    # act
    result = handler_lambda_first.lambda_udp_call("target.internal", 5000)

    # assert
    assert (result.response, result.correlation_id) == (CatsNetworkResponse.SENT, str(fixed_uuid))


def test_ec2_udp_call_generates_a_fresh_uuid_per_call(handler: NetworkHandler, mock_ssm: MagicMock) -> None:
    # arrange
    mock_ssm.get_command_invocation.return_value = make_ssm_response("Success", "SENT")

    # act
    first = handler.ec2_udp_call("target.internal", 5000)
    second = handler.ec2_udp_call("target.internal", 5000)

    # assert
    assert first.correlation_id is not None
    assert uuid.UUID(first.correlation_id).version == 4
    assert first.correlation_id != second.correlation_id


def test_ec2_udp_call_registry_hit_prepends_token(handler: NetworkHandler, mock_ssm: MagicMock, fixed_uuid: uuid.UUID) -> None:
    # arrange
    register_udp_receipt_token("target.internal", RECEIPT_TOKEN)
    mock_ssm.get_command_invocation.return_value = make_ssm_response("Success", "SENT")

    # act
    result = handler.ec2_udp_call("target.internal", 5000)

    # assert - token rides the transport; the result's correlation ID stays bare
    assert (_ssm_params(mock_ssm)["TargetMessage"], result.correlation_id) == ([f"{RECEIPT_TOKEN}:{fixed_uuid}"], str(fixed_uuid))


def test_ec2_udp_call_registry_miss_sends_bare_payload(handler: NetworkHandler, mock_ssm: MagicMock, fixed_uuid: uuid.UUID) -> None:
    # arrange
    mock_ssm.get_command_invocation.return_value = make_ssm_response("Success", "SENT")

    # act
    handler.ec2_udp_call("target.internal", 5000)

    # assert
    assert _ssm_params(mock_ssm)["TargetMessage"] == [str(fixed_uuid)]


def test_udp_call_registry_miss_warns_that_a_cats_listener_would_drop_it(
    handler: NetworkHandler, mock_ssm: MagicMock, fixed_uuid: uuid.UUID, caplog: pytest.LogCaptureFixture
) -> None:
    """A bare datagram to a CATS listener is dropped and receive_s3 reads False; the miss must be visible, not DEBUG."""
    # arrange
    mock_ssm.get_command_invocation.return_value = make_ssm_response("Success", "SENT")
    expected_message = (
        "No receipt token is known for target.internal, so the datagram is sent bare. If this is a CATS listener, name its account "
        + "first (in pytest, request that account's fixture; in cats run or MCP, pass the receiving account); otherwise ignore this."
    )

    # act
    with caplog.at_level(logging.WARNING, logger="cats.resources.network_interact"):
        handler.ec2_udp_call("target.internal", 5000)

    # assert
    assert [(r.levelname, r.message) for r in caplog.records] == [("WARNING", expected_message)]


def test_udp_call_registry_hit_logs_nothing(
    handler: NetworkHandler, mock_ssm: MagicMock, fixed_uuid: uuid.UUID, caplog: pytest.LogCaptureFixture
) -> None:
    # arrange
    register_udp_receipt_token("target.internal", RECEIPT_TOKEN)
    mock_ssm.get_command_invocation.return_value = make_ssm_response("Success", "SENT")

    # act
    with caplog.at_level(logging.WARNING, logger="cats.resources.network_interact"):
        handler.ec2_udp_call("target.internal", 5000)

    # assert
    assert caplog.records == []


def test_lambda_udp_call_registry_hit_prepends_token(handler_lambda_first: NetworkHandler, mock_lambda: MagicMock, fixed_uuid: uuid.UUID) -> None:
    # arrange
    register_udp_receipt_token("target.internal", RECEIPT_TOKEN)
    mock_lambda.invoke.return_value = make_lambda_response("SENT")

    # act
    result = handler_lambda_first.lambda_udp_call("target.internal", 5000)

    # assert
    assert (_lambda_event(mock_lambda)["TARGET_MESSAGE"], result.correlation_id) == (f"{RECEIPT_TOKEN}:{fixed_uuid}", str(fixed_uuid))


def test_lambda_udp_call_registry_miss_sends_bare_payload(
    handler_lambda_first: NetworkHandler, mock_lambda: MagicMock, fixed_uuid: uuid.UUID
) -> None:
    # arrange
    mock_lambda.invoke.return_value = make_lambda_response("SENT")

    # act
    handler_lambda_first.lambda_udp_call("target.internal", 5000)

    # assert
    assert _lambda_event(mock_lambda)["TARGET_MESSAGE"] == str(fixed_uuid)


# =============================================================================
# Delegation tests
# =============================================================================


def test_http_call_delegates_to_ec2_when_ec2_first(handler: NetworkHandler, mock_ssm: MagicMock) -> None:
    # arrange
    mock_ssm.get_command_invocation.return_value = make_ssm_response("Success", "SUCCESS")

    # act
    result = handler.http_call("target.internal", 8080)

    # assert
    assert result == CatsNetworkResponse.SUCCESS
    mock_ssm.send_command.assert_called_once()


def test_http_call_delegates_to_lambda_when_lambda_first(handler_lambda_first: NetworkHandler, mock_lambda: MagicMock) -> None:
    # arrange
    mock_lambda.invoke.return_value = make_lambda_response("SUCCESS")

    # act
    result = handler_lambda_first.http_call("target.internal", 8080)

    # assert
    assert result == CatsNetworkResponse.SUCCESS
    mock_lambda.invoke.assert_called_once()


@pytest.mark.parametrize(
    ("method_name", "protocol"),
    [("http_call", "http"), ("https_call", "https"), ("tcp_call", "tcp"), ("udp_call", "udp")],
)
def test_default_methods_dispatch_the_matching_protocol(handler: NetworkHandler, mock_ssm: MagicMock, method_name: str, protocol: str) -> None:
    # arrange
    mock_ssm.get_command_invocation.return_value = make_ssm_response("Success", "SENT" if protocol == "udp" else "SUCCESS")

    # act
    getattr(handler, method_name)("target.internal", 8080)

    # assert
    assert _ssm_params(mock_ssm)["Protocol"] == [protocol]


def test_udp_call_dispatch_returns_result_with_correlation_id(handler: NetworkHandler, mock_ssm: MagicMock, fixed_uuid: uuid.UUID) -> None:
    # arrange
    mock_ssm.get_command_invocation.return_value = make_ssm_response("Success", "SENT")

    # act
    result = handler.udp_call("target.internal", 5000)

    # assert
    assert (result.response, result.correlation_id) == (CatsNetworkResponse.SENT, str(fixed_uuid))


def test_response_bucket_name_is_the_deployed_bucket(handler: NetworkHandler) -> None:
    # act
    result = handler.response_bucket_name

    # assert
    assert result == "cats-responses-123456789012"


# =============================================================================
# Error handling tests - calling unavailable handler
# =============================================================================


def test_no_handlers_raises_at_init(cats_yaml: Path) -> None:
    """Config cannot declare an empty handler list; a stack whose outputs carry none is refused at runtime as well."""
    # arrange
    write_config(
        cats_yaml,
        {
            "cats_deployment_settings": {
                "parent_account_id": "123456789012",
                "aws_region": "us-east-1",
                "cfn_stack_prefix": "CATS-test-infra-",
            },
            "global_network_settings": {
                "enabled_network_handlers": ["ec2"],
            },
            "accounts_under_test": {
                "test_account": {
                    "account_id": "123456789012",
                    "account_network_under_test": {
                        "private": {
                            "vpc_id": "vpc-12345",
                            "subnet_ids": ["subnet-111"],
                        }
                    },
                }
            },
        },
    )
    mock_host = MagicMock()
    target_config = Settings().accounts_under_test["test_account"].account_network_under_test["private"]
    deployed = DeployedTargetResources(
        response_bucket_name="cats-responses-123456789012",
        vpc_id="vpc-12345",
        subnet_id_list=["subnet-111"],
        account_name="test_account",
        target_name="private",
        enabled_handlers=[],
        ec2=make_deployed_ec2(),
        lambda_=None,
    )

    # act & assert
    with pytest.raises(CatsConfigError, match="No network handlers deployed"):
        NetworkHandler(mock_host, deployed, target_config)


@pytest.mark.parametrize("method_name", ["lambda_http_call", "lambda_https_call", "lambda_tcp_call", "lambda_udp_call"])
def test_lambda_call_raises_when_lambda_not_enabled(handler: NetworkHandler, method_name: str) -> None:
    # arrange - handler fixture has lambda_=None
    expected_message = "Lambda handler not available. Add 'lambda' to enabled_network_handlers in cats.yaml and redeploy."

    # act & assert
    with pytest.raises(CatsConfigError) as exc_info:
        getattr(handler, method_name)("target.internal", 8080)
    assert str(exc_info.value) == expected_message


# =============================================================================
# shell() tests
# =============================================================================


def make_raw_ssm(status: str, stdout: str = "", stderr: str = "", response_code: int | None = None) -> dict[str, object]:
    """Build a raw SSM get_command_invocation response (no JSON wrapping)."""
    resp: dict[str, object] = {
        "Status": status,
        "StatusDetails": f"{status} details",
        "StandardOutputContent": stdout,
        "StandardErrorContent": stderr,
    }
    if response_code is not None:
        resp["ResponseCode"] = response_code
    return resp


@pytest.fixture
def shell_handler(cats_yaml: Path, mock_ssm: MagicMock) -> NetworkHandler:
    """NetworkHandler with allow_shell=True inherited from global settings."""
    return make_handler(cats_yaml, mock_ssm, ["ec2"], allow_shell=True)


def test_shell_str_normalizes_to_list(shell_handler: NetworkHandler, mock_ssm: MagicMock) -> None:
    # arrange
    mock_ssm.send_command.return_value = {"Command": {"CommandId": "cmd-1"}}
    mock_ssm.get_command_invocation.return_value = make_raw_ssm("Success", stdout="hello\n")

    # act
    shell_handler.shell("echo hello")

    # assert
    assert mock_ssm.send_command.call_args.kwargs["Parameters"] == {"commands": ["echo hello"]}


def test_shell_list_passes_through(shell_handler: NetworkHandler, mock_ssm: MagicMock) -> None:
    # arrange
    mock_ssm.send_command.return_value = {"Command": {"CommandId": "cmd-1"}}
    mock_ssm.get_command_invocation.return_value = make_raw_ssm("Success")

    # act
    shell_handler.shell(["cd /tmp", "ls"])

    # assert
    assert mock_ssm.send_command.call_args.kwargs["Parameters"] == {"commands": ["cd /tmp", "ls"]}


def test_shell_allow_shell_false_raises(handler: NetworkHandler) -> None:
    # arrange - `handler` fixture uses default allow_shell=False
    expected_message = "allow_shell is not enabled for account 'test_account'. Set allow_shell: true under account_settings and redeploy."

    # act & assert
    with pytest.raises(CatsConfigError) as exc_info:
        handler.shell("echo hi")
    assert str(exc_info.value) == expected_message


def test_shell_success_returns_shell_result(shell_handler: NetworkHandler, mock_ssm: MagicMock) -> None:
    # arrange
    mock_ssm.send_command.return_value = {"Command": {"CommandId": "cmd-1"}}
    mock_ssm.get_command_invocation.return_value = make_raw_ssm("Success", stdout="hi\n", stderr="warn\n")

    # act
    result = shell_handler.shell("echo hi")

    # assert
    assert result.stdout == "hi"
    assert result.stderr == "warn"
    assert result.exit_code == 0
    assert result.stdout_truncated is False
    assert result.stderr_truncated is False


def test_shell_failed_returns_nonzero_exit(shell_handler: NetworkHandler, mock_ssm: MagicMock) -> None:
    # arrange
    mock_ssm.send_command.return_value = {"Command": {"CommandId": "cmd-1"}}
    mock_ssm.get_command_invocation.return_value = make_raw_ssm("Failed", stdout="", stderr="oops", response_code=42)

    # act
    result = shell_handler.shell("exit 42")

    # assert
    assert result.exit_code == 42
    assert result.stderr == "oops"


@pytest.mark.parametrize(
    "status",
    ["TimedOut", "Cancelled", "Cancelling", "Undeliverable", "Terminated", "AccessDenied", "DeliveryTimedOut", "ExecutionTimedOut"],
)
def test_shell_infrastructure_statuses_raise(shell_handler: NetworkHandler, mock_ssm: MagicMock, status: str) -> None:
    # arrange
    mock_ssm.send_command.return_value = {"Command": {"CommandId": "cmd-1"}}
    mock_ssm.get_command_invocation.return_value = make_raw_ssm(status)

    # act & assert
    with pytest.raises(CatsSSMError) as exc_info:
        shell_handler.shell("echo hi")
    assert f"SSM command {status}" in str(exc_info.value)


def test_shell_stdout_truncation_detected(shell_handler: NetworkHandler, mock_ssm: MagicMock) -> None:
    # arrange
    mock_ssm.send_command.return_value = {"Command": {"CommandId": "cmd-1"}}
    mock_ssm.get_command_invocation.return_value = make_raw_ssm("Success", stdout="x" * SSM_STDOUT_LIMIT)

    # act
    result = shell_handler.shell("seq 1 99999")

    # assert
    assert result.stdout_truncated is True
    assert result.stderr_truncated is False


def test_shell_stderr_truncation_detected(shell_handler: NetworkHandler, mock_ssm: MagicMock) -> None:
    # arrange
    mock_ssm.send_command.return_value = {"Command": {"CommandId": "cmd-1"}}
    mock_ssm.get_command_invocation.return_value = make_raw_ssm("Success", stderr="e" * SSM_STDERR_LIMIT)

    # act
    result = shell_handler.shell("noisy")

    # assert
    assert result.stdout_truncated is False
    assert result.stderr_truncated is True


def test_shell_no_truncation(shell_handler: NetworkHandler, mock_ssm: MagicMock) -> None:
    # arrange
    mock_ssm.send_command.return_value = {"Command": {"CommandId": "cmd-1"}}
    mock_ssm.get_command_invocation.return_value = make_raw_ssm("Success", stdout="short", stderr="short")

    # act
    result = shell_handler.shell("echo short")

    # assert
    assert result.stdout_truncated is False
    assert result.stderr_truncated is False


def test_shell_returns_shell_result_type(shell_handler: NetworkHandler, mock_ssm: MagicMock) -> None:
    # arrange
    mock_ssm.send_command.return_value = {"Command": {"CommandId": "cmd-1"}}
    mock_ssm.get_command_invocation.return_value = make_raw_ssm("Success", stdout="x")

    # act
    result = shell_handler.shell("echo x")

    # assert
    assert isinstance(result, ShellResult)
