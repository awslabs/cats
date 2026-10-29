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

"""Tests for the `cats mcp` command: starting the FastMCP server, and the missing-extra path."""

import sys
from unittest.mock import MagicMock

import fastmcp
from pytest_mock import MockerFixture
from typer.testing import CliRunner

from cats.cli import cats_cli
from cats.cli.mcp import mcp
from cats.version import __version__


def test_mcp_command_starts_fastmcp_stdio_without_the_banner(mocker: MockerFixture, cli_runner: CliRunner) -> None:
    """FastMCP's banner advertises a third-party product and calls PyPI for an upgrade hint; `cats mcp` starts silent."""
    # arrange
    mock_run: MagicMock = mocker.patch("cats.cli.mcp.mcp.run")
    mocker.patch.object(fastmcp.settings, "check_for_updates", "stable")  # main() sets it; keep that out of other tests

    # act
    result = cli_runner.invoke(cats_cli, ["mcp"])

    # assert
    assert result.exit_code == 0
    mock_run.assert_called_once_with(show_banner=False)


def test_mcp_command_turns_fastmcp_update_checks_off(mocker: MockerFixture, cli_runner: CliRunner) -> None:
    """README promises nothing beyond the User-Agent token is transmitted: the PyPI version check is off, whatever FastMCP's default."""
    # arrange
    mocker.patch("cats.cli.mcp.mcp.run")
    mocker.patch.object(fastmcp.settings, "check_for_updates", "stable")

    # act
    cli_runner.invoke(cats_cli, ["mcp"])

    # assert
    assert fastmcp.settings.check_for_updates == "off"


def test_server_reports_the_cats_release_as_its_version() -> None:
    """serverInfo.version in the initialize handshake is CATS's release, not FastMCP's."""
    # act
    result = mcp.version

    # assert
    assert result == __version__


def test_missing_fastmcp_prints_install_hint_to_stderr_and_exits_1(mocker: MockerFixture, cli_runner: CliRunner) -> None:
    """stdout is the MCP protocol channel: an MCP host must see the hint in its stderr log, not a JSON parse error."""
    # arrange - re-import cats.cli.mcp with the fastmcp distribution absent
    mocker.patch.dict(sys.modules, {"fastmcp": None})
    sys.modules.pop("cats.cli.mcp", None)
    expected_stderr = "cats mcp requires the MCP extra. Install it with: pip install 'aws-cats[mcp]'  (or: uv add 'aws-cats[mcp]')\n"

    # act
    result = cli_runner.invoke(cats_cli, ["mcp"])

    # assert
    assert (result.exit_code, result.stdout, result.stderr) == (1, "", expected_stderr)


def test_unrelated_import_error_is_not_reported_as_the_missing_extra(mocker: MockerFixture, cli_runner: CliRunner) -> None:
    """A broken import inside cats.cli.mcp must surface as itself, not as 'install aws-cats[mcp]'."""
    # arrange - fastmcp is present, a CATS module the server imports is not
    mocker.patch.dict(sys.modules, {"cats.utils.aws_profile": None})
    sys.modules.pop("cats.cli.mcp", None)

    # act
    result = cli_runner.invoke(cats_cli, ["mcp"])

    # assert
    assert (isinstance(result.exception, ModuleNotFoundError), result.stderr) == (True, "")
