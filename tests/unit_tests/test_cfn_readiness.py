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

"""Tests for the deploy readiness gate (cats/cfn/readiness.py).

Every listener step is one non-blocking ec2:GetConsoleOutput call, so each test drives `advance`
once against a known console and asserts the phase, the error text and the API calls together.
The console texts are cut from a real AL2023 listener's serial console.
"""

from typing import Any
from unittest.mock import MagicMock, call

import pytest
from botocore.exceptions import ClientError
from pytest_mock import MockerFixture

from cats.cfn.readiness import (
    CONSOLE_TAIL_LINES,
    GATE_PROBE_EVENT,
    READINESS_TIMEOUT_SECONDS,
    LambdaReadiness,
    ListenerReadiness,
    ReadinessFailure,
    RoleReadiness,
    StackReadiness,
    console_tail,
    lambdas_from_outputs,
    listeners_from_outputs,
)
from cats.cfn.session import DEPLOY_ROLE_SESSION_NAME
from cats.constants import LISTENER_FAILED_MARKER, LISTENER_READY_MARKER
from tests.utils import FAKE_ASSUMED_CREDENTIALS, RoleProbeMocks, patch_role_probe

INSTANCE_ID = "i-0123456789abcdef0"
TEST_ROLE_ARN = "arn:aws:iam::111111111111:role/CATS-test-infra-testaccount-default-test-role"
READONLY_ROLE_ARN = "arn:aws:iam::111111111111:role/CATS-test-infra-testaccount-readonly"
STACK_NAME = "CATS-test-infra-testaccount"
REGION = "eu-west-1"
ASSUME_ROLE_DENIED_MESSAGE = (
    f"User: arn:aws:sts::999999999999:assumed-role/Admin/dev is not authorized to perform: sts:AssumeRole on resource: {TEST_ROLE_ARN}"
)
DESCRIBE_STACKS_DENIED_MESSAGE = (
    f"User: {TEST_ROLE_ARN} is not authorized to perform: cloudformation:DescribeStacks on resource: {STACK_NAME} "
    + "because no identity-based policy allows the cloudformation:DescribeStacks action"
)
ASSUME_ROLE_DENIED = ClientError({"Error": {"Code": "AccessDenied", "Message": ASSUME_ROLE_DENIED_MESSAGE}}, "AssumeRole")
DESCRIBE_STACKS_DENIED = ClientError({"Error": {"Code": "AccessDenied", "Message": DESCRIBE_STACKS_DENIED_MESSAGE}}, "DescribeStacks")

PROBE_LAMBDA_NAME = "CATS-test-infra-testaccount-probe-private"
LAMBDA_CANNOT_BE_ASSUMED_MESSAGE = "The role defined for the function cannot be assumed by Lambda."
LAMBDA_CANNOT_BE_ASSUMED = ClientError({"Error": {"Code": "AccessDeniedException", "Message": LAMBDA_CANNOT_BE_ASSUMED_MESSAGE}}, "Invoke")
LAMBDA_INVOKE_NOT_AUTHORIZED = ClientError(
    {"Error": {"Code": "AccessDeniedException", "Message": f"User: {TEST_ROLE_ARN} is not authorized to perform: lambda:InvokeFunction"}}, "Invoke"
)

# The console of a listener whose user data is still running: cloud-init has reached its final
# stage, the units are enabled, no verdict yet.
CONSOLE_BOOTING = "\n".join(
    [
        "[    9.127806] cloud-init[1644]: Cloud-init v. 22.2.2 running 'modules:final' at Wed, 30 Sep 2026 15:48:19 +0000. Up 9.06 seconds.",
        "[    9.458075] cloud-init[1644]: Created symlink /etc/systemd/system/multi-user.target.wants/tcpserver.service → "
        + "/etc/systemd/system/tcpserver.service.",
        '[    9.797084] cloud-init[1644]: Removed "/etc/systemd/system/multi-user.target.wants/sshd.service".',
        "",
    ]
)
CONSOLE_READY = CONSOLE_BOOTING + f"[   10.129306] cloud-init[1644]: {LISTENER_READY_MARKER}\n"
CONSOLE_PORTS_UNBOUND = (
    CONSOLE_BOOTING
    + f"[   30.129306] cloud-init[1644]: {LISTENER_FAILED_MARKER} tcp/8080\n"
    + "[   30.229306] cloud-init[1644]: x tcpserver.service - CATS TCP listener\n"
)
CONSOLE_STOPPED_EARLY = (
    CONSOLE_BOOTING
    + "[    9.900000] cloud-init[1644]: 2026-09-30 15:48:20,101 - cc_scripts_user.py[WARNING]: Failed to run module scripts_user "
    + "(scripts in /var/lib/cloud/instance)\n"
    + "[   10.209960] cloud-init[1644]: Cloud-init v. 22.2.2 finished at Wed, 30 Sep 2026 15:48:20 +0000. Datasource DataSourceEc2.  "
    + "Up 10.20 seconds\n"
)


def make_listener(started_at: float = 100.0) -> ListenerReadiness:
    return ListenerReadiness(instance_id=INSTANCE_ID, target_name="private", started_at=started_at)


def make_ec2(console: str | None) -> MagicMock:
    """EC2 client mock whose GetConsoleOutput returns `console` already decoded, as boto3 does; None: nothing posted yet."""
    ec2 = MagicMock()
    response: dict[str, Any] = {"InstanceId": INSTANCE_ID}
    if console is not None:
        response["Output"] = console
    ec2.get_console_output.return_value = response
    return ec2


@pytest.fixture(autouse=True)
def clock(mocker: MockerFixture) -> dict[str, float]:
    """time.monotonic under the module's control; advance by mutating clock['now']."""
    state = {"now": 100.0}
    mocker.patch("cats.cfn.readiness.time.monotonic", side_effect=lambda: state["now"])
    return state


def make_role(stack_name: str | None = STACK_NAME, started_at: float = 100.0) -> RoleReadiness:
    return RoleReadiness(role_arn=TEST_ROLE_ARN, region=REGION, stack_name=stack_name, started_at=started_at)


def make_lambda_readiness(started_at: float = 100.0) -> LambdaReadiness:
    return LambdaReadiness(function_name=PROBE_LAMBDA_NAME, started_at=started_at)


@pytest.fixture
def aws(mocker: MockerFixture) -> RoleProbeMocks:
    """STS and CloudFormation under the test's control; every probe succeeds unless a test says otherwise."""
    return patch_role_probe(mocker, STACK_NAME)


# =============================================================================
# ListenerReadiness.advance
# =============================================================================


def test_silent_console_within_the_window_keeps_booting(clock: dict[str, float]) -> None:
    # arrange - a console that has not posted anything yet has no Output key at all
    listener = make_listener()
    ec2 = make_ec2(console=None)
    clock["now"] = 100.0 + READINESS_TIMEOUT_SECONDS - 1

    # act
    notice = listener.advance(ec2)

    # assert
    expected_calls = [call.get_console_output(InstanceId=INSTANCE_ID, Latest=True)]
    assert (notice, listener.phase, listener.error, ec2.method_calls) == (None, "booting", None, expected_calls)


def test_console_without_a_verdict_keeps_booting() -> None:
    # arrange - user data is running; nothing decides yet
    listener = make_listener()
    ec2 = make_ec2(console=CONSOLE_BOOTING)

    # act
    notice = listener.advance(ec2)

    # assert
    assert (notice, listener.phase, listener.error) == (None, "booting", None)


def test_ready_marker_makes_the_listener_ready_with_a_notice() -> None:
    # arrange
    listener = make_listener()
    ec2 = make_ec2(console=CONSOLE_READY)

    # act
    notice = listener.advance(ec2)

    # assert
    assert (notice, listener.phase, listener.error) == (f"EC2 listener {INSTANCE_ID} (private) ready", "ready", None)


def test_failed_marker_fails_with_the_console_tail(clock: dict[str, float]) -> None:
    # arrange - user data waited its 20s for the port and gave up
    listener = make_listener()
    ec2 = make_ec2(console=CONSOLE_PORTS_UNBOUND)
    clock["now"] = 130.0
    expected_error = (
        f"EC2 listener {INSTANCE_ID} (private) is not ready after 30s: its user data found listener ports still unbound. Console:\n"
        + console_tail(CONSOLE_PORTS_UNBOUND)
    )

    # act
    notice = listener.advance(ec2)

    # assert
    assert (notice, listener.phase, listener.error) == (None, "failed", expected_error)


def test_cloud_init_finished_without_a_verdict_fails_without_waiting(clock: dict[str, float]) -> None:
    # arrange - the script died before its last line; cloud-init still writes its own final line
    listener = make_listener()
    ec2 = make_ec2(console=CONSOLE_STOPPED_EARLY)
    clock["now"] = 112.0
    expected_error = (
        f"EC2 listener {INSTANCE_ID} (private) is not ready after 12s: cloud-init finished without the listeners' ready line, "
        + "so the user data stopped early. Console:\n"
        + console_tail(CONSOLE_STOPPED_EARLY)
    )

    # act
    notice = listener.advance(ec2)

    # assert
    assert (notice, listener.phase, listener.error) == (None, "failed", expected_error)


def test_silent_console_past_the_window_fails_naming_the_bound(clock: dict[str, float]) -> None:
    # arrange
    listener = make_listener()
    ec2 = make_ec2(console=None)
    clock["now"] = 100.0 + READINESS_TIMEOUT_SECONDS
    expected_error = (
        f"EC2 listener {INSTANCE_ID} (private) is not ready after {READINESS_TIMEOUT_SECONDS}s: "
        + f"it did not report ready within {READINESS_TIMEOUT_SECONDS}s. Console:\n  (no console output yet)"
    )

    # act
    notice = listener.advance(ec2)

    # assert
    assert (notice, listener.phase, listener.error) == (None, "failed", expected_error)


def test_instance_type_without_live_console_fails_naming_the_remedy(clock: dict[str, float]) -> None:
    # arrange - Latest=True is refused on an instance type that is not Nitro-based
    listener = make_listener()
    ec2 = MagicMock()
    ec2.get_console_output.side_effect = ClientError(
        {"Error": {"Code": "UnsupportedOperation", "Message": "The instance type does not support the latest console output."}},
        "GetConsoleOutput",
    )
    clock["now"] = 101.0
    expected_error = (
        f"EC2 listener {INSTANCE_ID} (private) is not ready after 1s: its instance type has no live serial console "
        + "(ec2:GetConsoleOutput with Latest), so its boot cannot be watched. Use a Nitro-based ec2_instance_type; the default t3.micro is one"
    )

    # act
    notice = listener.advance(ec2)

    # assert
    assert (notice, listener.phase, listener.error) == (None, "failed", expected_error)


def test_other_console_error_propagates() -> None:
    # arrange - the deploy identity lacks ec2:GetConsoleOutput: the caller's problem, not the listener's
    listener = make_listener()
    ec2 = MagicMock()
    ec2.get_console_output.side_effect = ClientError(
        {"Error": {"Code": "UnauthorizedOperation", "Message": "You are not authorized to perform this operation."}}, "GetConsoleOutput"
    )

    # act / assert
    with pytest.raises(ClientError, match="UnauthorizedOperation"):
        listener.advance(ec2)
    assert listener.phase == "booting"


@pytest.mark.parametrize("phase", ["ready", "failed"])
def test_done_listener_makes_no_calls(phase: str) -> None:
    # arrange
    listener = make_listener()
    listener.phase = phase  # type: ignore[assignment]
    ec2 = make_ec2(console=CONSOLE_READY)

    # act
    notice = listener.advance(ec2)

    # assert
    assert (notice, ec2.method_calls) == (None, [])


# =============================================================================
# console_tail
# =============================================================================


def test_console_tail_keeps_the_last_lines_indented() -> None:
    # arrange
    output = "\n".join(f"line {i}" for i in range(CONSOLE_TAIL_LINES + 5))
    expected = "\n".join(f"  line {i}" for i in range(5, CONSOLE_TAIL_LINES + 5))

    # act
    tail = console_tail(output)

    # assert
    assert tail == expected


def test_console_tail_of_nothing_says_so() -> None:
    # act
    tail = console_tail("")

    # assert
    assert tail == "  (no console output yet)"


# =============================================================================
# listeners_from_outputs
# =============================================================================


def test_listeners_from_outputs_takes_one_per_instance_id_output_in_order() -> None:
    # arrange
    outputs: list[Any] = [
        {"OutputKey": "TestRoleArn", "OutputValue": "arn:aws:iam::111111111111:role/CATS-test"},
        {"OutputKey": "Ec2InstanceIdprivate", "OutputValue": "i-0aaa"},
        {"OutputKey": "Ec2DnsNameprivate", "OutputValue": "ip-10-0-0-1.ec2.internal"},
        {"OutputKey": "Ec2InstanceIdpublic", "OutputValue": "i-0bbb"},
    ]
    expected = [("i-0aaa", "private"), ("i-0bbb", "public")]

    # act
    listeners = listeners_from_outputs(outputs)

    # assert
    assert expected == [(listener.instance_id, listener.target_name) for listener in listeners]


def test_listeners_from_outputs_without_instance_ids_is_empty() -> None:
    # arrange - an account with no network targets has a test role and a bucket, no listeners
    outputs: list[Any] = [
        {"OutputKey": "TestRoleArn", "OutputValue": "arn:aws:iam::111111111111:role/CATS-test"},
        {"OutputKey": "ResponseBucketName", "OutputValue": "cats-bucket"},
    ]

    # act
    listeners = listeners_from_outputs(outputs)

    # assert
    assert listeners == []


# =============================================================================
# RoleReadiness.advance
# =============================================================================


def test_assume_role_denied_keeps_assuming_without_describing(aws: RoleProbeMocks) -> None:
    # arrange - the trust policy has not converged: STS does not know the role yet
    role = make_role()
    aws.sts.assume_role.side_effect = ASSUME_ROLE_DENIED

    # act
    notice = role.advance()

    # assert
    assert (notice, role.phase, role.error, aws.cfn.describe_stacks.call_count) == (None, "probing", None, 0)


def test_describe_denied_reassumes_on_the_next_cycle(aws: RoleProbeMocks) -> None:
    # arrange - the role is assumable but CatsTestRolePolicy is denied; a session assumed in that
    # window can stay denied after IAM has converged, so each cycle probes a fresh one
    role = make_role()
    aws.cfn.describe_stacks.side_effect = DESCRIBE_STACKS_DENIED

    # act
    first = role.advance()
    second = role.advance()

    # assert
    expected_assume_calls = [
        call(RoleArn=TEST_ROLE_ARN, RoleSessionName=DEPLOY_ROLE_SESSION_NAME),
        call(RoleArn=TEST_ROLE_ARN, RoleSessionName=DEPLOY_ROLE_SESSION_NAME),
    ]
    expected_describe_calls = [call(StackName=STACK_NAME), call(StackName=STACK_NAME)]
    assert ((first, second), role.phase, aws.sts.assume_role.call_args_list, aws.cfn.describe_stacks.call_args_list) == (
        (None, None),
        "probing",
        expected_assume_calls,
        expected_describe_calls,
    )


def test_describe_denied_then_fresh_session_allowed_becomes_ready(aws: RoleProbeMocks) -> None:
    # arrange - the first session is denied; the session assumed on the next cycle is allowed
    role = make_role()
    aws.cfn.describe_stacks.side_effect = [DESCRIBE_STACKS_DENIED, {"Stacks": [{"StackName": STACK_NAME}]}]

    # act
    first = role.advance()
    second = role.advance()

    # assert
    assert ((first, second), role.phase, aws.sts.assume_role.call_count) == (
        (None, "Test role CATS-test-infra-testaccount-default-test-role authorized"),
        "ready",
        2,
    )


def test_describe_allowed_makes_the_test_role_ready_in_the_cycle_it_was_assumed(aws: RoleProbeMocks) -> None:
    # act
    role = make_role()
    notice = role.advance()

    # assert
    expected_calls = [call.assume_role(RoleArn=TEST_ROLE_ARN, RoleSessionName=DEPLOY_ROLE_SESSION_NAME)]
    assert (notice, role.phase, aws.sts.method_calls, aws.cfn.describe_stacks.call_args_list) == (
        "Test role CATS-test-infra-testaccount-default-test-role authorized",
        "ready",
        expected_calls,
        [call(StackName=STACK_NAME)],
    )


def test_additional_role_is_ready_when_assumable_and_never_describes(aws: RoleProbeMocks) -> None:
    # arrange - an additional role's grants are the user's; being assumable is the verdict
    role = RoleReadiness(role_arn=READONLY_ROLE_ARN, region=REGION, stack_name=None)

    # act
    notice = role.advance()

    # assert
    assert (notice, role.phase, aws.cfn.describe_stacks.call_count) == ("Role CATS-test-infra-testaccount-readonly assumable", "ready", 0)


def test_assume_denied_past_the_bound_fails_naming_both_causes(aws: RoleProbeMocks, clock: dict[str, float]) -> None:
    # arrange
    role = make_role()
    aws.sts.assume_role.side_effect = ASSUME_ROLE_DENIED
    clock["now"] = 100.0 + READINESS_TIMEOUT_SECONDS
    expected_error = (
        f"Test role {TEST_ROLE_ARN} could not be assumed after {READINESS_TIMEOUT_SECONDS}s: {ASSUME_ROLE_DENIED_MESSAGE}. "
        + "Either its trust policy has not converged in IAM yet, or the identity running 'cats infra deploy' is not allowed sts:AssumeRole "
        + "on CATS test roles (the readiness probe assumes the test role from the same credentials 'cats test' uses)."
    )

    # act
    notice = role.advance()

    # assert
    assert (notice, role.phase, role.error) == (None, "failed", expected_error)


def test_describe_denied_past_the_bound_fails_naming_both_causes(aws: RoleProbeMocks, clock: dict[str, float]) -> None:
    # arrange - assumed at 100 s; the grant is still denied when the bound passes
    role = make_role()
    aws.cfn.describe_stacks.side_effect = DESCRIBE_STACKS_DENIED
    role.advance()
    clock["now"] = 100.0 + READINESS_TIMEOUT_SECONDS
    expected_error = (
        f"Test role {TEST_ROLE_ARN} was assumed but cloudformation:DescribeStacks on the stack is still denied after {READINESS_TIMEOUT_SECONDS}s: "
        + f"{DESCRIBE_STACKS_DENIED_MESSAGE}. Either its CatsTestRolePolicy has not converged in IAM yet, or a service control policy or "
        + "permissions boundary denies it."
    )

    # act
    notice = role.advance()

    # assert
    assert (notice, role.phase, role.error) == (None, "failed", expected_error)


def test_assume_denied_at_the_bound_after_describe_denied_names_the_assume(aws: RoleProbeMocks, clock: dict[str, float]) -> None:
    # arrange - DescribeStacks was denied first; at the bound the fresh AssumeRole is the call denied, so it is the one reported
    role = make_role()
    aws.cfn.describe_stacks.side_effect = DESCRIBE_STACKS_DENIED
    role.advance()
    aws.sts.assume_role.side_effect = ASSUME_ROLE_DENIED
    clock["now"] = 100.0 + READINESS_TIMEOUT_SECONDS
    expected_error = (
        f"Test role {TEST_ROLE_ARN} could not be assumed after {READINESS_TIMEOUT_SECONDS}s: {ASSUME_ROLE_DENIED_MESSAGE}. "
        + "Either its trust policy has not converged in IAM yet, or the identity running 'cats infra deploy' is not allowed sts:AssumeRole "
        + "on CATS test roles (the readiness probe assumes the test role from the same credentials 'cats test' uses)."
    )

    # act
    notice = role.advance()

    # assert
    assert (notice, role.phase, role.error) == (None, "failed", expected_error)


def test_explicit_deny_fails_in_the_first_cycle(aws: RoleProbeMocks, clock: dict[str, float]) -> None:
    # arrange - an SCP denies the call: no amount of waiting converges that away
    role = make_role()
    aws.cfn.describe_stacks.side_effect = ClientError(
        {
            "Error": {
                "Code": "AccessDenied",
                "Message": f"User: {TEST_ROLE_ARN} is not authorized to perform: cloudformation:DescribeStacks on resource: {STACK_NAME} "
                + "with an explicit deny in a service control policy",
            }
        },
        "DescribeStacks",
    )
    clock["now"] = 101.0
    expected_error = (
        f"Test role {TEST_ROLE_ARN}: cloudformation:DescribeStacks is explicitly denied: User: {TEST_ROLE_ARN} is not authorized to perform: "
        + f"cloudformation:DescribeStacks on resource: {STACK_NAME} with an explicit deny in a service control policy. "
        + "A service control policy, permissions boundary or resource policy denies it; waiting will not change that."
    )

    # act
    notice = role.advance()

    # assert
    assert (notice, role.phase, role.error) == (None, "failed", expected_error)


def test_other_sts_error_propagates(aws: RoleProbeMocks) -> None:
    # arrange - the environment has no credentials at all: the caller's problem, not the role's
    role = make_role()
    aws.sts.assume_role.side_effect = ClientError(
        {"Error": {"Code": "ExpiredToken", "Message": "The security token included in the request is expired"}}, "AssumeRole"
    )

    # act / assert
    with pytest.raises(ClientError, match="ExpiredToken"):
        role.advance()
    assert role.phase == "probing"


def test_cloudformation_client_is_built_from_the_assumed_credentials_in_the_stack_region(aws: RoleProbeMocks) -> None:
    # act
    make_role().advance()

    # assert
    expected_sessions = [
        call(),
        call(
            region_name=REGION,
            aws_access_key_id=FAKE_ASSUMED_CREDENTIALS["AccessKeyId"],
            aws_secret_access_key=FAKE_ASSUMED_CREDENTIALS["SecretAccessKey"],
            aws_session_token=FAKE_ASSUMED_CREDENTIALS["SessionToken"],
        ),
    ]
    assert aws.attributed_session.call_args_list == expected_sessions


@pytest.mark.parametrize("phase", ["ready", "failed"])
def test_done_role_makes_no_calls(aws: RoleProbeMocks, phase: str) -> None:
    # arrange
    role = make_role()
    role.phase = phase  # type: ignore[assignment]

    # act
    notice = role.advance()

    # assert
    assert (notice, aws.attributed_session.call_count) == (None, 0)


# =============================================================================
# LambdaReadiness.advance
# =============================================================================


def test_invoke_that_returns_makes_the_lambda_invocable_with_a_notice() -> None:
    # arrange - any return proves the function ran, so Lambda assumed the execution role; the payload is discarded
    probe_lambda = make_lambda_readiness()
    lambda_client = MagicMock()
    lambda_client.invoke.return_value = {"StatusCode": 200, "Payload": MagicMock()}

    # act
    notice = probe_lambda.advance(lambda_client)

    # assert
    expected_calls = [call.invoke(FunctionName=PROBE_LAMBDA_NAME, InvocationType="RequestResponse", Payload=GATE_PROBE_EVENT)]
    assert (notice, probe_lambda.phase, probe_lambda.error, lambda_client.method_calls) == (
        f"Probe Lambda {PROBE_LAMBDA_NAME} invocable",
        "ready",
        None,
        expected_calls,
    )


def test_role_not_yet_assumable_keeps_probing() -> None:
    # arrange - Lambda cannot assume the freshly created execution role yet
    probe_lambda = make_lambda_readiness()
    lambda_client = MagicMock()
    lambda_client.invoke.side_effect = LAMBDA_CANNOT_BE_ASSUMED

    # act
    notice = probe_lambda.advance(lambda_client)

    # assert
    assert (notice, probe_lambda.phase, probe_lambda.error) == (None, "probing", None)


def test_role_not_assumable_past_the_bound_fails_naming_both_causes(clock: dict[str, float]) -> None:
    # arrange
    probe_lambda = make_lambda_readiness()
    lambda_client = MagicMock()
    lambda_client.invoke.side_effect = LAMBDA_CANNOT_BE_ASSUMED
    clock["now"] = 100.0 + READINESS_TIMEOUT_SECONDS
    expected_error = (
        f"Probe Lambda {PROBE_LAMBDA_NAME}'s execution role could not be assumed by Lambda after {READINESS_TIMEOUT_SECONDS}s: "
        + f"{LAMBDA_CANNOT_BE_ASSUMED_MESSAGE}. Either its trust policy has not converged in IAM yet, or a service control policy "
        + "or permissions boundary prevents Lambda from assuming it."
    )

    # act
    notice = probe_lambda.advance(lambda_client)

    # assert
    assert (notice, probe_lambda.phase, probe_lambda.error) == (None, "failed", expected_error)


def test_lambda_other_access_denied_propagates() -> None:
    # arrange - the identity lacking lambda:InvokeFunction is not the execution-role race: it must not be waited out
    probe_lambda = make_lambda_readiness()
    lambda_client = MagicMock()
    lambda_client.invoke.side_effect = LAMBDA_INVOKE_NOT_AUTHORIZED

    # act / assert
    with pytest.raises(ClientError, match="lambda:InvokeFunction"):
        probe_lambda.advance(lambda_client)
    assert probe_lambda.phase == "probing"


def test_lambda_other_client_error_propagates() -> None:
    # arrange - the function was deleted out from under the gate: the caller's or service's problem, not convergence
    probe_lambda = make_lambda_readiness()
    lambda_client = MagicMock()
    lambda_client.invoke.side_effect = ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "Function not found"}}, "Invoke")

    # act / assert
    with pytest.raises(ClientError, match="ResourceNotFoundException"):
        probe_lambda.advance(lambda_client)
    assert probe_lambda.phase == "probing"


@pytest.mark.parametrize("phase", ["ready", "failed"])
def test_done_lambda_makes_no_calls(phase: str) -> None:
    # arrange
    probe_lambda = make_lambda_readiness()
    probe_lambda.phase = phase  # type: ignore[assignment]
    lambda_client = MagicMock()

    # act
    notice = probe_lambda.advance(lambda_client)

    # assert
    assert (notice, lambda_client.method_calls) == (None, [])


# =============================================================================
# lambdas_from_outputs
# =============================================================================


def test_lambdas_from_outputs_takes_one_per_probe_name_output_in_order() -> None:
    # arrange
    outputs: list[Any] = [
        {"OutputKey": "TestRoleArn", "OutputValue": "arn:aws:iam::111111111111:role/CATS-test"},
        {"OutputKey": "ProbeLambdaNameprivate", "OutputValue": "fn-private"},
        {"OutputKey": "Ec2InstanceIdprivate", "OutputValue": "i-0aaa"},
        {"OutputKey": "ProbeLambdaNamepublic", "OutputValue": "fn-public"},
    ]
    expected = ["fn-private", "fn-public"]

    # act
    lambdas = lambdas_from_outputs(outputs)

    # assert
    assert expected == [probe_lambda.function_name for probe_lambda in lambdas]


def test_lambdas_from_outputs_without_probe_names_is_empty() -> None:
    # arrange - an ec2-only stack has listeners and a test role, no probe functions
    outputs: list[Any] = [
        {"OutputKey": "TestRoleArn", "OutputValue": "arn:aws:iam::111111111111:role/CATS-test"},
        {"OutputKey": "Ec2InstanceIdprivate", "OutputValue": "i-0aaa"},
    ]

    # act
    lambdas = lambdas_from_outputs(outputs)

    # assert
    assert lambdas == []


# =============================================================================
# StackReadiness
# =============================================================================


def test_stack_readiness_advances_every_unfinished_listener_and_collects_notices() -> None:
    # arrange - one listener is already ready; of the other two, one becomes ready on this step
    ready = ListenerReadiness(instance_id="i-0aaa", target_name="a", started_at=100.0)
    ready.phase = "ready"
    stack = StackReadiness([ready, ListenerReadiness("i-0bbb", "b", started_at=100.0), ListenerReadiness("i-0ccc", "c", started_at=100.0)])
    consoles = {"i-0bbb": CONSOLE_READY, "i-0ccc": CONSOLE_BOOTING}
    ec2 = MagicMock()
    ec2.get_console_output.side_effect = lambda **kwargs: {"InstanceId": kwargs["InstanceId"], "Output": consoles[kwargs["InstanceId"]]}

    # act
    notices = stack.advance(ec2)

    # assert
    expected_calls = [call(InstanceId="i-0bbb", Latest=True), call(InstanceId="i-0ccc", Latest=True)]
    assert (notices, ec2.get_console_output.call_args_list, stack.is_done) == (["EC2 listener i-0bbb (b) ready"], expected_calls, False)


def test_stack_readiness_is_done_when_every_listener_is_ready() -> None:
    # arrange
    listeners = [ListenerReadiness("i-0aaa", "a"), ListenerReadiness("i-0bbb", "b")]
    for listener in listeners:
        listener.phase = "ready"

    # act
    stack = StackReadiness(listeners)

    # assert
    assert (stack.is_done, stack.failure) == (True, None)


def test_stack_readiness_is_done_with_the_first_failure_while_others_still_boot() -> None:
    # arrange - one failure decides the stack; the listener still booting is not waited for
    ready = ListenerReadiness("i-0aaa", "a")
    ready.phase = "ready"
    failed = ListenerReadiness("i-0bbb", "b")
    failed.phase = "failed"
    failed.error = "EC2 listener i-0bbb (b) is not ready after 180s: it did not report ready within 180s"
    still_booting = ListenerReadiness("i-0ccc", "c")

    # act
    stack = StackReadiness([ready, failed, still_booting])

    # assert
    expected = ReadinessFailure(kind="listener", detail="EC2 listener i-0bbb (b) is not ready after 180s: it did not report ready within 180s")
    assert (stack.is_done, stack.failure) == (True, expected)


def test_stack_readiness_is_not_done_while_a_listener_boots() -> None:
    # arrange
    ready = ListenerReadiness("i-0aaa", "a")
    ready.phase = "ready"

    # act
    stack = StackReadiness([ready, ListenerReadiness("i-0bbb", "b")])

    # assert
    assert (stack.is_done, stack.failure) == (False, None)


def test_stack_readiness_is_not_done_while_a_role_converges_after_every_listener_is_ready() -> None:
    # arrange
    listener = ListenerReadiness("i-0aaa", "a")
    listener.phase = "ready"

    # act
    stack = StackReadiness([listener], roles=[make_role()])

    # assert
    assert (stack.is_done, stack.failure) == (False, None)


def test_stack_readiness_is_not_done_while_a_listener_boots_after_every_role_is_ready() -> None:
    # arrange
    role = make_role()
    role.phase = "ready"

    # act
    stack = StackReadiness([ListenerReadiness("i-0aaa", "a")], roles=[role])

    # assert
    assert (stack.is_done, stack.failure) == (False, None)


def test_stack_readiness_reports_a_role_failure_before_a_listener_failure() -> None:
    # arrange - both failed in the same cycle; the role is reported first because its remediation differs
    role = make_role()
    role.phase = "failed"
    role.error = "Test role arn could not be assumed after 180s"
    listener = ListenerReadiness("i-0aaa", "a")
    listener.phase = "failed"
    listener.error = "EC2 listener i-0aaa (a) is not ready after 180s"

    # act
    stack = StackReadiness([listener], roles=[role])

    # assert
    assert (stack.is_done, stack.failure) == (True, ReadinessFailure(kind="role", detail="Test role arn could not be assumed after 180s"))


def test_stack_readiness_advances_roles_and_listeners_in_one_cycle(aws: RoleProbeMocks) -> None:
    # arrange - the role authorizes and one listener becomes ready on this step
    stack = StackReadiness([ListenerReadiness("i-0bbb", "b", started_at=100.0)], roles=[make_role()])
    ec2 = make_ec2(CONSOLE_READY)

    # act
    notices = stack.advance(ec2)

    # assert
    expected_notices = ["Test role CATS-test-infra-testaccount-default-test-role authorized", "EC2 listener i-0bbb (b) ready"]
    assert (notices, stack.is_done) == (expected_notices, True)


def test_stack_readiness_is_not_done_while_a_lambda_probes_after_role_and_listeners_ready() -> None:
    # arrange
    listener = ListenerReadiness("i-0aaa", "a")
    listener.phase = "ready"
    role = make_role()
    role.phase = "ready"

    # act
    stack = StackReadiness([listener], roles=[role], lambdas=[make_lambda_readiness()])

    # assert
    assert (stack.is_done, stack.failure) == (False, None)


def test_stack_readiness_reports_a_role_failure_before_a_lambda_failure() -> None:
    # arrange - both failed in the same cycle; the role is reported first because its remediation differs
    role = make_role()
    role.phase = "failed"
    role.error = "Test role arn could not be assumed after 180s"
    probe_lambda = make_lambda_readiness()
    probe_lambda.phase = "failed"
    probe_lambda.error = "Probe Lambda fn's execution role could not be assumed by Lambda after 180s"

    # act
    stack = StackReadiness([], roles=[role], lambdas=[probe_lambda])

    # assert
    assert stack.failure == ReadinessFailure(kind="role", detail="Test role arn could not be assumed after 180s")


def test_stack_readiness_reports_a_lambda_failure_before_a_listener_failure() -> None:
    # arrange - a lambda and a listener failed in the same cycle; the lambda is reported first
    probe_lambda = make_lambda_readiness()
    probe_lambda.phase = "failed"
    probe_lambda.error = "Probe Lambda fn's execution role could not be assumed by Lambda after 180s"
    listener = ListenerReadiness("i-0aaa", "a")
    listener.phase = "failed"
    listener.error = "EC2 listener i-0aaa (a) is not ready after 180s"

    # act
    stack = StackReadiness([listener], lambdas=[probe_lambda])

    # assert
    expected = ReadinessFailure(kind="lambda", detail="Probe Lambda fn's execution role could not be assumed by Lambda after 180s")
    assert stack.failure == expected


def test_stack_readiness_advances_roles_lambdas_and_listeners_in_one_cycle(aws: RoleProbeMocks) -> None:
    # arrange - the role authorizes, the probe is invocable, and one listener becomes ready on this step
    stack = StackReadiness([ListenerReadiness("i-0bbb", "b", started_at=100.0)], roles=[make_role()], lambdas=[make_lambda_readiness()])
    ec2 = make_ec2(CONSOLE_READY)
    lambda_client = MagicMock()
    lambda_client.invoke.return_value = {"StatusCode": 200, "Payload": MagicMock()}

    # act
    notices = stack.advance(ec2, lambda_client)

    # assert - roles, then lambdas, then listeners
    expected_notices = [
        "Test role CATS-test-infra-testaccount-default-test-role authorized",
        f"Probe Lambda {PROBE_LAMBDA_NAME} invocable",
        "EC2 listener i-0bbb (b) ready",
    ]
    assert (notices, stack.is_done) == (expected_notices, True)
