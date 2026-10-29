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

"""Tests for cats.cli.run subcommands."""

import json
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest
from botocore.exceptions import ClientError, ProfileNotFound
from pytest_mock import MockerFixture
from typer.testing import CliRunner

from cats.cli import cats_cli
from cats.constants import CatsNetworkResponse, CatsNetworkResult, DeployedEC2Resources, DeployedTargetResources, ShellResult
from cats.exceptions import CatsConfigError, CatsInfrastructureError, CatsUsageError


@pytest.fixture
def mock_get_account(mocker: MockerFixture) -> MagicMock:
    """Patch the get_account used inside cats.cli.run; the account knows one target, `priv`."""
    mock: MagicMock = mocker.patch("cats.cli.run.get_account")
    mock.return_value.name = "accA"
    mock.return_value.available_network_targets = ["priv"]
    return mock


@pytest.fixture
def mock_prepare_udp_receiver(mocker: MockerFixture) -> MagicMock:
    """Patch the prepare_udp_receiver used inside cats.cli.run; its own behavior is covered in test_prepare_udp_receiver.py."""
    mock: MagicMock = mocker.patch("cats.cli.run.prepare_udp_receiver")
    return mock


def test_account_info_emits_runtime_metadata(mock_get_account: MagicMock, cli_runner: CliRunner) -> None:
    # arrange
    handler = DeployedTargetResources(
        response_bucket_name="cats-bucket",
        vpc_id="vpc-123",
        subnet_id_list=["subnet-aaa", "subnet-bbb"],
        account_name="accA",
        target_name="priv",
        enabled_handlers=["ec2"],
        ec2=DeployedEC2Resources(dns_name="ip-10-0-1-5.ec2.internal", instance_id="i-abc"),
        lambda_=None,
    )
    acc = mock_get_account.return_value
    acc.name = "accA"
    acc.account_id = "111111111111"
    acc.tags = {"env": "prod"}
    acc.deployed_version = "1.0.0"
    acc._deployed_network_handlers = {"priv": handler}

    # act
    result = cli_runner.invoke(cats_cli, ["run", "account-info", "--account", "accA"])

    # assert
    expected = {
        "name": "accA",
        "account_id": "111111111111",
        "tags": {"env": "prod"},
        "deployed_version": "1.0.0",
        "targets": {
            "priv": {
                "vpc_id": "vpc-123",
                "subnet_ids": ["subnet-aaa", "subnet-bbb"],
                "enabled_handlers": ["ec2"],
                "ec2": {"dns_name": "ip-10-0-1-5.ec2.internal", "instance_id": "i-abc"},
                "lambda": None,
            }
        },
    }
    assert result.exit_code == 0
    assert json.loads(result.stdout) == expected


def test_http_call_emits_result_reason_and_http_status(mock_get_account: MagicMock, cli_runner: CliRunner) -> None:
    # arrange
    mock_get_account.return_value.priv.http_call.return_value = CatsNetworkResult(response=CatsNetworkResponse.SUCCESS, http_status=403)
    expected = {"result": "SUCCESS", "reason": None, "http_status": 403}

    # act
    result = cli_runner.invoke(cats_cli, ["run", "http-call", "--account", "accA", "--target", "priv", "--host", "10.0.1.5", "--port", "8080"])

    # assert
    assert result.exit_code == 0
    assert json.loads(result.stdout) == expected


def test_http_call_failure_emits_reason_and_null_http_status(mock_get_account: MagicMock, cli_runner: CliRunner) -> None:
    # arrange
    mock_get_account.return_value.priv.http_call.return_value = CatsNetworkResult(
        response=CatsNetworkResponse.DNS_FAILURE, reason="[Errno -2] Name or service not known"
    )
    expected = {"result": "DNS_FAILURE", "reason": "[Errno -2] Name or service not known", "http_status": None}

    # act
    result = cli_runner.invoke(
        cats_cli, ["run", "http-call", "--account", "accA", "--target", "priv", "--host", "nonexistent.invalid", "--port", "80"]
    )

    # assert
    assert result.exit_code == 0
    assert json.loads(result.stdout) == expected


def test_tcp_call_emits_result_reason_and_null_http_status(mock_get_account: MagicMock, cli_runner: CliRunner) -> None:
    # arrange
    mock_get_account.return_value.priv.tcp_call.return_value = CatsNetworkResult(response=CatsNetworkResponse.TIMEOUT, reason="timed out")
    expected = {"result": "TIMEOUT", "reason": "timed out", "http_status": None}

    # act
    result = cli_runner.invoke(cats_cli, ["run", "tcp-call", "--account", "accA", "--target", "priv", "--host", "10.0.1.5", "--port", "22"])

    # assert
    assert result.exit_code == 0
    assert json.loads(result.stdout) == expected


def test_udp_call_emits_result_reason_and_correlation_id(mock_get_account: MagicMock, cli_runner: CliRunner) -> None:
    # arrange
    mock_get_account.return_value.priv.udp_call.return_value = CatsNetworkResult(response=CatsNetworkResponse.SENT, correlation_id="abc-123")
    expected = {"result": "SENT", "reason": None, "correlation_id": "abc-123"}

    # act
    result = cli_runner.invoke(cats_cli, ["run", "udp-call", "--account", "accA", "--target", "priv", "--host", "10.0.1.5", "--port", "5000"])

    # assert
    assert result.exit_code == 0
    assert json.loads(result.stdout) == expected


def test_udp_call_without_receiving_account_constructs_only_the_sender(
    mock_get_account: MagicMock, mock_prepare_udp_receiver: MagicMock, cli_runner: CliRunner
) -> None:
    # arrange - a non-CATS UDP target: nothing to register, nothing to check
    mock_get_account.return_value.priv.udp_call.return_value = CatsNetworkResult(response=CatsNetworkResponse.SENT, correlation_id="abc-123")

    # act
    cli_runner.invoke(cats_cli, ["run", "udp-call", "--account", "accA", "--target", "priv", "--host", "10.0.1.5", "--port", "5000"])

    # assert
    assert (mock_prepare_udp_receiver.called, mock_get_account.call_args_list) == (False, [call(None, "accA")])


def test_udp_call_with_receiving_account_prepares_the_receiver_before_sending(
    mock_get_account: MagicMock, mock_prepare_udp_receiver: MagicMock, cli_runner: CliRunner
) -> None:
    # arrange - constructing the receiver registers its receipt token, which the sender's udp_call then prepends
    order: list[str] = []

    def prepare(*args: object) -> None:
        order.append("prepare")

    def send(*args: object) -> CatsNetworkResult:
        order.append("send")
        return CatsNetworkResult(response=CatsNetworkResponse.SENT, correlation_id="abc-123")

    mock_prepare_udp_receiver.side_effect = prepare
    mock_get_account.return_value.priv.udp_call.side_effect = send

    # act
    result = cli_runner.invoke(
        cats_cli,
        [
            "run",
            "udp-call",
            "--account",
            "accA",
            "--target",
            "priv",
            "--host",
            "ip-10-0-2-9.ec2.internal",
            "--port",
            "5000",
            "--receiving-account",
            "accB",
        ],
    )

    # assert
    expected = (0, [call(None, "accB", "ip-10-0-2-9.ec2.internal")], ["prepare", "send"])
    assert (result.exit_code, mock_prepare_udp_receiver.call_args_list, order) == expected


def test_udp_call_with_receiving_account_passes_the_config_path(
    mock_get_account: MagicMock, mock_prepare_udp_receiver: MagicMock, cli_runner: CliRunner
) -> None:
    # arrange
    mock_get_account.return_value.priv.udp_call.return_value = CatsNetworkResult(response=CatsNetworkResponse.SENT, correlation_id="abc-123")

    # act
    cli_runner.invoke(
        cats_cli,
        [
            "run",
            "udp-call",
            "--account",
            "accA",
            "--target",
            "priv",
            "--host",
            "ip-10-0-2-9.ec2.internal",
            "--port",
            "5000",
            "--receiving-account",
            "accB",
            "--config",
            "other/cats.yaml",
        ],
    )

    # assert
    mock_prepare_udp_receiver.assert_called_once_with(Path("other/cats.yaml"), "accB", "ip-10-0-2-9.ec2.internal")


def test_udp_call_host_that_is_not_the_receivers_listener_exits_two_before_sending(
    mock_get_account: MagicMock, mock_prepare_udp_receiver: MagicMock, cli_runner: CliRunner
) -> None:
    # arrange
    mock_prepare_udp_receiver.side_effect = CatsUsageError("host '10.0.1.5' is not a CATS listener of receiving account 'accB'.")

    # act
    result = cli_runner.invoke(
        cats_cli, ["run", "udp-call", "--account", "accA", "--target", "priv", "--host", "10.0.1.5", "--port", "5000", "--receiving-account", "accB"]
    )

    # assert
    expected = (2, {"error": "host '10.0.1.5' is not a CATS listener of receiving account 'accB'."}, False)
    assert (result.exit_code, json.loads(result.stderr), mock_get_account.return_value.priv.udp_call.called) == expected


def test_udp_call_dns_failure_emits_reason_with_correlation_id(mock_get_account: MagicMock, cli_runner: CliRunner) -> None:
    # arrange
    mock_get_account.return_value.priv.udp_call.return_value = CatsNetworkResult(
        response=CatsNetworkResponse.DNS_FAILURE, reason="[Errno -2] Name or service not known", correlation_id="abc-123"
    )
    expected = {"result": "DNS_FAILURE", "reason": "[Errno -2] Name or service not known", "correlation_id": "abc-123"}

    # act
    result = cli_runner.invoke(
        cats_cli, ["run", "udp-call", "--account", "accA", "--target", "priv", "--host", "nonexistent.invalid", "--port", "5000"]
    )

    # assert
    assert result.exit_code == 0
    assert json.loads(result.stdout) == expected


def test_receive_s3_polls_by_raw_id_and_emits_received(mock_get_account: MagicMock, cli_runner: CliRunner) -> None:
    # arrange - a different process produced the ID, so the CLI cannot hold the SENT result
    mock_get_account.return_value.priv._receive_s3_by_id.return_value = True

    # act
    result = cli_runner.invoke(cats_cli, ["run", "receive-s3", "--account", "accA", "--target", "priv", "--correlation-id", "abc-123"])

    # assert
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"received": True}
    mock_get_account.return_value.priv._receive_s3_by_id.assert_called_once_with("abc-123")


def test_cats_error_exits_nonzero_with_json_stderr(mock_get_account: MagicMock, cli_runner: CliRunner) -> None:
    # arrange
    mock_get_account.side_effect = CatsInfrastructureError("stack not found")

    # act
    result = cli_runner.invoke(cats_cli, ["run", "http-call", "--account", "accA", "--target", "priv", "--host", "x", "--port", "1"])

    # assert
    assert result.exit_code == 1
    assert json.loads(result.stderr) == {"error": "stack not found"}


# =============================================================================
# `cats run write-profile` tests
# =============================================================================

WRITER_RESULT = {
    "profile_name": "cats-accA-default",
    "profile_file": "/home/u/.aws/config",
    "role_arn": "arn:aws:iam::111111111111:role/CATS-accA-default-test-role",
    "parent": "source_profile = parent",
}


@pytest.fixture
def mock_write_cats_profile(mocker: MockerFixture) -> MagicMock:
    """Patch write_cats_profile as imported inside cats.cli.run; its own behavior is covered in test_aws_profile.py."""
    mock: MagicMock = mocker.patch("cats.cli.run.write_cats_profile")
    mock.return_value = WRITER_RESULT
    return mock


@pytest.fixture
def write_profile_runner() -> CliRunner:
    """A runner for `cats run write-profile`, neutralizing AWS_CONFIG_FILE.

    The command reads AWS_CONFIG_FILE as its default output file, so a shell that exports it
    (this sandbox does) would change the Path the writer is called with. The shared `cli_runner`
    leaves AWS_CONFIG_FILE alone on purpose, because infra-command tests resolve a real config
    through it; only the writer tests want it gone.
    """
    return CliRunner(env={"COLUMNS": "200", "NO_COLOR": "1", "AWS_SHARED_CREDENTIALS_FILE": None, "AWS_CONFIG_FILE": None})


def test_write_profile_emits_writer_result(mock_write_cats_profile: MagicMock, write_profile_runner: CliRunner) -> None:
    # act
    result = write_profile_runner.invoke(cats_cli, ["run", "write-profile", "--account", "accA"])

    # assert
    assert (result.exit_code, json.loads(result.stdout)) == (0, WRITER_RESULT)
    mock_write_cats_profile.assert_called_once_with("accA", "default", None, None, None)


def test_write_profile_forwards_role(mock_write_cats_profile: MagicMock, write_profile_runner: CliRunner) -> None:
    # act
    write_profile_runner.invoke(cats_cli, ["run", "write-profile", "--account", "accA", "--role", "readonly"])

    # assert
    mock_write_cats_profile.assert_called_once_with("accA", "readonly", None, None, None)


def test_write_profile_forwards_profile_name_override(mock_write_cats_profile: MagicMock, write_profile_runner: CliRunner) -> None:
    # act
    write_profile_runner.invoke(cats_cli, ["run", "write-profile", "--account", "accA", "--profile-name", "cats-custom"])

    # assert
    mock_write_cats_profile.assert_called_once_with("accA", "default", "cats-custom", None, None)


def test_write_profile_forwards_profile_file_override(mock_write_cats_profile: MagicMock, write_profile_runner: CliRunner) -> None:
    # act
    write_profile_runner.invoke(cats_cli, ["run", "write-profile", "--account", "accA", "--profile-file", "/tmp/c"])  # noqa: S108

    # assert
    mock_write_cats_profile.assert_called_once_with("accA", "default", None, Path("/tmp/c"), None)  # noqa: S108


def test_write_profile_honours_aws_config_file_env(mock_write_cats_profile: MagicMock) -> None:
    # arrange
    runner = CliRunner(env={"COLUMNS": "200", "NO_COLOR": "1", "AWS_CONFIG_FILE": "/env/config"})

    # act
    runner.invoke(cats_cli, ["run", "write-profile", "--account", "accA"])

    # assert
    mock_write_cats_profile.assert_called_once_with("accA", "default", None, Path("/env/config"), None)


def test_write_profile_file_flag_beats_env_var(mock_write_cats_profile: MagicMock) -> None:
    # arrange
    runner = CliRunner(env={"COLUMNS": "200", "NO_COLOR": "1", "AWS_CONFIG_FILE": "/env/config"})

    # act
    runner.invoke(cats_cli, ["run", "write-profile", "--account", "accA", "--profile-file", "/flag/config"])

    # assert
    mock_write_cats_profile.assert_called_once_with("accA", "default", None, Path("/flag/config"), None)


def test_write_profile_runner_isolates_a_shell_aws_config_file(
    mock_write_cats_profile: MagicMock, write_profile_runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The developer's or CI runner's shell must not change what the writer is called with."""
    # arrange
    monkeypatch.setenv("AWS_CONFIG_FILE", "/shell/config")

    # act
    write_profile_runner.invoke(cats_cli, ["run", "write-profile", "--account", "accA"])

    # assert
    mock_write_cats_profile.assert_called_once_with("accA", "default", None, None, None)


def test_write_profile_forwards_config_flag(mock_write_cats_profile: MagicMock, write_profile_runner: CliRunner) -> None:
    # act
    write_profile_runner.invoke(cats_cli, ["run", "write-profile", "--account", "accA", "--config", "/tmp/other.yaml"])  # noqa: S108

    # assert
    mock_write_cats_profile.assert_called_once_with("accA", "default", None, None, Path("/tmp/other.yaml"))  # noqa: S108


def test_write_profile_usage_error_exits_2(mock_write_cats_profile: MagicMock, write_profile_runner: CliRunner) -> None:
    """The prefix guard and the config-file guard are the caller's own input: exit 2, nothing on stdout."""
    # arrange
    mock_write_cats_profile.side_effect = CatsUsageError("profile_name must start with 'cats-' (got 'default')")

    # act
    result = write_profile_runner.invoke(cats_cli, ["run", "write-profile", "--account", "accA", "--profile-name", "default"])

    # assert
    expected = (2, "", {"error": "profile_name must start with 'cats-' (got 'default')"})
    assert (result.exit_code, result.stdout, json.loads(result.stderr)) == expected


def test_write_profile_unknown_role_exits_2(mock_write_cats_profile: MagicMock, write_profile_runner: CliRunner) -> None:
    """An unknown --role is the writer's CatsUsageError (checked from cats.yaml before any AWS call), reported as exit 2."""
    # arrange
    message = "Role 'nope' is not a name under additional_roles in cats.yaml for account 'accA'. Available roles: ['default', 'readonly']"
    mock_write_cats_profile.side_effect = CatsUsageError(message)

    # act
    result = write_profile_runner.invoke(cats_cli, ["run", "write-profile", "--account", "accA", "--role", "nope"])

    # assert
    expected = (2, {"error": message})
    assert (result.exit_code, json.loads(result.stderr)) == expected


def test_write_profile_infrastructure_error_exits_1(mock_write_cats_profile: MagicMock, write_profile_runner: CliRunner) -> None:
    # arrange
    mock_write_cats_profile.side_effect = CatsInfrastructureError("stack not found")

    # act
    result = write_profile_runner.invoke(cats_cli, ["run", "write-profile", "--account", "accA"])

    # assert
    assert (result.exit_code, json.loads(result.stderr)) == (1, {"error": "stack not found"})


def test_write_profile_has_no_format_option(mock_write_cats_profile: MagicMock, write_profile_runner: CliRunner) -> None:
    """There is no option that prints credentials: `--format` is a usage error, not ignored."""
    # act
    result = write_profile_runner.invoke(cats_cli, ["run", "write-profile", "--account", "accA", "--format", "export"])

    # assert
    assert (result.exit_code, result.stdout, mock_write_cats_profile.called) == (2, "", False)


def test_creds_is_not_a_command(mock_write_cats_profile: MagicMock, write_profile_runner: CliRunner) -> None:
    """`cats run` has no subcommand that hands out credentials; `write-profile` is the only credential-adjacent one."""
    # act
    result = write_profile_runner.invoke(cats_cli, ["run", "creds", "--account", "accA"])

    # assert
    assert (result.exit_code, result.stdout, mock_write_cats_profile.called) == (2, "", False)


# =============================================================================
# `cats run shell` tests
# =============================================================================


def test_shell_emits_result_json(mock_get_account: MagicMock, cli_runner: CliRunner) -> None:
    # arrange
    mock_get_account.return_value.priv.shell.return_value = ShellResult(
        stdout="hi",
        stderr="",
        exit_code=0,
        duration_ms=120,
        stdout_truncated=False,
        stderr_truncated=False,
    )
    expected = {
        "stdout": "hi",
        "stderr": "",
        "exit_code": 0,
        "duration_ms": 120,
        "stdout_truncated": False,
        "stderr_truncated": False,
    }

    # act
    result = cli_runner.invoke(cats_cli, ["run", "shell", "--account", "accA", "--target", "priv", "--command", "echo hi"])

    # assert
    assert result.exit_code == 0
    assert json.loads(result.stdout) == expected
    mock_get_account.return_value.priv.shell.assert_called_once_with(["echo hi"])


def test_shell_multiple_commands_forwarded_as_list(mock_get_account: MagicMock, cli_runner: CliRunner) -> None:
    # arrange
    mock_get_account.return_value.priv.shell.return_value = ShellResult(
        stdout="",
        stderr="",
        exit_code=0,
        duration_ms=1,
        stdout_truncated=False,
        stderr_truncated=False,
    )

    # act
    cli_runner.invoke(
        cats_cli,
        ["run", "shell", "--account", "accA", "--target", "priv", "--command", "cd /tmp", "--command", "ls"],
    )

    # assert
    mock_get_account.return_value.priv.shell.assert_called_once_with(["cd /tmp", "ls"])


def test_shell_without_command_is_a_typer_usage_error(cli_runner: CliRunner) -> None:
    """--command is required like --account and --target: its absence is Click's usage error, exit 2, nothing on stdout."""
    # act
    result = cli_runner.invoke(cats_cli, ["run", "shell", "--account", "accA", "--target", "priv"])

    # assert
    assert (result.exit_code, result.stdout) == (2, "")


def test_shell_with_an_empty_command_exits_2_as_json(cli_runner: CliRunner) -> None:
    """An empty command line passes Click and is CATS's own usage error, on the JSON stderr contract."""
    # act
    result = cli_runner.invoke(cats_cli, ["run", "shell", "--account", "accA", "--target", "priv", "--command", ""])

    # assert
    expected = (2, {"error": "--command must be non-empty. Repeat for multi-line scripts."})
    assert (result.exit_code, json.loads(result.stderr)) == expected


def test_shell_config_error_exits_two(mock_get_account: MagicMock, cli_runner: CliRunner) -> None:
    # arrange
    mock_get_account.return_value.priv.shell.side_effect = CatsConfigError("allow_shell not enabled")

    # act
    result = cli_runner.invoke(cats_cli, ["run", "shell", "--account", "accA", "--target", "priv", "--command", "echo"])

    # assert
    assert result.exit_code == 2
    assert json.loads(result.stderr) == {"error": "allow_shell not enabled"}


def test_unknown_target_emits_json_stderr_and_exit_2(mock_get_account: MagicMock, cli_runner: CliRunner) -> None:
    """An unknown --target is the caller's input: exit 2 with the targets that exist, not a traceback."""
    # act
    result = cli_runner.invoke(cats_cli, ["run", "http-call", "--account", "accA", "--target", "nope", "--host", "10.0.1.5", "--port", "80"])

    # assert
    expected = {"error": "Network target 'nope' not found for account 'accA'. Available targets: ['priv']"}
    assert result.exit_code == 2
    assert json.loads(result.stderr) == expected


def test_attribute_error_inside_a_call_is_a_traceback_not_a_usage_error(mock_get_account: MagicMock, cli_runner: CliRunner) -> None:
    """An AttributeError raised by a bug inside the probe is not the caller's mistake: it must not be reported as exit 2."""
    # arrange
    mock_get_account.return_value.priv.http_call.side_effect = AttributeError("'NoneType' object has no attribute 'dns_name'")

    # act
    result = cli_runner.invoke(cats_cli, ["run", "http-call", "--account", "accA", "--target", "priv", "--host", "10.0.1.5", "--port", "80"])

    # assert
    assert (result.exit_code, type(result.exception), result.stderr) == (1, AttributeError, "")


def test_client_error_emits_json_stderr_and_exit_1(mock_get_account: MagicMock, cli_runner: CliRunner) -> None:
    """A raw AWS ClientError (expired creds, throttling) becomes stderr JSON, not a traceback."""
    # arrange
    error = ClientError({"Error": {"Code": "ExpiredToken", "Message": "The security token is expired"}}, "AssumeRole")
    mock_get_account.return_value.priv.http_call.side_effect = error

    # act
    result = cli_runner.invoke(cats_cli, ["run", "http-call", "--account", "accA", "--target", "priv", "--host", "10.0.1.5", "--port", "80"])

    # assert
    expected = {"error": "An error occurred (ExpiredToken) when calling the AssumeRole operation: The security token is expired"}
    assert result.exit_code == 1
    assert json.loads(result.stderr) == expected


def test_http_call_passes_config_to_get_account(mock_get_account: MagicMock, cli_runner: CliRunner) -> None:
    # arrange
    mock_get_account.return_value.priv.http_call.return_value = CatsNetworkResult(response=CatsNetworkResponse.SUCCESS, http_status=200)

    # act
    result = cli_runner.invoke(
        cats_cli,
        ["run", "http-call", "--account", "accA", "--target", "priv", "--host", "10.0.1.5", "--port", "80", "--config", "other/cats.yaml"],
    )

    # assert
    assert result.exit_code == 0
    assert mock_get_account.call_args == call(Path("other/cats.yaml"), "accA")


def test_environment_failure_is_a_json_error_not_a_traceback(mock_get_account: MagicMock, cli_runner: CliRunner) -> None:
    """ProfileNotFound and its botocore siblings honor the cats run error contract."""
    # arrange
    mock_get_account.side_effect = ProfileNotFound(profile="missing")

    # act
    result = cli_runner.invoke(cats_cli, ["run", "http-call", "--account", "accA", "--target", "priv", "--host", "x", "--port", "1"])

    # assert
    expected = (1, {"error": "The config profile (missing) could not be found"})
    assert expected == (result.exit_code, json.loads(result.stderr))


def test_bad_port_exits_two_before_any_aws_call(mock_get_account: MagicMock, cli_runner: CliRunner) -> None:
    """A caller's mistake is exit 2 and never pays for an AssumeRole."""
    # act
    result = cli_runner.invoke(cats_cli, ["run", "tcp-call", "--account", "accA", "--target", "priv", "--host", "10.0.1.5", "--port", "70000"])

    # assert
    expected = (2, {"error": "port must be an integer in 1..65535, got 70000"}, False)
    assert (result.exit_code, json.loads(result.stderr), mock_get_account.called) == expected


def test_url_as_host_exits_two_before_any_aws_call(mock_get_account: MagicMock, cli_runner: CliRunner) -> None:
    # act
    args = ["run", "http-call", "--account", "accA", "--target", "priv", "--host", "https://x.example", "--port", "443"]
    result = cli_runner.invoke(cats_cli, args)

    # assert
    expected = (
        2,
        {
            "error": "host must be a hostname or IP literal, not a URL or host:port, got 'https://x.example' "
            + "(the method you call chooses the protocol; pass the port as the second argument)"
        },
        False,
    )
    assert (result.exit_code, json.loads(result.stderr), mock_get_account.called) == expected


def test_short_config_flag_is_accepted(mock_get_account: MagicMock, cli_runner: CliRunner) -> None:
    # arrange
    mock_get_account.return_value.priv.tcp_call.return_value = CatsNetworkResult(response=CatsNetworkResponse.SUCCESS)

    # act
    args = ["run", "tcp-call", "--account", "accA", "--target", "priv", "--host", "10.0.1.5", "--port", "80", "-c", "x/cats.yaml"]
    result = cli_runner.invoke(cats_cli, args)

    # assert
    assert (result.exit_code, mock_get_account.call_args) == (0, call(Path("x/cats.yaml"), "accA"))


def test_missing_config_file_exits_two(cli_runner: CliRunner, tmp_path: Path) -> None:
    """A --config path that is not a file is the caller's configuration, so exit 2 with the path named."""
    # arrange
    missing = tmp_path / "nope.yaml"

    # act
    result = cli_runner.invoke(cats_cli, ["run", "account-info", "--account", "accA", "--config", str(missing)])

    # assert
    expected = (2, {"error": f"Config file not found: {missing}"})
    assert (result.exit_code, json.loads(result.stderr)) == expected
