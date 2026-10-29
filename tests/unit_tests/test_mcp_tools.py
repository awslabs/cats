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

"""Tests for cats.cli.mcp tool wrappers."""

import asyncio
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, call

import pytest
from botocore.exceptions import ClientError
from pytest_mock import MockerFixture

from cats.cfn.deploy import StackState
from cats.cli.mcp import (
    _CATS_CONFIG_PATH,
    _CATS_REPORTING_PATH,
    INSTRUCTIONS,
    account_info,
    http_call,
    infra_status_tool,
    list_accounts,
    mcp,
    receive_s3,
    shell,
    tcp_call,
    udp_call,
    validate_tool,
    write_profile,
)
from cats.constants import CatsNetworkResponse, CatsNetworkResult, DeployedEC2Resources, DeployedTargetResources, ShellResult
from cats.exceptions import CatsConfigError, CatsError, CatsInfrastructureError, CatsUsageError
from tests.utils import make_config, write_config, write_isolated_yaml


@pytest.fixture
def mock_get_account(mocker: MockerFixture) -> MagicMock:
    """Patch get_account as it is imported inside cats.cli.mcp."""
    mock: MagicMock = mocker.patch("cats.cli.mcp.get_account")
    return mock


def test_account_info_returns_runtime_metadata(mock_get_account: MagicMock) -> None:
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
    result = account_info(account="accA")

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
    assert result == expected


def test_http_call_returns_result_reason_and_http_status(mock_get_account: MagicMock) -> None:
    # arrange
    mock_get_account.return_value.priv.http_call.return_value = CatsNetworkResult(response=CatsNetworkResponse.SUCCESS, http_status=200)
    expected = {"result": "SUCCESS", "reason": None, "http_status": 200}

    # act
    result = http_call(account="accA", target="priv", host="10.0.1.5", port=8080)

    # assert
    assert result == expected


def test_http_call_failure_returns_reason_and_null_http_status(mock_get_account: MagicMock) -> None:
    # arrange
    mock_get_account.return_value.priv.http_call.return_value = CatsNetworkResult(
        response=CatsNetworkResponse.TLS_FAILURE, reason="[SSL: WRONG_VERSION_NUMBER] wrong version number"
    )
    expected = {"result": "TLS_FAILURE", "reason": "[SSL: WRONG_VERSION_NUMBER] wrong version number", "http_status": None}

    # act
    result = http_call(account="accA", target="priv", host="10.0.1.5", port=8080)

    # assert
    assert result == expected


def test_tcp_call_returns_result_reason_and_null_http_status(mock_get_account: MagicMock) -> None:
    # arrange
    mock_get_account.return_value.priv.tcp_call.return_value = CatsNetworkResult(
        response=CatsNetworkResponse.FAILURE, reason="[Errno 101] Network is unreachable"
    )
    expected = {"result": "FAILURE", "reason": "[Errno 101] Network is unreachable", "http_status": None}

    # act
    result = tcp_call(account="accA", target="priv", host="10.0.1.5", port=22)

    # assert
    assert result == expected


def test_udp_call_returns_result_reason_and_correlation_id(mock_get_account: MagicMock) -> None:
    # arrange
    mock_get_account.return_value.priv.udp_call.return_value = CatsNetworkResult(response=CatsNetworkResponse.SENT, correlation_id="abc-123")
    expected = {"result": "SENT", "reason": None, "correlation_id": "abc-123"}

    # act
    result = udp_call(account="accA", target="priv", host="10.0.1.5", port=5000)

    # assert
    assert result == expected


@pytest.fixture
def mock_prepare_udp_receiver(mocker: MockerFixture) -> MagicMock:
    """Patch prepare_udp_receiver as it is imported inside cats.cli.mcp; its own behavior is covered in test_prepare_udp_receiver.py."""
    mock: MagicMock = mocker.patch("cats.cli.mcp.prepare_udp_receiver")
    return mock


def test_udp_call_without_receiving_account_constructs_only_the_sender(mock_get_account: MagicMock, mock_prepare_udp_receiver: MagicMock) -> None:
    # arrange
    mock_get_account.return_value.priv.udp_call.return_value = CatsNetworkResult(response=CatsNetworkResponse.SENT, correlation_id="abc-123")

    # act
    udp_call(account="accA", target="priv", host="10.0.1.5", port=5000)

    # assert
    assert (mock_prepare_udp_receiver.called, mock_get_account.call_args_list) == (False, [call(None, "accA")])


def test_udp_call_with_receiving_account_prepares_the_receiver_before_sending(
    mock_get_account: MagicMock, mock_prepare_udp_receiver: MagicMock
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
    result = udp_call(
        account="accA", target="priv", host="ip-10-0-2-9.ec2.internal", port=5000, receiving_account="accB", config_path="other/cats.yaml"
    )

    # assert
    expected = (
        {"result": "SENT", "reason": None, "correlation_id": "abc-123"},
        [call(Path("other/cats.yaml"), "accB", "ip-10-0-2-9.ec2.internal")],
        ["prepare", "send"],
    )
    assert (result, mock_prepare_udp_receiver.call_args_list, order) == expected


def test_udp_call_host_that_is_not_the_receivers_listener_raises_before_sending(
    mock_get_account: MagicMock, mock_prepare_udp_receiver: MagicMock
) -> None:
    # arrange
    mock_prepare_udp_receiver.side_effect = CatsUsageError("host '10.0.1.5' is not a CATS listener of receiving account 'accB'.")

    # act & assert
    with pytest.raises(CatsUsageError, match=r"^host '10\.0\.1\.5' is not a CATS listener of receiving account 'accB'\.$"):
        udp_call(account="accA", target="priv", host="10.0.1.5", port=5000, receiving_account="accB")
    assert mock_get_account.return_value.priv.udp_call.called is False


def test_receive_s3_polls_by_raw_id_and_returns_received_flag(mock_get_account: MagicMock) -> None:
    # arrange
    mock_get_account.return_value.priv._receive_s3_by_id.return_value = True

    # act
    result = receive_s3(account="accB", target="priv", correlation_id="abc-123")

    # assert
    assert result == {"received": True}
    mock_get_account.return_value.priv._receive_s3_by_id.assert_called_once_with("abc-123")


def test_tool_forwards_explicit_config_path(mock_get_account: MagicMock) -> None:
    # arrange
    mock_get_account.return_value.priv.http_call.return_value = CatsNetworkResult(response=CatsNetworkResponse.SUCCESS)

    # act
    http_call(account="accA", target="priv", host="x", port=1, config_path="/tmp/other.yaml")  # noqa: S108

    # assert
    mock_get_account.assert_called_once_with(Path("/tmp/other.yaml"), "accA")  # noqa: S108


def test_tool_expands_a_tilde_in_config_path(mock_get_account: MagicMock) -> None:
    """config_path gets the same `~` treatment as profile_file: the MCP host's shell did not expand it."""
    # arrange
    mock_get_account.return_value.priv.http_call.return_value = CatsNetworkResult(response=CatsNetworkResponse.SUCCESS)

    # act
    http_call(account="accA", target="priv", host="x", port=1, config_path="~/proj/cats.yaml")

    # assert
    mock_get_account.assert_called_once_with(Path.home() / "proj" / "cats.yaml", "accA")


def test_tool_defaults_config_path_to_none(mock_get_account: MagicMock) -> None:
    # arrange
    mock_get_account.return_value.priv.http_call.return_value = CatsNetworkResult(response=CatsNetworkResponse.SUCCESS)

    # act
    http_call(account="accA", target="priv", host="x", port=1)

    # assert
    mock_get_account.assert_called_once_with(None, "accA")


def test_tool_propagates_cats_error(mock_get_account: MagicMock) -> None:
    # arrange
    mock_get_account.side_effect = CatsInfrastructureError("stack not found")

    # act / assert
    with pytest.raises(CatsInfrastructureError, match="stack not found"):
        http_call(account="accA", target="priv", host="x", port=1)


def test_list_accounts_returns_config_summary(cats_yaml: Path) -> None:
    # arrange
    write_config(
        cats_yaml,
        make_config(
            accounts={"accA": "111111111111", "accB": "222222222222"},
            network_targets={"priv": {"vpc_id": "vpc-1", "subnet_ids": ["subnet-1"]}},
        ),
    )

    # act
    result = list_accounts()

    # assert
    expected = {
        "accounts": [
            {"name": "accA", "account_id": "111111111111", "tags": {}, "targets": ["priv"]},
            {"name": "accB", "account_id": "222222222222", "tags": {}, "targets": ["priv"]},
        ]
    }
    assert result == expected


@pytest.fixture
def mock_write_cats_profile(mocker: MockerFixture) -> MagicMock:
    """Patch write_cats_profile as imported inside cats.cli.mcp."""
    mock: MagicMock = mocker.patch("cats.cli.mcp.write_cats_profile")
    mock.return_value = {
        "profile_name": "cats-accA-default",
        "profile_file": "/home/u/.aws/config",
        "role_arn": "arn:aws:iam::111111111111:role/CATS-accA-default-test-role",
        "parent": "source_profile = parent",
    }
    return mock


def test_write_profile_returns_writer_result(mock_write_cats_profile: MagicMock) -> None:
    # act
    result = write_profile(account="accA")

    # assert
    expected = {
        "profile_name": "cats-accA-default",
        "profile_file": "/home/u/.aws/config",
        "role_arn": "arn:aws:iam::111111111111:role/CATS-accA-default-test-role",
        "parent": "source_profile = parent",
    }
    assert result == expected


def test_write_profile_always_writes_the_default_config_file(mock_write_cats_profile: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    """The MCP tool names no file: the writer gets None (its ~/.aws/config default), whatever AWS_CONFIG_FILE says in this process."""
    # arrange
    monkeypatch.setenv("AWS_CONFIG_FILE", "/env/config")

    # act
    write_profile(account="accA")

    # assert
    mock_write_cats_profile.assert_called_once_with("accA", "default", None, None, None)


def test_write_profile_tool_exposes_no_file_argument() -> None:
    """An agent reading untrusted tool output must not be able to aim the writer at another INI-shaped file (.gitconfig, .pypirc, tox.ini)."""
    # act
    tool = asyncio.run(mcp.get_tool("cats_write_profile"))

    # assert
    expected = ["account", "role", "profile_name", "config_path"]
    assert tool is not None
    assert list(tool.parameters["properties"]) == expected


def test_write_profile_propagates_cats_error(mocker: MockerFixture) -> None:
    # arrange
    mocker.patch("cats.cli.mcp.write_cats_profile", side_effect=CatsError("profile_name must start with 'cats-'"))

    # act / assert
    with pytest.raises(CatsError, match="profile_name must start with 'cats-'"):
        write_profile(account="accA", profile_name="default")


def test_shell_returns_shell_result_dict(mock_get_account: MagicMock) -> None:
    # arrange
    mock_get_account.return_value.priv.shell.return_value = ShellResult(
        stdout="hi",
        stderr="",
        exit_code=0,
        duration_ms=5,
        stdout_truncated=False,
        stderr_truncated=False,
    )
    expected = {
        "stdout": "hi",
        "stderr": "",
        "exit_code": 0,
        "duration_ms": 5,
        "stdout_truncated": False,
        "stderr_truncated": False,
    }

    # act
    result = shell(account="accA", target="priv", command=["echo hi"])

    # assert
    assert result == expected
    mock_get_account.return_value.priv.shell.assert_called_once_with(["echo hi"])


def test_validate_tool_returns_valid_for_good_config(cats_yaml: Path) -> None:
    # arrange
    write_config(cats_yaml, make_config(accounts={"myaccount": "234567890123"}))
    expected = {"valid": True, "message": "Valid", "config_path": str(cats_yaml)}

    # act
    result = validate_tool()

    # assert
    assert result == expected


def test_validate_tool_returns_invalid_for_bad_config(isolated_yaml: Path) -> None:
    # arrange
    write_isolated_yaml(isolated_yaml, {"unknown_field": "value"})
    expected_message = """1 validation error for CatsConfig
  unknown key 'unknown_field' [type=unknown_key]"""
    expected = {"valid": False, "message": expected_message, "config_path": str(isolated_yaml)}

    # act
    result = validate_tool(config_path=str(isolated_yaml))

    # assert
    assert result == expected


def test_validate_tool_does_not_echo_the_files_values(isolated_yaml: Path) -> None:
    """The client is an agent acting on untrusted text; a report quoting input_value would read any YAML the server can
    (the CLI's report, for the user reading their own file, keeps the values)."""
    # arrange
    write_isolated_yaml(isolated_yaml, {"accounts_under_test": {"prod": {"account_id": "111111111111"}}, "db_password": "hunter2-super-secret"})
    expected_message = """1 validation error for CatsConfig
  unknown key 'db_password' [type=unknown_key]"""

    # act
    result = validate_tool(config_path=str(isolated_yaml))

    # assert
    assert result == {"valid": False, "message": expected_message, "config_path": str(isolated_yaml)}


def test_validate_tool_forwards_explicit_config_path_as_path(mocker: MockerFixture) -> None:
    # arrange
    mock: MagicMock = mocker.patch("cats.cli.mcp.validate_config")
    mock.return_value = (True, "Valid", Path("/tmp/other.yaml"))  # noqa: S108

    # act
    validate_tool(config_path="/tmp/other.yaml")  # noqa: S108

    # assert
    mock.assert_called_once_with(Path("/tmp/other.yaml"), include_input=False)  # noqa: S108


def test_validate_tool_passes_none_when_config_path_omitted(mocker: MockerFixture) -> None:
    # arrange
    mock: MagicMock = mocker.patch("cats.cli.mcp.validate_config")
    mock.return_value = (True, "Valid", Path("./cats.yaml"))

    # act
    validate_tool()

    # assert
    mock.assert_called_once_with(None, include_input=False)


@pytest.fixture
def mock_get_deploy_session(mocker: MockerFixture) -> MagicMock:
    """Patch get_deploy_session as imported inside cats.cli.mcp."""
    mock: MagicMock = mocker.patch("cats.cli.mcp.get_deploy_session")
    return mock


@pytest.fixture
def installed_version(mocker: MockerFixture) -> str:
    """Pin the installed release reported by cats_infra_status so expectations survive version bumps."""
    mocker.patch("cats.cli.mcp.__version__", "1.0.0")
    return "1.0.0"


@pytest.fixture
def mock_get_stack_state(mocker: MockerFixture) -> MagicMock:
    """Patch get_stack_state as imported inside cats.cli.mcp."""
    mock: MagicMock = mocker.patch("cats.cli.mcp.get_stack_state")
    return mock


def healthy_state(version: str | None = "1.0.0") -> StackState:
    """StackState for a CREATE_COMPLETE stack deployed by the given CATS release."""
    return StackState(status="CREATE_COMPLETE", cats_managed=True, has_udp_receipt_token=True, version=version, account_keyname=None)


def test_infra_status_returns_entry_per_account(
    cats_yaml: Path,
    installed_version: str,
    mock_get_deploy_session: MagicMock,
    mock_get_stack_state: MagicMock,
) -> None:
    # arrange
    write_config(cats_yaml, make_config(accounts={"accA": "111111111111", "accB": "222222222222"}))
    mock_get_stack_state.return_value = healthy_state()
    expected = {
        "installed_version": "1.0.0",
        "accounts": [
            {"account": "accA", "stack_name": "CATS-test-infra-accA", "status": "CREATE_COMPLETE", "version": "1.0.0", "error": None},
            {"account": "accB", "stack_name": "CATS-test-infra-accB", "status": "CREATE_COMPLETE", "version": "1.0.0", "error": None},
        ],
    }

    # act
    result = infra_status_tool()

    # assert
    assert result == expected


def test_infra_status_reports_version_of_stack_from_other_release(
    cats_yaml: Path,
    installed_version: str,
    mock_get_deploy_session: MagicMock,
    mock_get_stack_state: MagicMock,
) -> None:
    # arrange
    write_config(cats_yaml, make_config(accounts={"accA": "111111111111"}))
    mock_get_stack_state.return_value = healthy_state(version="0.99.0")
    expected = {
        "installed_version": "1.0.0",
        "accounts": [
            {"account": "accA", "stack_name": "CATS-test-infra-accA", "status": "CREATE_COMPLETE", "version": "0.99.0", "error": None},
        ],
    }

    # act
    result = infra_status_tool()

    # assert
    assert result == expected


def test_infra_status_reports_none_version_for_stack_without_tag(
    cats_yaml: Path,
    installed_version: str,
    mock_get_deploy_session: MagicMock,
    mock_get_stack_state: MagicMock,
) -> None:
    # arrange
    write_config(cats_yaml, make_config(accounts={"accA": "111111111111"}))
    mock_get_stack_state.return_value = healthy_state(version=None)
    expected = {
        "installed_version": "1.0.0",
        "accounts": [
            {"account": "accA", "stack_name": "CATS-test-infra-accA", "status": "CREATE_COMPLETE", "version": None, "error": None},
        ],
    }

    # act
    result = infra_status_tool()

    # assert
    assert result == expected


def test_infra_status_filters_to_one_account_when_specified(
    cats_yaml: Path,
    installed_version: str,
    mock_get_deploy_session: MagicMock,
    mock_get_stack_state: MagicMock,
) -> None:
    # arrange
    write_config(cats_yaml, make_config(accounts={"accA": "111111111111", "accB": "222222222222"}))
    mock_get_stack_state.return_value = healthy_state()
    expected = {
        "installed_version": "1.0.0",
        "accounts": [
            {"account": "accB", "stack_name": "CATS-test-infra-accB", "status": "CREATE_COMPLETE", "version": "1.0.0", "error": None},
        ],
    }

    # act
    result = infra_status_tool(account="accB")

    # assert
    assert result == expected


def test_infra_status_reports_none_status_for_undeployed_stack(
    cats_yaml: Path,
    installed_version: str,
    mock_get_deploy_session: MagicMock,
    mock_get_stack_state: MagicMock,
) -> None:
    # arrange
    write_config(cats_yaml, make_config(accounts={"accA": "111111111111"}))
    mock_get_stack_state.return_value = None
    expected = {
        "installed_version": "1.0.0",
        "accounts": [
            {"account": "accA", "stack_name": "CATS-test-infra-accA", "status": None, "version": None, "error": None},
        ],
    }

    # act
    result = infra_status_tool()

    # assert
    assert result == expected


def test_infra_status_isolates_per_account_errors(
    cats_yaml: Path,
    installed_version: str,
    mock_get_stack_state: MagicMock,
    mocker: MockerFixture,
) -> None:
    # arrange
    write_config(cats_yaml, make_config(accounts={"accA": "111111111111", "accB": "222222222222"}))
    client_error = ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "AssumeRole")

    def _session(name: str) -> MagicMock:
        if name == "accA":
            raise client_error
        return MagicMock()

    mocker.patch("cats.cli.mcp.get_deploy_session", side_effect=_session)
    mock_get_stack_state.return_value = healthy_state()
    expected = {
        "installed_version": "1.0.0",
        "accounts": [
            {"account": "accA", "stack_name": "CATS-test-infra-accA", "status": None, "version": None, "error": str(client_error)},
            {"account": "accB", "stack_name": "CATS-test-infra-accB", "status": "CREATE_COMPLETE", "version": "1.0.0", "error": None},
        ],
    }

    # act
    result = infra_status_tool()

    # assert
    assert result == expected


def test_infra_status_propagates_an_unexpected_error(
    cats_yaml: Path,
    installed_version: str,
    mock_get_stack_state: MagicMock,
    mocker: MockerFixture,
) -> None:
    """Only CATS, AWS API and botocore failures are per-account `error` entries; a bug is not an account's status."""
    # arrange
    write_config(cats_yaml, make_config(accounts={"accA": "111111111111"}))
    mocker.patch("cats.cli.mcp.get_deploy_session", side_effect=TypeError("unexpected"))

    # act / assert
    with pytest.raises(TypeError, match="unexpected"):
        infra_status_tool()


def test_infra_status_returns_empty_list_for_empty_config(cats_yaml: Path, installed_version: str) -> None:
    # arrange
    config = make_config(accounts={"tmp": "111111111111"})
    config["accounts_under_test"] = {}
    write_config(cats_yaml, config)
    expected: dict[str, object] = {"installed_version": "1.0.0", "accounts": []}

    # act
    result = infra_status_tool()

    # assert
    assert result == expected


def test_infra_status_raises_for_unknown_account(cats_yaml: Path) -> None:
    # arrange
    write_config(cats_yaml, make_config(accounts={"accA": "111111111111"}))

    # act / assert
    with pytest.raises(CatsUsageError, match=r"Account 'typo' not found in config\."):
        infra_status_tool(account="typo")


def test_list_accounts_config_path_does_not_stick(cats_yaml: Path, tmp_path: Path) -> None:
    """An explicit config_path must not leak into later calls that omit it."""
    # arrange
    write_config(cats_yaml, make_config(accounts={"ambient": "111111111111"}))
    other_yaml = tmp_path / "other" / "cats.yaml"
    other_yaml.parent.mkdir()
    write_config(other_yaml, make_config(accounts={"other": "222222222222"}))

    # act
    with_path = list_accounts(config_path=str(other_yaml))
    without_path = list_accounts()

    # assert
    expected = (["other"], ["ambient"])
    assert ([a["name"] for a in with_path["accounts"]], [a["name"] for a in without_path["accounts"]]) == expected


def test_list_accounts_missing_config_path_raises(tmp_path: Path) -> None:
    """A nonexistent config_path used to silently return zero accounts."""
    # arrange
    missing = tmp_path / "nope.yaml"

    # act / assert
    with pytest.raises(CatsConfigError, match=f"Config file not found: {missing}"):
        list_accounts(config_path=str(missing))


def test_infra_status_config_path_does_not_stick(
    cats_yaml: Path,
    tmp_path: Path,
    mock_get_deploy_session: MagicMock,
    mock_get_stack_state: MagicMock,
) -> None:
    """An explicit config_path must not leak into later calls that omit it."""
    # arrange
    write_config(cats_yaml, make_config(accounts={"ambient": "111111111111"}))
    other_yaml = tmp_path / "other" / "cats.yaml"
    other_yaml.parent.mkdir()
    write_config(other_yaml, make_config(accounts={"other": "222222222222"}))
    mock_get_stack_state.return_value = healthy_state()

    # act
    with_path = cast(list[dict[str, object]], infra_status_tool(config_path=str(other_yaml))["accounts"])
    without_path = cast(list[dict[str, object]], infra_status_tool()["accounts"])

    # assert
    expected = (["other"], ["ambient"])
    assert ([e["account"] for e in with_path], [e["account"] for e in without_path]) == expected


def test_cats_config_path_points_at_file_with_cats_config_class() -> None:
    # act
    contents = _CATS_CONFIG_PATH.read_text()

    # assert
    assert "class CatsConfig" in contents


def test_cats_reporting_path_points_at_file_with_report_entry_class() -> None:
    # act
    contents = _CATS_REPORTING_PATH.read_text()

    # assert
    assert "class ReportEntry" in contents


def test_instructions_contains_resolved_config_path() -> None:
    # assert
    assert str(_CATS_CONFIG_PATH) in INSTRUCTIONS


def test_instructions_contains_resolved_reporting_path() -> None:
    # assert
    assert str(_CATS_REPORTING_PATH) in INSTRUCTIONS
