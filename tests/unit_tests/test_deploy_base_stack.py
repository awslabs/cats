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

"""Tests for tests.integration.infra.deploy_base_stack.

The script runs in CI before the integration suite, where a silent failure would mean tests
running against a stale base stack. These cover the outcomes it has to tell apart: no stack
yet, a stack to update, a template that already matches, a deployed version newer than this
branch's, another pipeline mid-write, and a deploy that genuinely failed - and, with a cross
account, the order the two peered stacks converge in and what each pass hands the next.
"""

from collections.abc import Callable
from itertools import cycle
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError, NoCredentialsError, WaiterError
from pytest_mock import MockerFixture

from tests.integration.infra.deploy_base_stack import (
    CROSS_ACCOUNT_VARIABLE,
    CROSS_FALLBACK_STACK_NAME,
    CROSS_STACK,
    CROSS_TEMPLATE_PATH,
    CROSS_VPC_ID_PARAMETER,
    FALLBACK_STACK_NAME,
    HOME_STACK,
    NO_UPDATES_MESSAGE,
    TEMPLATE_PATH,
    VPC_ID_PARAMETER,
    AccountClients,
    BaseStackBusyError,
    BaseStackOutputError,
    converge,
    deploy_peered,
    description_version,
    enable_dns_resolution,
    find_stack_name,
    main,
    stack_parameters,
    template_version,
)

DISCOVERED_STACK_NAME = "cats-integ-vpc-handmade"
REPO_ROOT = Path(__file__).parent.parent.parent
# Read from the templates so a version bump does not need these tests edited.
LOCAL_VERSION = template_version(TEMPLATE_PATH.read_text(encoding="utf-8"))
CROSS_LOCAL_VERSION = template_version(CROSS_TEMPLATE_PATH.read_text(encoding="utf-8"))
HOME_ACCOUNT = "1" * 12
CROSS_ACCOUNT = "2" * 12
PEERING_ID = "pcx-0123456789abcdef0"
CROSS_OUTPUTS = {
    "VpcId": "vpc-0cross",
    "SubnetId": "subnet-0cross",
    "VpcCidr": "10.97.0.0/16",
    "PeeringAcceptRoleArn": f"arn:aws:iam::{CROSS_ACCOUNT}:role/CATS-integ-peering-accept-role",
}
HOME_OUTPUTS = {"VpcId": "vpc-0home", "PeeringConnectionId": PEERING_ID}


def stack_status_response(
    status: str,
    description: str | None = None,
    parameters: dict[str, str] | None = None,
    outputs: dict[str, str] | None = None,
) -> dict[str, Any]:
    """A describe_stacks response carrying one status, Description, parameter set and outputs.

    The Description defaults to the version the local home template declares, which is the
    equal-version case: the script attempts an update and lets CloudFormation decide.
    """
    if description is None:
        description = f"Persistent VPC for CATS integration tests (v{LOCAL_VERSION})"
    stack: dict[str, Any] = {"StackStatus": status, "Description": description}
    if parameters is not None:
        stack["Parameters"] = [{"ParameterKey": key, "ParameterValue": value} for key, value in parameters.items()]
    if outputs is not None:
        stack["Outputs"] = [{"OutputKey": key, "OutputValue": value} for key, value in outputs.items()]
    return {"Stacks": [stack]}


def make_validation_error(message: str) -> ClientError:
    """A ClientError shaped like the ValidationError CloudFormation returns."""
    return ClientError({"Error": {"Code": "ValidationError", "Message": message}}, "OperationName")


def peering_response(requester_dns: bool, accepter_dns: bool) -> dict[str, Any]:
    """A describe_vpc_peering_connections response with each side's DNS option as given."""
    return {
        "VpcPeeringConnections": [
            {
                "VpcPeeringConnectionId": PEERING_ID,
                "RequesterVpcInfo": {"PeeringOptions": {"AllowDnsResolutionFromRemoteVpc": requester_dns}},
                "AccepterVpcInfo": {"PeeringOptions": {"AllowDnsResolutionFromRemoteVpc": accepter_dns}},
            }
        ]
    }


@pytest.fixture
def mock_cloudformation() -> MagicMock:
    """CloudFormation client whose parameter lookup finds a settled DISCOVERED_STACK_NAME."""
    mock = MagicMock()
    mock.describe_stack_resources.return_value = {"StackResources": [{"StackName": DISCOVERED_STACK_NAME}]}
    mock.describe_stacks.return_value = stack_status_response("UPDATE_COMPLETE")
    return mock


@pytest.fixture
def mock_missing_stack(mock_cloudformation: MagicMock) -> MagicMock:
    """CloudFormation client for a fresh account: nothing owns the VPC ID parameter."""
    mock_cloudformation.describe_stack_resources.side_effect = make_validation_error("Stack for /cats/integ-test/vpc-id does not exist")
    return mock_cloudformation


@pytest.fixture
def mock_no_sleep(mocker: MockerFixture) -> MagicMock:
    """Skip the settle poll interval so tests that wait still run in milliseconds."""
    mock: MagicMock = mocker.patch("tests.integration.infra.deploy_base_stack.time.sleep")
    return mock


@pytest.fixture
def home_cloudformation() -> MagicMock:
    """Home account CloudFormation whose base stack already matches, with a peering in its outputs."""
    mock = MagicMock()
    mock.describe_stack_resources.return_value = {"StackResources": [{"StackName": FALLBACK_STACK_NAME}]}
    mock.describe_stacks.return_value = stack_status_response("UPDATE_COMPLETE", outputs=HOME_OUTPUTS)
    mock.update_stack.side_effect = make_validation_error(NO_UPDATES_MESSAGE)
    return mock


@pytest.fixture
def home_ec2() -> MagicMock:
    """Home account EC2 describing a peering with DNS resolution already on both sides."""
    mock = MagicMock()
    mock.describe_vpc_peering_connections.return_value = peering_response(requester_dns=True, accepter_dns=True)
    return mock


@pytest.fixture
def home(home_cloudformation: MagicMock, home_ec2: MagicMock) -> AccountClients:
    return AccountClients(account_id=HOME_ACCOUNT, cloudformation=home_cloudformation, ec2=home_ec2)


@pytest.fixture
def cross_cloudformation() -> MagicMock:
    """Cross account CloudFormation whose base stack already matches."""
    mock = MagicMock()
    mock.describe_stack_resources.return_value = {"StackResources": [{"StackName": CROSS_FALLBACK_STACK_NAME}]}
    mock.describe_stacks.return_value = stack_status_response(
        "UPDATE_COMPLETE",
        description=f"Persistent VPC in the cross account for CATS integration tests (v{CROSS_LOCAL_VERSION})",
        outputs=CROSS_OUTPUTS,
    )
    mock.update_stack.side_effect = make_validation_error(NO_UPDATES_MESSAGE)
    return mock


@pytest.fixture
def cross_ec2() -> MagicMock:
    mock = MagicMock()
    mock.describe_vpc_peering_connections.return_value = peering_response(requester_dns=True, accepter_dns=True)
    return mock


@pytest.fixture
def cross(cross_cloudformation: MagicMock, cross_ec2: MagicMock) -> AccountClients:
    return AccountClients(account_id=CROSS_ACCOUNT, cloudformation=cross_cloudformation, ec2=cross_ec2)


@pytest.fixture
def mock_session(mocker: MockerFixture, mock_cloudformation: MagicMock) -> MagicMock:
    """boto3.Session whose clients are the home account's, with STS answering HOME_ACCOUNT."""
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": HOME_ACCOUNT}
    sts.assume_role.return_value = {"Credentials": {"AccessKeyId": "AKIA", "SecretAccessKey": "secret", "SessionToken": "token"}}
    clients = {"cloudformation": mock_cloudformation, "sts": sts, "ec2": MagicMock()}
    session = MagicMock()
    session.client.side_effect = lambda name: clients[name]
    mocker.patch("tests.integration.infra.deploy_base_stack.boto3.Session", return_value=session)
    return session


# --- Templates and versions ---


def test_template_path_points_at_the_integ_vpc_template() -> None:
    """Resolved from the script's own location so CI's working directory cannot change it."""
    # arrange
    expected = REPO_ROOT / "tests" / "integration" / "infra" / "integ-test-vpc.yaml"

    # act, assert
    assert expected == TEMPLATE_PATH


def test_cross_template_path_points_at_the_cross_vpc_template() -> None:
    # arrange
    expected = REPO_ROOT / "tests" / "integration" / "infra" / "integ-test-cross-vpc.yaml"

    # act, assert
    assert expected == CROSS_TEMPLATE_PATH


def test_version_marker_read_from_the_description() -> None:
    # act
    result = description_version("Persistent VPC for CATS integration tests (v7)")

    # assert
    assert result == 7


def test_description_without_a_marker_is_version_one() -> None:
    """Marks the stack deployed before versioning existed as the oldest possible version."""
    # act
    result = description_version("Persistent VPC for CATS integration tests")

    # assert
    assert result == 1


def test_cross_template_declares_a_version() -> None:
    """The cross stack is ordered by the same marker as the home one."""
    # act, assert
    assert CROSS_LOCAL_VERSION >= 1


def test_only_the_cross_stack_declares_iam_capability() -> None:
    """The cross template names an IAM role; the home template declares no IAM resources."""
    # act, assert
    assert (HOME_STACK.capabilities, CROSS_STACK.capabilities) == ((), ("CAPABILITY_NAMED_IAM",))


# --- Parameters ---


def test_create_parameters_are_the_explicit_values_only() -> None:
    # arrange
    expected = [{"ParameterKey": "HomeAccountId", "ParameterValue": HOME_ACCOUNT}]

    # act
    result = stack_parameters({"HomeAccountId": HOME_ACCOUNT}, None)

    # assert
    assert expected == result


def test_update_carries_forward_deployed_parameters_not_set() -> None:
    """A single-account run must not converge the peering parameters back to their defaults."""
    # arrange
    deployed = stack_status_response("UPDATE_COMPLETE", parameters={"PeerVpcId": "vpc-0cross", "PeerCidr": "10.97.0.0/16"})["Stacks"][0]
    expected = [{"ParameterKey": "PeerVpcId", "UsePreviousValue": True}, {"ParameterKey": "PeerCidr", "UsePreviousValue": True}]

    # act
    result = stack_parameters({}, deployed)

    # assert
    assert expected == result


def test_update_explicit_value_replaces_the_deployed_one() -> None:
    # arrange
    deployed = stack_status_response("UPDATE_COMPLETE", parameters={"PeerVpcId": "vpc-0old", "PeerCidr": "10.97.0.0/16"})["Stacks"][0]
    expected = [{"ParameterKey": "PeerVpcId", "ParameterValue": "vpc-0new"}, {"ParameterKey": "PeerCidr", "UsePreviousValue": True}]

    # act
    result = stack_parameters({"PeerVpcId": "vpc-0new"}, deployed)

    # assert
    assert expected == result


def test_update_of_a_stack_without_parameters_passes_none() -> None:
    """The v5 home stack has no parameters; UsePreviousValue for a key it lacks would be rejected."""
    # arrange
    deployed = stack_status_response("UPDATE_COMPLETE")["Stacks"][0]

    # act
    result = stack_parameters({}, deployed)

    # assert
    assert result == []


# --- Discovery ---


def test_stack_lookup_uses_the_discovery_parameter_physical_id(mock_cloudformation: MagicMock) -> None:
    # arrange
    expected = {"PhysicalResourceId": CROSS_VPC_ID_PARAMETER}

    # act
    find_stack_name(mock_cloudformation, CROSS_VPC_ID_PARAMETER)

    # assert
    assert expected == mock_cloudformation.describe_stack_resources.call_args.kwargs


def test_owning_stack_is_discovered_by_name(mock_cloudformation: MagicMock) -> None:
    # act
    result = find_stack_name(mock_cloudformation, VPC_ID_PARAMETER)

    # assert
    assert result == DISCOVERED_STACK_NAME


def test_unknown_parameter_reports_no_stack_name(mock_missing_stack: MagicMock) -> None:
    # act
    result = find_stack_name(mock_missing_stack, VPC_ID_PARAMETER)

    # assert
    assert result is None


# --- converge: one stack ---


def test_older_local_template_leaves_the_newer_stack_alone(mock_cloudformation: MagicMock) -> None:
    """A stale branch must not converge the shared stack backward under a newer pipeline."""
    # arrange
    newer = f"Persistent VPC for CATS integration tests (v{LOCAL_VERSION + 1})"
    mock_cloudformation.describe_stacks.return_value = stack_status_response("UPDATE_COMPLETE", newer)

    # act
    converge(mock_cloudformation, HOME_STACK, {})

    # assert
    assert mock_cloudformation.update_stack.call_args_list == []


def test_older_local_template_returns_the_newer_stack_outputs(mock_cloudformation: MagicMock) -> None:
    """The peered flow still needs the outputs of a stack it did not touch."""
    # arrange
    newer = f"Persistent VPC for CATS integration tests (v{LOCAL_VERSION + 1})"
    mock_cloudformation.describe_stacks.return_value = stack_status_response("UPDATE_COMPLETE", newer, outputs=HOME_OUTPUTS)

    # act
    result = converge(mock_cloudformation, HOME_STACK, {})

    # assert
    assert result == HOME_OUTPUTS


def test_newer_local_template_updates_the_older_stack(mock_cloudformation: MagicMock) -> None:
    # arrange
    older = f"Persistent VPC for CATS integration tests (v{LOCAL_VERSION - 1})"
    mock_cloudformation.describe_stacks.return_value = stack_status_response("UPDATE_COMPLETE", older)

    # act
    converge(mock_cloudformation, HOME_STACK, {})

    # assert
    assert mock_cloudformation.update_stack.call_count == 1


def test_unversioned_deployed_stack_is_updated(mock_cloudformation: MagicMock) -> None:
    """The hand-deployed stack carries no marker, so this branch's template is newer."""
    # arrange
    mock_cloudformation.describe_stacks.return_value = stack_status_response("UPDATE_COMPLETE", "Persistent VPC for CATS integration tests")

    # act
    converge(mock_cloudformation, HOME_STACK, {})

    # assert
    assert mock_cloudformation.update_stack.call_count == 1


def test_no_existing_stack_creates_under_the_fallback_name(mock_missing_stack: MagicMock) -> None:
    # arrange
    expected = {
        "StackName": FALLBACK_STACK_NAME,
        "TemplateBody": TEMPLATE_PATH.read_text(encoding="utf-8"),
        "Parameters": [],
        "Capabilities": [],
    }

    # act
    converge(mock_missing_stack, HOME_STACK, {})

    # assert
    assert expected == mock_missing_stack.create_stack.call_args.kwargs


def test_no_existing_cross_stack_creates_with_its_parameters_and_capability(mock_missing_stack: MagicMock) -> None:
    # arrange
    expected = {
        "StackName": CROSS_FALLBACK_STACK_NAME,
        "TemplateBody": CROSS_TEMPLATE_PATH.read_text(encoding="utf-8"),
        "Parameters": [{"ParameterKey": "HomeAccountId", "ParameterValue": HOME_ACCOUNT}],
        "Capabilities": ["CAPABILITY_NAMED_IAM"],
    }

    # act
    converge(mock_missing_stack, CROSS_STACK, {"HomeAccountId": HOME_ACCOUNT})

    # assert
    assert expected == mock_missing_stack.create_stack.call_args.kwargs


def test_no_existing_stack_waits_for_create_complete(mock_missing_stack: MagicMock) -> None:
    # arrange
    expected = (("stack_create_complete",), {"StackName": FALLBACK_STACK_NAME})

    # act
    converge(mock_missing_stack, HOME_STACK, {})

    # assert
    waiter = mock_missing_stack.get_waiter
    assert expected == (waiter.call_args.args, waiter.return_value.wait.call_args.kwargs)


def test_created_stack_outputs_are_returned(mock_missing_stack: MagicMock) -> None:
    # arrange
    mock_missing_stack.describe_stacks.return_value = stack_status_response("CREATE_COMPLETE", outputs=CROSS_OUTPUTS)

    # act
    result = converge(mock_missing_stack, CROSS_STACK, {"HomeAccountId": HOME_ACCOUNT})

    # assert
    assert result == CROSS_OUTPUTS


def test_existing_stack_updates_under_the_discovered_name(mock_cloudformation: MagicMock) -> None:
    # arrange
    expected = {
        "StackName": DISCOVERED_STACK_NAME,
        "TemplateBody": TEMPLATE_PATH.read_text(encoding="utf-8"),
        "Parameters": [],
        "Capabilities": [],
    }

    # act
    converge(mock_cloudformation, HOME_STACK, {})

    # assert
    assert expected == mock_cloudformation.update_stack.call_args.kwargs


def test_existing_stack_update_carries_its_parameters_forward(mock_cloudformation: MagicMock) -> None:
    # arrange
    mock_cloudformation.describe_stacks.return_value = stack_status_response("UPDATE_COMPLETE", parameters={"PeerVpcId": "vpc-0cross"})
    expected = [{"ParameterKey": "PeerVpcId", "UsePreviousValue": True}]

    # act
    converge(mock_cloudformation, HOME_STACK, {})

    # assert
    assert expected == mock_cloudformation.update_stack.call_args.kwargs["Parameters"]


def test_existing_stack_waits_for_update_complete(mock_cloudformation: MagicMock) -> None:
    # arrange
    expected = (("stack_update_complete",), {"StackName": DISCOVERED_STACK_NAME})

    # act
    converge(mock_cloudformation, HOME_STACK, {})

    # assert
    waiter = mock_cloudformation.get_waiter
    assert expected == (waiter.call_args.args, waiter.return_value.wait.call_args.kwargs)


def test_updated_stack_outputs_are_read_after_the_update(mock_cloudformation: MagicMock) -> None:
    """The peering ID exists only once the update has run, so the pre-update describe will not do."""
    # arrange
    mock_cloudformation.describe_stacks.side_effect = [
        stack_status_response("UPDATE_COMPLETE", outputs={"VpcId": "vpc-0home"}),
        stack_status_response("UPDATE_COMPLETE", outputs=HOME_OUTPUTS),
    ]

    # act
    result = converge(mock_cloudformation, HOME_STACK, {})

    # assert
    assert result == HOME_OUTPUTS


def test_template_matching_the_stack_waits_for_nothing(mock_cloudformation: MagicMock) -> None:
    """CloudFormation rejects a no-change update, which is the clean no-op case."""
    # arrange
    mock_cloudformation.update_stack.side_effect = make_validation_error(NO_UPDATES_MESSAGE)

    # act
    converge(mock_cloudformation, HOME_STACK, {})

    # assert
    assert mock_cloudformation.get_waiter.call_args_list == []


def test_template_matching_the_stack_returns_its_outputs(mock_cloudformation: MagicMock) -> None:
    # arrange
    mock_cloudformation.describe_stacks.return_value = stack_status_response("UPDATE_COMPLETE", outputs=HOME_OUTPUTS)
    mock_cloudformation.update_stack.side_effect = make_validation_error(NO_UPDATES_MESSAGE)

    # act
    result = converge(mock_cloudformation, HOME_STACK, {})

    # assert
    assert result == HOME_OUTPUTS


def test_rejected_update_on_a_settled_stack_raises(mock_cloudformation: MagicMock) -> None:
    """A rejection with nothing running is a real problem and must not be swallowed."""
    # arrange
    mock_cloudformation.update_stack.side_effect = make_validation_error("Circular dependency between resources")

    # act, assert
    with pytest.raises(ClientError):
        converge(mock_cloudformation, HOME_STACK, {})


def test_stack_already_being_written_settles_before_deciding(mock_cloudformation: MagicMock, mock_no_sleep: MagicMock) -> None:
    """No update is asked for until the other pipeline's operation has finished."""
    # arrange - busy on the first look, settled on the second
    mock_cloudformation.describe_stacks.side_effect = [
        stack_status_response("UPDATE_IN_PROGRESS"),
        stack_status_response("UPDATE_COMPLETE"),
        stack_status_response("UPDATE_COMPLETE"),
    ]

    # act
    converge(mock_cloudformation, HOME_STACK, {})

    # assert - one settle wait happened, and only then the single update
    assert (mock_no_sleep.call_count, mock_cloudformation.update_stack.call_count) == (1, 1)


def test_stack_settling_into_the_wanted_template_does_nothing(mock_cloudformation: MagicMock, mock_no_sleep: MagicMock) -> None:
    """The other pipeline may have deployed this same template, leaving nothing to do."""
    # arrange
    mock_cloudformation.describe_stacks.side_effect = [
        stack_status_response("UPDATE_IN_PROGRESS"),
        stack_status_response("UPDATE_COMPLETE"),
    ]
    mock_cloudformation.update_stack.side_effect = make_validation_error(NO_UPDATES_MESSAGE)

    # act
    converge(mock_cloudformation, HOME_STACK, {})

    # assert
    assert mock_cloudformation.get_waiter.call_args_list == []


def test_update_claimed_by_another_writer_is_retried(mock_cloudformation: MagicMock, mock_no_sleep: MagicMock) -> None:
    """A write that started in the window since settling is not a failure - wait and retry."""
    # arrange - rejected while in progress, accepted on the second attempt
    mock_cloudformation.describe_stacks.side_effect = [
        stack_status_response("UPDATE_COMPLETE"),
        stack_status_response("UPDATE_IN_PROGRESS"),
        stack_status_response("UPDATE_COMPLETE"),
        stack_status_response("UPDATE_COMPLETE"),
    ]
    mock_cloudformation.update_stack.side_effect = [
        make_validation_error(f"Stack {DISCOVERED_STACK_NAME} is in UPDATE_IN_PROGRESS state and can not be updated"),
        None,
    ]

    # act
    converge(mock_cloudformation, HOME_STACK, {})

    # assert
    expected = (("stack_update_complete",), {"StackName": DISCOVERED_STACK_NAME})
    waiter = mock_cloudformation.get_waiter
    assert expected == (waiter.call_args.args, waiter.return_value.wait.call_args.kwargs)


def test_update_claimed_by_a_writer_that_converged_exits_clean(mock_cloudformation: MagicMock, mock_no_sleep: MagicMock) -> None:
    """The retry finds the other writer left the stack matching this template."""
    # arrange
    mock_cloudformation.describe_stacks.side_effect = [
        stack_status_response("UPDATE_COMPLETE"),
        stack_status_response("UPDATE_IN_PROGRESS"),
        stack_status_response("UPDATE_COMPLETE"),
    ]
    mock_cloudformation.update_stack.side_effect = [
        make_validation_error(f"Stack {DISCOVERED_STACK_NAME} is in UPDATE_IN_PROGRESS state and can not be updated"),
        make_validation_error(NO_UPDATES_MESSAGE),
    ]

    # act
    converge(mock_cloudformation, HOME_STACK, {})

    # assert
    assert mock_cloudformation.get_waiter.call_args_list == []


def test_stack_claimed_on_every_attempt_raises(mock_cloudformation: MagicMock, mock_no_sleep: MagicMock) -> None:
    """Losing the race repeatedly means something is wrong with the account, so fail loudly."""
    # arrange - settled when asked, in progress the moment an update is attempted
    mock_cloudformation.describe_stacks.side_effect = cycle([stack_status_response("UPDATE_COMPLETE"), stack_status_response("UPDATE_IN_PROGRESS")])
    mock_cloudformation.update_stack.side_effect = make_validation_error(f"Stack {DISCOVERED_STACK_NAME} is in UPDATE_IN_PROGRESS state")

    # act, assert
    with pytest.raises(BaseStackBusyError):
        converge(mock_cloudformation, HOME_STACK, {})


# --- deploy_peered: both stacks and the peering ---


def test_peered_converges_cross_then_home_then_cross(
    home: AccountClients,
    cross: AccountClients,
    home_cloudformation: MagicMock,
    cross_cloudformation: MagicMock,
) -> None:
    """The cycle's fixed order: the home peering needs the cross outputs, the cross route needs the peering."""
    # arrange
    order: list[str] = []

    def record(name: str) -> Callable[..., None]:
        def no_change(**kwargs: object) -> None:
            order.append(name)
            raise make_validation_error(NO_UPDATES_MESSAGE)

        return no_change

    cross_cloudformation.update_stack.side_effect = record("cross")
    home_cloudformation.update_stack.side_effect = record("home")

    # act
    deploy_peered(home, cross)

    # assert
    assert order == ["cross", "home", "cross"]


def test_peered_home_parameters_come_from_the_cross_outputs(
    home: AccountClients,
    cross: AccountClients,
    home_cloudformation: MagicMock,
) -> None:
    # arrange
    expected = [
        {"ParameterKey": "PeerVpcId", "ParameterValue": "vpc-0cross"},
        {"ParameterKey": "PeerOwnerId", "ParameterValue": CROSS_ACCOUNT},
        {"ParameterKey": "PeerRoleArn", "ParameterValue": CROSS_OUTPUTS["PeeringAcceptRoleArn"]},
        {"ParameterKey": "PeerCidr", "ParameterValue": "10.97.0.0/16"},
    ]

    # act
    deploy_peered(home, cross)

    # assert
    assert expected == home_cloudformation.update_stack.call_args.kwargs["Parameters"]


def test_peered_first_cross_pass_carries_the_peering_id_forward(
    home: AccountClients,
    cross: AccountClients,
    cross_cloudformation: MagicMock,
) -> None:
    """Before the home stack has run, the only peering ID the cross stack can have is its own last one."""
    # arrange
    cross_cloudformation.describe_stacks.return_value = stack_status_response(
        "UPDATE_COMPLETE",
        description=f"(v{CROSS_LOCAL_VERSION})",
        parameters={"HomeAccountId": HOME_ACCOUNT, "PeeringConnectionId": PEERING_ID},
        outputs=CROSS_OUTPUTS,
    )
    expected = [
        {"ParameterKey": "HomeAccountId", "ParameterValue": HOME_ACCOUNT},
        {"ParameterKey": "PeeringConnectionId", "UsePreviousValue": True},
    ]

    # act
    deploy_peered(home, cross)

    # assert
    assert expected == cross_cloudformation.update_stack.call_args_list[0].kwargs["Parameters"]


def test_peered_second_cross_pass_sets_the_peering_id_from_the_home_output(
    home: AccountClients,
    cross: AccountClients,
    cross_cloudformation: MagicMock,
) -> None:
    # arrange
    expected = [
        {"ParameterKey": "HomeAccountId", "ParameterValue": HOME_ACCOUNT},
        {"ParameterKey": "PeeringConnectionId", "ParameterValue": PEERING_ID},
    ]

    # act
    deploy_peered(home, cross)

    # assert
    assert expected == cross_cloudformation.update_stack.call_args_list[1].kwargs["Parameters"]


def test_peered_home_without_a_peering_output_raises(
    home: AccountClients,
    cross: AccountClients,
    home_cloudformation: MagicMock,
) -> None:
    """A home stack that converged with no peering leaves the cross stack nothing to route to."""
    # arrange
    home_cloudformation.describe_stacks.return_value = stack_status_response("UPDATE_COMPLETE", outputs={"VpcId": "vpc-0home"})

    # act, assert
    with pytest.raises(BaseStackOutputError):
        deploy_peered(home, cross)


def test_peered_home_without_a_peering_output_runs_no_second_cross_pass(
    home: AccountClients,
    cross: AccountClients,
    home_cloudformation: MagicMock,
    cross_cloudformation: MagicMock,
) -> None:
    # arrange
    home_cloudformation.describe_stacks.return_value = stack_status_response("UPDATE_COMPLETE", outputs={"VpcId": "vpc-0home"})

    # act
    with pytest.raises(BaseStackOutputError):
        deploy_peered(home, cross)

    # assert
    assert cross_cloudformation.update_stack.call_count == 1


def test_peered_cross_without_an_accept_role_output_raises(
    home: AccountClients,
    cross: AccountClients,
    cross_cloudformation: MagicMock,
) -> None:
    # arrange
    cross_cloudformation.describe_stacks.return_value = stack_status_response(
        "UPDATE_COMPLETE",
        description=f"(v{CROSS_LOCAL_VERSION})",
        outputs={"VpcId": "vpc-0cross", "VpcCidr": "10.97.0.0/16"},
    )

    # act, assert
    with pytest.raises(BaseStackOutputError):
        deploy_peered(home, cross)


def test_peered_enables_dns_on_each_side_from_its_own_account(
    home: AccountClients,
    cross: AccountClients,
    home_ec2: MagicMock,
    cross_ec2: MagicMock,
) -> None:
    """Each account can set only its own side's options."""
    # arrange
    home_ec2.describe_vpc_peering_connections.return_value = peering_response(requester_dns=False, accepter_dns=False)
    cross_ec2.describe_vpc_peering_connections.return_value = peering_response(requester_dns=False, accepter_dns=False)
    expected_home = {"VpcPeeringConnectionId": PEERING_ID, "RequesterPeeringConnectionOptions": {"AllowDnsResolutionFromRemoteVpc": True}}
    expected_cross = {"VpcPeeringConnectionId": PEERING_ID, "AccepterPeeringConnectionOptions": {"AllowDnsResolutionFromRemoteVpc": True}}

    # act
    deploy_peered(home, cross)

    # assert
    assert (expected_home, expected_cross) == (
        home_ec2.modify_vpc_peering_connection_options.call_args.kwargs,
        cross_ec2.modify_vpc_peering_connection_options.call_args.kwargs,
    )


def test_dns_already_on_is_left_alone() -> None:
    """A steady-state run makes no write at all."""
    # arrange
    ec2 = MagicMock()
    ec2.describe_vpc_peering_connections.return_value = peering_response(requester_dns=True, accepter_dns=False)

    # act
    enable_dns_resolution(ec2, PEERING_ID, "Requester")

    # assert
    assert ec2.modify_vpc_peering_connection_options.call_args_list == []


def test_dns_off_on_one_side_is_set_on_that_side_only() -> None:
    # arrange
    ec2 = MagicMock()
    ec2.describe_vpc_peering_connections.return_value = peering_response(requester_dns=True, accepter_dns=False)
    expected = {"VpcPeeringConnectionId": PEERING_ID, "AccepterPeeringConnectionOptions": {"AllowDnsResolutionFromRemoteVpc": True}}

    # act
    enable_dns_resolution(ec2, PEERING_ID, "Accepter")

    # assert
    assert expected == ec2.modify_vpc_peering_connection_options.call_args.kwargs


def test_dns_option_absent_from_the_description_counts_as_off() -> None:
    """A peering described without PeeringOptions has never had them set."""
    # arrange
    ec2 = MagicMock()
    ec2.describe_vpc_peering_connections.return_value = {"VpcPeeringConnections": [{"RequesterVpcInfo": {}, "AccepterVpcInfo": {}}]}

    # act
    enable_dns_resolution(ec2, PEERING_ID, "Requester")

    # assert
    assert ec2.modify_vpc_peering_connection_options.call_count == 1


# --- main ---


def test_unset_cross_account_converges_the_home_stack_alone(
    monkeypatch: pytest.MonkeyPatch,
    mock_session: MagicMock,
    mock_cloudformation: MagicMock,
) -> None:
    # arrange
    monkeypatch.delenv(CROSS_ACCOUNT_VARIABLE, raising=False)
    expected = (0, DISCOVERED_STACK_NAME)

    # act
    main()

    # assert
    assert expected == (mock_session.client("sts").assume_role.call_count, mock_cloudformation.update_stack.call_args.kwargs["StackName"])


def test_unset_cross_account_carries_the_deployed_parameters_forward(
    monkeypatch: pytest.MonkeyPatch,
    mock_session: MagicMock,
    mock_cloudformation: MagicMock,
) -> None:
    """The home-only path can never pass the peering parameters' empty defaults."""
    # arrange
    monkeypatch.delenv(CROSS_ACCOUNT_VARIABLE, raising=False)
    mock_cloudformation.describe_stacks.return_value = stack_status_response("UPDATE_COMPLETE", parameters={"PeerVpcId": "vpc-0cross"})
    expected = [{"ParameterKey": "PeerVpcId", "UsePreviousValue": True}]

    # act
    main()

    # assert
    assert expected == mock_cloudformation.update_stack.call_args.kwargs["Parameters"]


def test_cross_account_set_assumes_its_deploy_role(
    monkeypatch: pytest.MonkeyPatch,
    mock_session: MagicMock,
    mock_cloudformation: MagicMock,
) -> None:
    """The same role the tests deploy through, so the script needs no credential of its own."""
    # arrange
    monkeypatch.setenv(CROSS_ACCOUNT_VARIABLE, CROSS_ACCOUNT)
    mock_cloudformation.describe_stacks.return_value = stack_status_response("UPDATE_COMPLETE", outputs={**CROSS_OUTPUTS, **HOME_OUTPUTS})
    mock_cloudformation.update_stack.side_effect = make_validation_error(NO_UPDATES_MESSAGE)
    expected = {"RoleArn": f"arn:aws:iam::{CROSS_ACCOUNT}:role/CATS-integ-deploy-role", "RoleSessionName": "cats-integ-base-stack"}

    # act
    main()

    # assert
    assert expected == mock_session.client("sts").assume_role.call_args.kwargs


def test_cross_account_set_passes_the_home_account_to_the_cross_stack(
    monkeypatch: pytest.MonkeyPatch,
    mock_session: MagicMock,
    mock_cloudformation: MagicMock,
) -> None:
    """The accept role trusts the account STS says the script runs in."""
    # arrange
    monkeypatch.setenv(CROSS_ACCOUNT_VARIABLE, CROSS_ACCOUNT)
    # One client stands in for both accounts here; unversioned so neither template is "older".
    outputs = {**CROSS_OUTPUTS, **HOME_OUTPUTS}
    mock_cloudformation.describe_stacks.return_value = stack_status_response("UPDATE_COMPLETE", "base stack", outputs=outputs)
    mock_cloudformation.update_stack.side_effect = make_validation_error(NO_UPDATES_MESSAGE)
    expected = {"ParameterKey": "HomeAccountId", "ParameterValue": HOME_ACCOUNT}

    # act
    main()

    # assert
    assert expected == mock_cloudformation.update_stack.call_args_list[0].kwargs["Parameters"][0]


def test_missing_output_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    mock_session: MagicMock,
    mock_cloudformation: MagicMock,
) -> None:
    # arrange
    monkeypatch.setenv(CROSS_ACCOUNT_VARIABLE, CROSS_ACCOUNT)
    mock_cloudformation.update_stack.side_effect = make_validation_error(NO_UPDATES_MESSAGE)

    # act
    result = main()

    # assert
    assert result == 1


def test_stack_stuck_in_progress_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    mock_session: MagicMock,
    mock_cloudformation: MagicMock,
    mock_no_sleep: MagicMock,
) -> None:
    """A stack nobody finishes writing fails the job rather than waiting out the whole timeout."""
    # arrange
    monkeypatch.delenv(CROSS_ACCOUNT_VARIABLE, raising=False)
    mock_cloudformation.describe_stacks.return_value = stack_status_response("UPDATE_IN_PROGRESS")

    # act
    result = main()

    # assert
    assert result == 1


def test_failed_update_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    mock_session: MagicMock,
    mock_cloudformation: MagicMock,
) -> None:
    """A stack left in a failed state must fail the CI job, not let the suite run."""
    # arrange
    monkeypatch.delenv(CROSS_ACCOUNT_VARIABLE, raising=False)
    mock_cloudformation.get_waiter.return_value.wait.side_effect = WaiterError(
        name="stack_update_complete",
        reason="Waiter encountered a terminal failure state: UPDATE_ROLLBACK_COMPLETE",
        last_response={},
    )

    # act
    result = main()

    # assert
    assert result == 1


def test_no_credentials_exits_nonzero(monkeypatch: pytest.MonkeyPatch, mock_session: MagicMock) -> None:
    """A shell with no AWS session gets the one-line failure, not botocore's traceback."""
    # arrange
    monkeypatch.delenv(CROSS_ACCOUNT_VARIABLE, raising=False)
    mock_session.client("sts").get_caller_identity.side_effect = NoCredentialsError()

    # act
    result = main()

    # assert
    assert result == 1


def test_settled_stack_exits_zero(monkeypatch: pytest.MonkeyPatch, mock_session: MagicMock) -> None:
    # arrange
    monkeypatch.delenv(CROSS_ACCOUNT_VARIABLE, raising=False)

    # act
    result = main()

    # assert
    assert result == 0
