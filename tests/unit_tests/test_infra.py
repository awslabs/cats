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

"""Tests for cats.cli.infra module - CloudFormation deployment CLI."""

import threading
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError, ParamValidationError
from pytest_mock import MockerFixture
from syrupy.assertion import SnapshotAssertion
from typer.testing import CliRunner

from cats.cfn.deploy import DeployResult
from cats.cli import cats_cli
from cats.cli import infra as infra_module
from cats.cli.infra import console
from cats.cli.infra_ui.progress import MultiAccountProgress
from cats.config import AWS_REGION_UNSET_MESSAGE
from cats.constants import LISTENER_READY_MARKER
from cats.exceptions import CatsConfigError, CatsInfrastructureError
from tests.utils import make_config, make_stack_tags, normalize_aws_ids, normalize_output, normalize_paths, patch_role_probe, write_config


@pytest.fixture(autouse=True)
def mock_current_account(mocker: MockerFixture) -> MagicMock:
    """Mock get_current_account_id to return the test account."""
    return mocker.patch("cats.cfn.session.get_current_account_id", return_value="111111111111")


@pytest.fixture(autouse=True)
def mock_console_not_terminal(mocker: MockerFixture) -> None:
    """Mock console.is_terminal to False to prevent Rich Live from blocking."""
    mocker.patch.object(type(console), "is_terminal", new_callable=mocker.PropertyMock, return_value=False)


def _stub_ec2_describes(mock_client: MagicMock, infra: dict[str, str]) -> None:
    """The one mock stands in for every client; answer the resolver's EC2 lookups for SUBNET_A in VPC."""
    mock_client.describe_subnets.return_value = {
        "Subnets": [{"SubnetId": infra["SUBNET_A"], "AvailabilityZone": "us-east-1a", "VpcId": infra["VPC"]}]
    }
    mock_client.describe_route_tables.return_value = {"RouteTables": [{"RouteTableId": "rtb-main", "Associations": [{"Main": True}]}]}
    mock_client.describe_instance_types.return_value = {"InstanceTypes": [{"ProcessorInfo": {"SupportedArchitectures": ["x86_64"]}}]}
    mock_client.describe_images.return_value = {
        "Images": [
            {"ImageId": "ami-0123456789abcdef0", "Name": "al2023-ami-2023.7.20250301.0-kernel-6.1-x86_64", "CreationDate": "2025-03-01T00:00:00.000Z"}
        ]
    }


def _stub_readiness_gate(mock_client: MagicMock, instance_id: str = "i-0123456789abcdef0") -> None:
    """The one mock stands in for EC2 too: the listener's serial console already carries its ready verdict."""
    mock_client.get_console_output.return_value = {"InstanceId": instance_id, "Output": f"cloud-init[1644]: {LISTENER_READY_MARKER}\n"}


def _stack_id(stack_name: str) -> str:
    return f"arn:aws:cloudformation:us-east-1:111111111111:stack/{stack_name}/abc123"


def _stub_stack_lifecycles(mock_client: MagicMock, outputs_by_stack: dict[str, list[dict[str, str]]]) -> None:
    """describe_stacks says each stack does not exist until create_stack is called, then returns it with its own outputs."""
    created: set[str] = set()

    def create_stack(**kwargs: object) -> dict[str, str]:
        stack_name = str(kwargs["StackName"])
        created.add(stack_name)
        return {"StackId": _stack_id(stack_name)}

    def describe_stacks(**kwargs: object) -> dict[str, object]:
        stack_name = str(kwargs["StackName"])
        if stack_name not in created:
            raise ClientError({"Error": {"Code": "ValidationError", "Message": "Stack does not exist"}}, "DescribeStacks")
        return {"Stacks": [{"StackId": _stack_id(stack_name), "StackStatus": "CREATE_COMPLETE", "Outputs": outputs_by_stack[stack_name]}]}

    mock_client.create_stack.side_effect = create_stack
    mock_client.describe_stacks.side_effect = describe_stacks


def _stub_stack_lifecycle(mock_client: MagicMock, stack_name: str, instance_id: str = "i-0123456789abcdef0") -> None:
    """Every stack a test creates describes with the same one EC2 listener output; `stack_name` names the fixture's usual one."""
    listener_output = [{"OutputKey": "Ec2InstanceIdprivate", "OutputValue": instance_id}]
    _stub_stack_lifecycles(mock_client, defaultdict(lambda: listener_output, {stack_name: listener_output}))


@pytest.fixture
def mock_cfn_success(mocker: MockerFixture, mock_aws_infra: dict[str, str]) -> MagicMock:
    """Mock CloudFormation client for successful operations, through to the EC2 listener being ready."""
    mock_cfn = MagicMock()
    _stub_ec2_describes(mock_cfn, mock_aws_infra)
    _stub_stack_lifecycle(mock_cfn, "CATS-test-infra-testaccount")
    _stub_readiness_gate(mock_cfn)
    mock_cfn.describe_stack_events.return_value = {
        "StackEvents": [
            {
                "EventId": "event-1",
                "Timestamp": datetime.now(UTC),
                "LogicalResourceId": "CATS-test-infra-testaccount",
                "ResourceType": "AWS::CloudFormation::Stack",
                "ResourceStatus": "CREATE_COMPLETE",
                "ResourceStatusReason": None,
            }
        ]
    }
    mocker.patch("boto3.Session.client", return_value=mock_cfn)
    return mock_cfn


@pytest.fixture
def mock_cfn_failure(mocker: MockerFixture, mock_aws_infra: dict[str, str]) -> MagicMock:
    """Mock CloudFormation client for failed operations."""
    mock_cfn = MagicMock()
    _stub_ec2_describes(mock_cfn, mock_aws_infra)
    mock_cfn.describe_stacks.side_effect = ClientError({"Error": {"Code": "ValidationError", "Message": "Stack does not exist"}}, "DescribeStacks")
    mock_cfn.create_stack.return_value = {"StackId": "arn:aws:cloudformation:us-east-1:111111111111:stack/CATS-test-infra-testaccount/abc123"}
    mock_cfn.describe_stack_events.return_value = {
        "StackEvents": [
            {
                "EventId": "event-1",
                "Timestamp": datetime.now(UTC),
                "LogicalResourceId": "CATS-test-infra-testaccount",
                "ResourceType": "AWS::CloudFormation::Stack",
                "ResourceStatus": "CREATE_FAILED",
                "ResourceStatusReason": "Stack creation failed",
            }
        ]
    }
    mocker.patch("boto3.Session.client", return_value=mock_cfn)
    return mock_cfn


def test_infra_without_arg_returns_exit_code_2(cli_runner: CliRunner) -> None:
    # arrange
    expected_exit_code = 2

    # act
    result = cli_runner.invoke(cats_cli, ["infra"])

    # assert
    assert result.exit_code == expected_exit_code


def _infra_config(mock_aws_infra: dict[str, str]) -> dict[str, object]:
    """Build standard infra test config."""
    return make_config(
        network_targets={"private": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_A"]]}},
        allow_deploy_to_current_account=True,
    )


def test_snapshot_deploy_success(
    cats_yaml: Path,
    mock_aws_infra: dict[str, str],
    mock_cfn_success: MagicMock,
    snapshot: SnapshotAssertion,
    cli_runner: CliRunner,
) -> None:
    # arrange
    write_config(cats_yaml, _infra_config(mock_aws_infra))
    expected_exit_code = 0

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "deploy", "testaccount"])

    # assert
    assert result.exit_code == expected_exit_code
    assert normalize_output(result.output) == snapshot


def test_snapshot_deploy_failure(
    cats_yaml: Path,
    mock_aws_infra: dict[str, str],
    mock_cfn_failure: MagicMock,
    snapshot: SnapshotAssertion,
    cli_runner: CliRunner,
) -> None:
    # arrange
    write_config(cats_yaml, _infra_config(mock_aws_infra))
    expected_exit_code = 1

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "deploy", "testaccount"])

    # assert
    assert result.exit_code == expected_exit_code
    assert normalize_output(result.output) == snapshot


def test_snapshot_destroy_nonexistent_stack(
    cats_yaml: Path,
    mock_aws_infra: dict[str, str],
    mock_cfn_success: MagicMock,
    snapshot: SnapshotAssertion,
    cli_runner: CliRunner,
) -> None:
    # arrange
    write_config(cats_yaml, _infra_config(mock_aws_infra))
    expected_exit_code = 0

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "destroy", "testaccount"])

    # assert
    assert result.exit_code == expected_exit_code
    assert normalize_output(result.output) == snapshot


def test_snapshot_show_template(
    cats_yaml: Path,
    mock_aws_infra: dict[str, str],
    snapshot_cfn: SnapshotAssertion,
    cli_runner: CliRunner,
) -> None:
    # arrange
    write_config(cats_yaml, _infra_config(mock_aws_infra))
    expected_exit_code = 0

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "show-template", "testaccount"])

    # assert
    assert result.exit_code == expected_exit_code
    assert normalize_aws_ids(result.output) == snapshot_cfn


def test_snapshot_deploy_with_account_and_all(
    cats_yaml: Path,
    mock_aws_infra: dict[str, str],
    snapshot: SnapshotAssertion,
    cli_runner: CliRunner,
) -> None:
    # arrange
    config = _infra_config(mock_aws_infra)
    config["cats_deployment_settings"] = {"deploy_role_name": "SomeRole"}
    write_config(cats_yaml, config)
    expected_exit_code = 2

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "deploy", "testaccount", "--all"])

    # assert
    assert result.exit_code == expected_exit_code
    assert normalize_paths(result.output) == snapshot


def test_snapshot_deploy_without_account_or_all(
    cats_yaml: Path,
    mock_aws_infra: dict[str, str],
    snapshot: SnapshotAssertion,
    cli_runner: CliRunner,
) -> None:
    # arrange
    write_config(cats_yaml, _infra_config(mock_aws_infra))
    expected_exit_code = 2

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "deploy"])

    # assert
    assert result.exit_code == expected_exit_code
    assert normalize_paths(result.output) == snapshot


def test_snapshot_deploy_unknown_account(
    cats_yaml: Path,
    mock_aws_infra: dict[str, str],
    snapshot: SnapshotAssertion,
    cli_runner: CliRunner,
) -> None:
    # arrange
    write_config(cats_yaml, _infra_config(mock_aws_infra))
    expected_exit_code = 2

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "deploy", "nonexistent"])

    # assert
    assert result.exit_code == expected_exit_code
    assert normalize_paths(result.output) == snapshot


def test_snapshot_targets(
    cats_yaml: Path,
    mock_aws_infra: dict[str, str],
    snapshot: SnapshotAssertion,
    cli_runner: CliRunner,
) -> None:
    # arrange
    write_config(cats_yaml, _infra_config(mock_aws_infra))
    expected_exit_code = 0

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "targets", "testaccount"])

    # assert
    assert result.exit_code == expected_exit_code
    assert normalize_aws_ids(result.output) == snapshot


def test_snapshot_status_not_deployed(
    cats_yaml: Path,
    mock_aws_infra: dict[str, str],
    snapshot: SnapshotAssertion,
    mocker: MockerFixture,
    cli_runner: CliRunner,
) -> None:
    # arrange
    write_config(cats_yaml, _infra_config(mock_aws_infra))
    expected_exit_code = 0

    mock_cfn = MagicMock()
    mock_cfn.describe_stacks.side_effect = ClientError({"Error": {"Code": "ValidationError", "Message": "Stack does not exist"}}, "DescribeStacks")
    mocker.patch("boto3.Session.client", return_value=mock_cfn)

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "status", "testaccount"])

    # assert
    assert result.exit_code == expected_exit_code
    assert result.output == snapshot


def test_snapshot_status_deployed(
    cats_yaml: Path,
    mock_aws_infra: dict[str, str],
    snapshot: SnapshotAssertion,
    mocker: MockerFixture,
    cli_runner: CliRunner,
) -> None:
    # arrange
    write_config(cats_yaml, _infra_config(mock_aws_infra))
    mocker.patch("cats.cli.infra.__version__", "1.0.0")
    expected_exit_code = 0

    mock_cfn = MagicMock()
    mock_cfn.describe_stacks.return_value = {"Stacks": [{"StackStatus": "CREATE_COMPLETE", "Tags": make_stack_tags("1.0.0")}]}
    mocker.patch("boto3.Session.client", return_value=mock_cfn)

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "status", "testaccount"])

    # assert
    assert result.exit_code == expected_exit_code
    assert result.output == snapshot


def test_snapshot_status_deployed_by_other_release(
    cats_yaml: Path,
    mock_aws_infra: dict[str, str],
    snapshot: SnapshotAssertion,
    mocker: MockerFixture,
    cli_runner: CliRunner,
) -> None:
    # arrange
    write_config(cats_yaml, _infra_config(mock_aws_infra))
    mocker.patch("cats.cli.infra.__version__", "1.0.0")
    expected_exit_code = 0

    mock_cfn = MagicMock()
    mock_cfn.describe_stacks.return_value = {"Stacks": [{"StackStatus": "UPDATE_COMPLETE", "Tags": make_stack_tags("0.99.0")}]}
    mocker.patch("boto3.Session.client", return_value=mock_cfn)

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "status", "testaccount"])

    # assert
    assert result.exit_code == expected_exit_code
    assert result.output == snapshot


def test_snapshot_status_deployed_without_version_tag(
    cats_yaml: Path,
    mock_aws_infra: dict[str, str],
    snapshot: SnapshotAssertion,
    mocker: MockerFixture,
    cli_runner: CliRunner,
) -> None:
    # arrange
    write_config(cats_yaml, _infra_config(mock_aws_infra))
    mocker.patch("cats.cli.infra.__version__", "1.0.0")
    expected_exit_code = 0

    mock_cfn = MagicMock()
    mock_cfn.describe_stacks.return_value = {"Stacks": [{"StackStatus": "CREATE_COMPLETE", "Tags": make_stack_tags(None)}]}
    mocker.patch("boto3.Session.client", return_value=mock_cfn)

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "status", "testaccount"])

    # assert
    assert result.exit_code == expected_exit_code
    assert result.output == snapshot


def test_deploy_all_deploys_multiple_accounts(
    cats_yaml: Path,
    mock_aws_infra: dict[str, str],
    mock_cfn_success: MagicMock,
    cli_runner: CliRunner,
) -> None:
    # arrange
    network_targets = {"private": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_A"]]}}
    config = make_config(
        accounts={"accountA": "111111111111", "accountB": "111111111111"},
        network_targets=network_targets,
        allow_deploy_to_current_account=True,
    )
    write_config(cats_yaml, config)

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "deploy", "--all"])

    # assert
    assert result.exit_code == 0
    assert mock_cfn_success.create_stack.call_count == 2


def test_deploy_all_closes_each_account_the_cycle_its_own_gate_finishes(
    cats_yaml: Path,
    mock_aws_infra: dict[str, str],
    cli_runner: CliRunner,
    mocker: MockerFixture,
) -> None:
    """An account with no listeners is Created as soon as its test role authorizes, not when the slowest EC2 boot in the batch ends."""
    # arrange - accountA has no network targets (role gate only, authorized on cycle 1); accountB's listener is ready on cycle 2
    config = make_config(
        accounts={"accountA": "111111111111", "accountB": "111111111111"},
        network_targets={"private": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_A"]]}},
        allow_deploy_to_current_account=True,
    )
    config["accounts_under_test"]["accountA"]["account_network_under_test"] = {}
    write_config(cats_yaml, config)
    test_role_output = {"OutputKey": "TestRoleArn", "OutputValue": "arn:aws:iam::111111111111:role/CATS-test-infra-accountA-default-test-role"}
    mock_client = MagicMock()
    _stub_ec2_describes(mock_client, mock_aws_infra)
    _stub_stack_lifecycles(
        mock_client,
        {
            "CATS-test-infra-accountA": [test_role_output],
            "CATS-test-infra-accountB": [test_role_output, {"OutputKey": "Ec2InstanceIdprivate", "OutputValue": "i-0bbb"}],
        },
    )
    mock_client.get_console_output.side_effect = [
        {"InstanceId": "i-0bbb"},
        {"InstanceId": "i-0bbb", "Output": f"cloud-init[1644]: {LISTENER_READY_MARKER}\n"},
    ]
    mock_client.describe_stack_events.return_value = {
        "StackEvents": [
            {
                "EventId": "event-1",
                "Timestamp": datetime.now(UTC),
                "LogicalResourceId": "stack",
                "ResourceType": "AWS::CloudFormation::Stack",
                "ResourceStatus": "CREATE_COMPLETE",
                "ResourceStatusReason": None,
            }
        ]
    }
    mocker.patch("boto3.Session.client", return_value=mock_client)
    patch_role_probe(mocker, "CATS-test-infra-accountA")
    sleep = mocker.patch("cats.cfn.deploy.time.sleep")
    closed: list[tuple[str, int]] = []  # (account, poll cycles slept before it was closed)
    original_mark_complete = MultiAccountProgress.mark_complete

    def mark_complete(self: MultiAccountProgress, result: DeployResult) -> None:
        closed.append((result.account_name, sleep.call_count))
        original_mark_complete(self, result)

    mocker.patch.object(MultiAccountProgress, "mark_complete", mark_complete)

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "deploy", "--all"])

    # assert - accountA closed before the first sleep, accountB after it, each exactly once
    assert (result.exit_code, closed) == (0, [("accountA", 0), ("accountB", 1)])


def test_destroy_all_prints_a_failure_that_polling_finds(
    cats_yaml: Path,
    mock_aws_infra: dict[str, str],
    cli_runner: CliRunner,
    mocker: MockerFixture,
) -> None:
    """A destroy that fails while its events are polled reaches the Failures block, not only the exit code."""
    # arrange - both stacks exist and start deleting; accountB's events cannot be read once the delete is running
    config = make_config(accounts={"accountA": "111111111111", "accountB": "111111111111"}, allow_deploy_to_current_account=True)
    write_config(cats_yaml, config)
    mock_client = MagicMock()
    mock_client.describe_stacks.side_effect = lambda **kwargs: {
        "Stacks": [{"StackId": _stack_id(str(kwargs["StackName"])), "StackStatus": "CREATE_COMPLETE"}]
    }
    mock_client.list_stack_resources.return_value = {"StackResourceSummaries": []}
    denied = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "not authorized to perform: cloudformation:DescribeStackEvents"}}, "DescribeStackEvents"
    )
    delete_complete: dict[str, object] = {
        "StackEvents": [
            {
                "EventId": "event-1",
                "Timestamp": datetime.now(UTC),
                "LogicalResourceId": "stack",
                "ResourceType": "AWS::CloudFormation::Stack",
                "ResourceStatus": "DELETE_COMPLETE",
                "ResourceStatusReason": None,
            }
        ]
    }
    deleting: set[str] = set()  # stack ids delete_stack has been called for

    def delete_stack(**kwargs: object) -> dict[str, object]:
        deleting.add(_stack_id(str(kwargs["StackName"])))
        return {}

    def describe_stack_events(**kwargs: object) -> dict[str, object]:
        stack_id = str(kwargs["StackName"])
        if stack_id not in deleting:
            return {"StackEvents": []}  # start_destroy seeds its poller from the history before deleting
        if stack_id == _stack_id("CATS-test-infra-accountB"):
            raise denied
        return delete_complete

    mock_client.delete_stack.side_effect = delete_stack
    mock_client.describe_stack_events.side_effect = describe_stack_events
    mocker.patch("boto3.Session.client", return_value=mock_client)
    expected_stderr = [
        "",
        "Failures:",
        "  accountB: An error occurred (AccessDenied) when calling the DescribeStackEvents operation: not authorized to perform: "
        + "cloudformation:DescribeStackEvents",
    ]

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "destroy", "--all"])

    # assert
    assert (result.exit_code, result.stderr.splitlines()) == (1, expected_stderr)


def test_deploy_hands_start_deploy_the_additional_role_arns_the_tests_will_assume(
    cats_yaml: Path,
    mock_aws_infra: dict[str, str],
    mock_cfn_success: MagicMock,
    cli_runner: CliRunner,
    mocker: MockerFixture,
) -> None:
    """The readiness gate probes the additional roles by the ARNs cats/resources/session_interact.py builds at test time."""
    # arrange - declared in the order the assertion expects: write_config's yaml.dump sorts keys, so an unsorted dict would be reordered on reload
    config = make_config(
        network_targets={"private": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_A"]]}},
        additional_roles={"audit_only": {"managed_policies": ["SecurityAudit"]}, "readonly": {"managed_policies": ["ReadOnlyAccess"]}},
        allow_deploy_to_current_account=True,
    )
    write_config(cats_yaml, config)
    start_deploy = mocker.patch("cats.cli.infra.start_deploy", return_value=DeployResult(account_name="testaccount", success=True))

    # act
    cli_runner.invoke(cats_cli, ["infra", "deploy", "testaccount"])

    # assert - one ARN per additional role, in the order the config declares them (the resolver does not sort)
    expected = [
        "arn:aws:iam::111111111111:role/CATS-test-infra-testaccount-audit_only",
        "arn:aws:iam::111111111111:role/CATS-test-infra-testaccount-readonly",
    ]
    assert start_deploy.call_args.kwargs["additional_role_arns"] == expected


def test_destroy_all_with_no_stacks_succeeds(
    cats_yaml: Path,
    mock_aws_infra: dict[str, str],
    mock_cfn_success: MagicMock,
    cli_runner: CliRunner,
) -> None:
    # arrange
    network_targets = {"private": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_A"]]}}
    config = make_config(
        accounts={"accountA": "111111111111", "accountB": "111111111111"},
        network_targets=network_targets,
        allow_deploy_to_current_account=True,
    )
    write_config(cats_yaml, config)

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "destroy", "--all"])

    # assert
    assert result.exit_code == 0


def test_deploy_rejects_oversized_template_before_touching_aws(
    cats_yaml: Path,
    mock_aws_infra: dict[str, str],
    cli_runner: CliRunner,
    mocker: MockerFixture,
) -> None:
    """An oversized config is refused from the offline estimate: exit 2, the message on stderr, and no deploy session opened."""
    # arrange - shrink the limit below any real template so the gate triggers on a fitting config
    mocker.patch("cats.cfn.render.CFN_TEMPLATE_BODY_LIMIT", 1024)
    get_deploy_session = mocker.patch("cats.cli.infra.get_deploy_session")
    network_targets = {f"target{i}": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_A"]]} for i in range(4)}
    config = make_config(
        network_targets=network_targets,
        allow_deploy_to_current_account=True,
        enabled_network_handlers=["ec2", "lambda"],
    )
    write_config(cats_yaml, config)

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "deploy", "testaccount"])

    # assert: a configuration error, on stderr, exit 2, before any AWS call
    expected_stderr_line = (
        "FAIL Template for 'testaccount' renders to 38,727 bytes; CloudFormation's TemplateBody limit is 1,024. "
        + "Reduce network targets, listen ports or enabled network handlers in this account, "
        + "or split its targets across a second account entry with the same account_id."
    )
    assert (result.exit_code, result.stderr.splitlines()[-1], get_deploy_session.call_count) == (2, expected_stderr_line, 0)


def test_deploy_all_rejects_oversized_template_with_the_same_exit_code(
    cats_yaml: Path,
    mock_aws_infra: dict[str, str],
    cli_runner: CliRunner,
    mocker: MockerFixture,
) -> None:
    """The multi-account path exits 2 for the same pre-flight failure the single-account path exits 2 on."""
    # arrange
    mocker.patch("cats.cfn.render.CFN_TEMPLATE_BODY_LIMIT", 1024)
    network_targets = {f"target{i}": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_A"]]} for i in range(4)}
    config = make_config(
        accounts={"accountA": "111111111111", "accountB": "111111111111"},
        network_targets=network_targets,
        allow_deploy_to_current_account=True,
        enabled_network_handlers=["ec2", "lambda"],
    )
    write_config(cats_yaml, config)

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "deploy", "--all"])

    # assert
    assert result.exit_code == 2


def test_deploy_two_named_accounts_deploys_both(
    cats_yaml: Path,
    mock_aws_infra: dict[str, str],
    mock_cfn_success: MagicMock,
    cli_runner: CliRunner,
) -> None:
    """Deploy accepts multiple account names, matching the documented CLI."""
    # arrange
    network_targets = {"private": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_A"]]}}
    config = make_config(
        accounts={"accountA": "111111111111", "accountB": "111111111111"},
        network_targets=network_targets,
        allow_deploy_to_current_account=True,
    )
    write_config(cats_yaml, config)

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "deploy", "accountA", "accountB"])

    # assert
    assert (result.exit_code, mock_cfn_success.create_stack.call_count) == (0, 2)


def test_deploy_all_one_account_start_failure_exits_1_without_traceback(
    cats_yaml: Path,
    mock_aws_infra: dict[str, str],
    mock_cfn_success: MagicMock,
    cli_runner: CliRunner,
    mocker: MockerFixture,
) -> None:
    """One account's start failure becomes a failed result; the others still complete."""
    # arrange
    network_targets = {"private": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_A"]]}}
    config = make_config(
        accounts={"accountA": "111111111111", "accountB": "111111111111"},
        network_targets=network_targets,
        allow_deploy_to_current_account=True,
    )
    write_config(cats_yaml, config)
    real_start = infra_module._start_deploy_account

    def fail_account_b(name: str, disable_rollback: bool = False, on_notice: object = None) -> object:
        if name == "accountB":
            raise RuntimeError("role assume failed")
        return real_start(name, disable_rollback, on_notice)  # type: ignore[arg-type]

    mocker.patch("cats.cli.infra._start_deploy_account", side_effect=fail_account_b)

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "deploy", "--all"])

    # assert: exit 1 via typer.Exit, not an unhandled RuntimeError crash
    assert (result.exit_code, isinstance(result.exception, SystemExit)) == (1, True)


def test_deploy_all_mixed_config_and_operational_failures_exits_2(
    cats_yaml: Path,
    cli_runner: CliRunner,
    mocker: MockerFixture,
) -> None:
    """A batch with one configuration failure and one operational failure reports the configuration one: exit 2."""
    # arrange
    config = make_config(accounts={"accountA": "111111111111", "accountB": "111111111111"}, allow_deploy_to_current_account=True)
    write_config(cats_yaml, config)

    def fail_each_differently(name: str, disable_rollback: bool = False, on_notice: object = None) -> object:
        if name == "accountA":
            raise CatsConfigError("template too large")
        raise RuntimeError("role assume failed")

    mocker.patch("cats.cli.infra._start_deploy_account", side_effect=fail_each_differently)

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "deploy", "--all"])

    # assert
    assert result.exit_code == 2


def test_destroy_all_mixed_config_and_operational_failures_exits_2(
    cats_yaml: Path,
    cli_runner: CliRunner,
    mocker: MockerFixture,
) -> None:
    """The documented batch rule applies to destroy as it does to deploy: a configuration failure in the batch reports 2.

    Reachable when get_deploy_session refuses the current account (allow_deploy_to_current_account false, or no
    deploy_role_name): single-account destroy of that case already exits 2; the batch exited 1."""
    # arrange
    config = make_config(accounts={"accountA": "111111111111", "accountB": "111111111111"}, allow_deploy_to_current_account=True)
    write_config(cats_yaml, config)

    def fail_each_differently(name: str) -> object:
        if name == "accountA":
            raise CatsConfigError("Deploying to current account 111111111111 blocked.")
        raise RuntimeError("role assume failed")

    mocker.patch("cats.cli.infra._start_destroy_account", side_effect=fail_each_differently)

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "destroy", "--all"])

    # assert
    assert result.exit_code == 2


def test_deploy_all_without_a_region_reports_it_once(
    cats_yaml: Path,
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing Region is one line on stderr for the whole batch."""
    # arrange
    monkeypatch.delenv("AWS_REGION", raising=False)
    config = make_config(accounts={"accountA": "111111111111", "accountB": "111111111111"}, allow_deploy_to_current_account=True)
    del config["cats_deployment_settings"]["aws_region"]
    write_config(cats_yaml, config)

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "deploy", "--all"])

    # assert
    assert (result.exit_code, result.stderr.splitlines()) == (2, [f"FAIL {AWS_REGION_UNSET_MESSAGE}"])


def test_status_exits_1_when_an_account_errors(
    cats_yaml: Path,
    mock_aws_infra: dict[str, str],
    mocker: MockerFixture,
    cli_runner: CliRunner,
) -> None:
    # arrange
    write_config(cats_yaml, _infra_config(mock_aws_infra))
    mock_cfn = MagicMock()
    mock_cfn.describe_stacks.side_effect = ClientError({"Error": {"Code": "AccessDenied", "Message": "not allowed"}}, "DescribeStacks")
    mocker.patch("boto3.Session.client", return_value=mock_cfn)

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "status", "testaccount"])

    # assert
    assert result.exit_code == 1


def test_status_prints_an_error_containing_rich_markup_verbatim(
    cats_yaml: Path,
    mock_aws_infra: dict[str, str],
    mocker: MockerFixture,
    cli_runner: CliRunner,
) -> None:
    """AWS error text is data, not markup: a '[sts:AssumeRole]' segment must reach the user, not be eaten as a style tag."""
    # arrange
    write_config(cats_yaml, _infra_config(mock_aws_infra))
    mock_cfn = MagicMock()
    mock_cfn.describe_stacks.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "not authorized to perform [sts:AssumeRole] on the test role"}}, "DescribeStacks"
    )
    mocker.patch("boto3.Session.client", return_value=mock_cfn)

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "status", "testaccount"])

    # assert - the status cell carries the AWS message verbatim, brackets included
    expected_row = (
        "│ testaccount │ error: An error occurred (AccessDenied) when calling the DescribeStacks operation: "
        + "not authorized to perform [sts:AssumeRole] on the test role │ -     │ -       │"
    )
    assert (result.exit_code, result.output.splitlines()[3]) == (1, expected_row)


def test_status_unknown_account_is_a_usage_error(
    cats_yaml: Path,
    mock_aws_infra: dict[str, str],
    cli_runner: CliRunner,
) -> None:
    # arrange
    write_config(cats_yaml, _infra_config(mock_aws_infra))

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "status", "nonexistent"])

    # assert
    assert result.exit_code == 2


def test_targets_unknown_account_is_a_usage_error(
    cats_yaml: Path,
    mock_aws_infra: dict[str, str],
    cli_runner: CliRunner,
) -> None:
    """An unknown account name must be a usage error, not a raw KeyError traceback."""
    # arrange
    write_config(cats_yaml, _infra_config(mock_aws_infra))

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "targets", "nonexistent"])

    # assert
    assert (result.exit_code, isinstance(result.exception, SystemExit)) == (2, True)


def test_show_template_unknown_account_is_a_usage_error(
    cats_yaml: Path,
    mock_aws_infra: dict[str, str],
    cli_runner: CliRunner,
) -> None:
    """An unknown account name must be a usage error, not a raw KeyError traceback."""
    # arrange
    write_config(cats_yaml, _infra_config(mock_aws_infra))

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "show-template", "nonexistent"])

    # assert
    assert (result.exit_code, isinstance(result.exception, SystemExit)) == (2, True)


def test_show_template_two_named_accounts_dumps_both(
    cats_yaml: Path,
    mock_aws_infra: dict[str, str],
    cli_runner: CliRunner,
    tmp_path: Path,
) -> None:
    """show-template is variadic like deploy and destroy."""
    # arrange
    network_targets = {"private": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_A"]]}}
    config = make_config(
        accounts={"accountA": "111111111111", "accountB": "111111111111"},
        network_targets=network_targets,
        allow_deploy_to_current_account=True,
    )
    write_config(cats_yaml, config)
    dump_dir = tmp_path / "templates"

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "show-template", "accountA", "accountB", "--dump-dir", str(dump_dir)])

    # assert
    expected = (0, ["accountA.yaml", "accountB.yaml"])
    assert (result.exit_code, sorted(p.name for p in dump_dir.iterdir())) == expected


def test_show_template_without_names_renders_every_account(
    cats_yaml: Path,
    mock_aws_infra: dict[str, str],
    cli_runner: CliRunner,
    tmp_path: Path,
) -> None:
    """Read-only commands default to every account; there is no --all to remember."""
    # arrange
    network_targets = {"private": {"vpc_id": mock_aws_infra["VPC"], "subnet_ids": [mock_aws_infra["SUBNET_A"]]}}
    config = make_config(
        accounts={"accountA": "111111111111", "accountB": "111111111111"}, network_targets=network_targets, allow_deploy_to_current_account=True
    )
    write_config(cats_yaml, config)
    dump_dir = tmp_path / "templates"

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "show-template", "--dump-dir", str(dump_dir)])

    # assert
    expected = (0, ["accountA.yaml", "accountB.yaml"])
    assert (result.exit_code, sorted(p.name for p in dump_dir.iterdir())) == expected


def test_targets_two_named_accounts_lists_both(
    cats_yaml: Path,
    mock_aws_infra: dict[str, str],
    cli_runner: CliRunner,
) -> None:
    """targets takes account keynames like status, not one optional positional."""
    # arrange
    write_config(cats_yaml, make_config(accounts={"accountA": "111111111111", "accountB": "222222222222"}))

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "targets", "accountA", "accountB"])

    # assert
    expected = (0, ["CATS-test-infra-accountA", "CATS-test-infra-accountB"])
    assert (result.exit_code, [line.split()[-1] for line in result.stdout.splitlines() if "Stack:" in line]) == expected


def test_targets_without_names_lists_every_account(
    cats_yaml: Path,
    mock_aws_infra: dict[str, str],
    cli_runner: CliRunner,
) -> None:
    # arrange
    write_config(cats_yaml, make_config(accounts={"accountA": "111111111111", "accountB": "222222222222"}))

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "targets"])

    # assert
    expected = (0, ["CATS-test-infra-accountA", "CATS-test-infra-accountB"])
    assert (result.exit_code, [line.split()[-1] for line in result.stdout.splitlines() if "Stack:" in line]) == expected


@pytest.mark.parametrize("command", ["targets", "status", "show-template"])
def test_read_only_commands_have_no_all_flag(command: str, cats_yaml: Path, mock_aws_infra: dict[str, str], cli_runner: CliRunner) -> None:
    """One shape for the three read-only commands: names, or nothing for every account. --all belongs to deploy and destroy."""
    # arrange
    write_config(cats_yaml, _infra_config(mock_aws_infra))

    # act
    result = cli_runner.invoke(cats_cli, ["infra", command, "--all"])

    # assert
    assert (result.exit_code, result.stdout) == (2, "")


def test_status_reads_accounts_concurrently_and_keeps_argument_order(
    cats_yaml: Path,
    mock_aws_infra: dict[str, str],
    mocker: MockerFixture,
    cli_runner: CliRunner,
) -> None:
    """status assumes a role and describes a stack per account; ten accounts in series is the speed goal's enemy."""
    # arrange
    accounts = {f"acct{i}": "111111111111" for i in range(1, 5)}
    write_config(cats_yaml, make_config(accounts=accounts, allow_deploy_to_current_account=True))
    mocker.patch("cats.cli.infra.__version__", "1.0.0")
    threads_seen: set[str] = set()

    def describe_stacks(**kwargs: object) -> dict[str, object]:
        threads_seen.add(threading.current_thread().name)
        return {"Stacks": [{"StackStatus": "CREATE_COMPLETE", "Tags": make_stack_tags("1.0.0")}]}

    mock_cfn = MagicMock()
    mock_cfn.describe_stacks.side_effect = describe_stacks
    mocker.patch("boto3.Session.client", return_value=mock_cfn)

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "status", "acct3", "acct1", "acct4", "acct2"])

    # assert
    rows = [line.split()[1] for line in result.stdout.splitlines() if line.startswith("│ acct")]
    expected = (0, ["acct3", "acct1", "acct4", "acct2"], True)
    assert (result.exit_code, rows, len(threads_seen) > 1) == expected


@pytest.mark.parametrize(
    ("status", "expected_style"),
    [
        ("CREATE_COMPLETE", "green"),
        ("UPDATE_COMPLETE", "green"),
        ("ROLLBACK_COMPLETE", "red"),
        ("UPDATE_ROLLBACK_COMPLETE", "red"),
        ("CREATE_FAILED", "red"),
        ("CREATE_IN_PROGRESS", "yellow"),
    ],
)
def test_status_style_colors_rolled_back_stacks_red(status: str, expected_style: str) -> None:
    """ROLLBACK_COMPLETE ends in COMPLETE but is a failed deploy."""
    # act
    style = infra_module._status_style(status)

    # assert
    assert style == expected_style


def test_missing_config_path_exits_2_naming_the_file(cli_runner: CliRunner, tmp_path: Path) -> None:
    """-c to a path that is not a file is a configuration error (exit 2), not an empty config."""
    # arrange
    missing = tmp_path / "nope.yaml"

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "status", "-c", str(missing)])

    # assert
    assert (result.exit_code, result.stderr) == (2, f"FAIL Config file not found: {missing}\n")


@pytest.mark.parametrize("arguments", [["targets"], ["show-template"], ["status"], ["deploy", "--all"], ["destroy", "--all"]])
def test_absent_default_config_exits_2_naming_the_file(arguments: list[str], cats_yaml: Path, cli_runner: CliRunner) -> None:
    """With no cats.yaml the default config is empty: `targets` printed a blank line, `show-template` nothing, `status` blamed
    aws_region, all exit 0 or a wrong diagnosis. The file is the thing to fix, so every command says so and exits 2."""
    # arrange - CATS_CONFIG names a file nobody wrote

    # act
    result = cli_runner.invoke(cats_cli, ["infra", *arguments])

    # assert
    assert (result.exit_code, result.stdout, result.stderr) == (2, "", f"FAIL Config file not found: {cats_yaml}\n")


_NO_IDENTITY_LINE = (
    "FAIL Cannot reach AWS to read the caller identity: Unable to locate credentials. "
    + "The environment CATS runs in needs a working AWS identity (a profile, SSO, or environment variables) and a Region."
)


@pytest.mark.parametrize("command", ["deploy", "show-template"])
def test_without_credentials_is_one_diagnostic_line_not_a_traceback(
    command: str,
    cats_yaml: Path,
    mock_aws_infra: dict[str, str],
    cli_runner: CliRunner,
    mock_current_account: MagicMock,
) -> None:
    """The first AWS-touching command a new user runs; a missing identity is an operation failure: exit 1, the SDK's message, nothing else."""
    # arrange
    write_config(cats_yaml, _infra_config(mock_aws_infra))
    mock_current_account.side_effect = CatsInfrastructureError(_NO_IDENTITY_LINE.removeprefix("FAIL "))

    # act
    result = cli_runner.invoke(cats_cli, ["infra", command, "testaccount"])

    # assert
    assert (result.exit_code, result.stderr.splitlines()) == (1, [_NO_IDENTITY_LINE])


def test_aws_sdk_error_escaping_a_command_is_one_diagnostic_line(
    cats_yaml: Path,
    mock_aws_infra: dict[str, str],
    cli_runner: CliRunner,
    mocker: MockerFixture,
) -> None:
    """A ClientError no command caught (expired credentials, throttling) is reported like a CatsError: exit 1, one line on stderr."""
    # arrange
    write_config(cats_yaml, _infra_config(mock_aws_infra))
    error = ClientError({"Error": {"Code": "ExpiredToken", "Message": "The security token included in the request is expired"}}, "GetCallerIdentity")
    mocker.patch("cats.cli.infra.get_deploy_session", side_effect=error)

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "show-template", "testaccount"])

    # assert
    expected_line = (
        "FAIL An error occurred (ExpiredToken) when calling the GetCallerIdentity operation: The security token included in the request is expired"
    )
    assert (result.exit_code, result.stderr.splitlines()) == (1, [expected_line])


def test_cats_debug_lets_an_aws_sdk_error_propagate_with_its_traceback(
    cats_yaml: Path,
    mock_aws_infra: dict[str, str],
    cli_runner: CliRunner,
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one-line diagnostic also flattens an SDK error CATS itself caused (a bad request); CATS_DEBUG=1 shows the frames."""
    # arrange
    monkeypatch.setenv("CATS_DEBUG", "1")
    write_config(cats_yaml, _infra_config(mock_aws_infra))
    error = ClientError({"Error": {"Code": "ExpiredToken", "Message": "The security token included in the request is expired"}}, "GetCallerIdentity")
    mocker.patch("cats.cli.infra.get_deploy_session", side_effect=error)

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "show-template", "testaccount"])

    # assert
    assert (result.exception, result.stderr) == (error, "")


def test_cats_debug_lets_a_cats_error_caused_by_an_sdk_error_propagate(
    cats_yaml: Path,
    mock_aws_infra: dict[str, str],
    cli_runner: CliRunner,
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session boundary wraps every BotoCoreError as 'Cannot reach AWS', which misdescribes a ParamValidationError CATS itself
    caused; CATS_DEBUG=1 lets the wrapped error propagate too, with the SDK error as its cause."""
    # arrange
    monkeypatch.setenv("CATS_DEBUG", "1")
    write_config(cats_yaml, _infra_config(mock_aws_infra))
    cause = ParamValidationError(report="Invalid length for parameter RoleSessionName")
    wrapped = CatsInfrastructureError("Cannot reach AWS to read the caller identity: ...")
    wrapped.__cause__ = cause
    mocker.patch("cats.cli.infra.get_deploy_session", side_effect=wrapped)

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "show-template", "testaccount"])

    # assert
    assert (result.exception, result.stderr) == (wrapped, "")


def test_cats_debug_follows_an_sdk_error_in_the_context_when_no_cause_was_chained(
    cats_yaml: Path,
    mock_aws_infra: dict[str, str],
    cli_runner: CliRunner,
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CatsError raised inside an except block without `from e` carries the SDK error as __context__, not __cause__;
    CATS_DEBUG=1 must not depend on every boundary chaining explicitly."""
    # arrange
    monkeypatch.setenv("CATS_DEBUG", "1")
    write_config(cats_yaml, _infra_config(mock_aws_infra))
    context = ParamValidationError(report="Invalid length for parameter RoleSessionName")
    wrapped = CatsInfrastructureError("Cannot reach AWS to read the caller identity: ...")
    wrapped.__context__ = context
    mocker.patch("cats.cli.infra.get_deploy_session", side_effect=wrapped)

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "show-template", "testaccount"])

    # assert
    assert (result.exception, result.stderr) == (wrapped, "")


def test_cats_debug_leaves_a_cats_error_without_an_sdk_cause_as_one_line(
    cats_yaml: Path,
    mock_aws_infra: dict[str, str],
    cli_runner: CliRunner,
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CATS_DEBUG is about SDK errors; a CatsError CATS raised on its own (a template over the size limit) stays a diagnostic."""
    # arrange
    monkeypatch.setenv("CATS_DEBUG", "1")
    write_config(cats_yaml, _infra_config(mock_aws_infra))
    mocker.patch("cats.cli.infra.get_deploy_session", side_effect=CatsInfrastructureError("the stack is in ROLLBACK_FAILED"))

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "show-template", "testaccount"])

    # assert
    assert (result.exit_code, result.stderr.splitlines()) == (1, ["FAIL the stack is in ROLLBACK_FAILED"])


def test_cats_debug_ignores_a_context_suppressed_with_from_none(
    cats_yaml: Path,
    mock_aws_infra: dict[str, str],
    cli_runner: CliRunner,
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`raise CatsError(...) from None` hides the SDK error from the traceback, so re-raising would show a traceback without
    the thing the flag exists to show; the error stays a diagnostic."""
    # arrange
    monkeypatch.setenv("CATS_DEBUG", "1")
    write_config(cats_yaml, _infra_config(mock_aws_infra))
    wrapped = CatsInfrastructureError("Cannot reach AWS to read the caller identity: ...")
    wrapped.__context__ = ParamValidationError(report="Invalid length for parameter RoleSessionName")
    wrapped.__suppress_context__ = True
    mocker.patch("cats.cli.infra.get_deploy_session", side_effect=wrapped)

    # act
    result = cli_runner.invoke(cats_cli, ["infra", "show-template", "testaccount"])

    # assert
    assert (result.exit_code, result.stderr.splitlines()) == (1, ["FAIL Cannot reach AWS to read the caller identity: ..."])
