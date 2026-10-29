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

"""Tests for cats.cli.testing module (cats test command)."""

import os
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import cast
from unittest.mock import MagicMock, call

import pytest
from pytest_mock import MockerFixture
from typer.testing import CliRunner

from cats.cli import cats_cli
from cats.cli.testing import _FORWARDED_SIGNALS
from tests.utils import make_config, write_config

# What xdist and a CI's PYTEST_ADDOPTS set for THIS session; a nested real `cats test` run must not inherit it
# (the PYTEST_XDIST_* variables would make the child believe it is one of our workers, and an outer -n or --cov
# would change the run). PYTEST_CURRENT_TEST is deliberately absent: pytest sets it again at the start of the call
# phase, after fixtures have run, so a fixture cannot remove it; the child's own pytest overwrites it anyway.
# tests/integration/reporting_helpers.py strips the same set for the same reason.
_OUTER_SESSION_VARIABLES = ("PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT", "PYTEST_XDIST_TESTRUNUID", "PYTEST_ADDOPTS")


@pytest.fixture
def mock_popen(mocker: MockerFixture) -> MagicMock:
    """Replace the child-process launch; the mock's process reports exit code 0 unless a test says otherwise."""
    mock: MagicMock = mocker.patch("cats.cli.testing.subprocess.Popen")
    mock.return_value.wait.return_value = 0
    return mock


@pytest.fixture
def nested_run_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """An environment for a real nested `cats test` run that this session's own pytest and xdist variables do not reach."""
    for name in _OUTER_SESSION_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def _write_passing_test(directory: Path) -> Path:
    passing_test = directory / "test_pass.py"
    passing_test.write_text("def test_always_passes():\n    assert True\n")
    return passing_test


@pytest.mark.usefixtures("nested_run_environment")
def test_cats_test_returns_nonzero_when_tests_fail(
    cats_yaml: Path,
    tmp_path: Path,
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # arrange
    monkeypatch.chdir(tmp_path)  # the nested run's report.json lands here, not in this repository
    write_config(cats_yaml, make_config())
    failing_test = tmp_path / "test_fail.py"
    failing_test.write_text("def test_always_fails():\n    assert False\n")

    # act
    result = cli_runner.invoke(cats_cli, ["test", str(failing_test)])

    # assert
    assert result.exit_code == pytest.ExitCode.TESTS_FAILED


@pytest.mark.usefixtures("nested_run_environment")
def test_cats_test_returns_zero_when_tests_pass(
    cats_yaml: Path,
    tmp_path: Path,
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # arrange
    monkeypatch.chdir(tmp_path)  # the nested run's report.json lands here, not in this repository
    write_config(cats_yaml, make_config())
    passing_test = _write_passing_test(tmp_path)

    # act
    result = cli_runner.invoke(cats_cli, ["test", str(passing_test)])

    # assert
    assert result.exit_code == pytest.ExitCode.OK


@pytest.mark.usefixtures("nested_run_environment")
def test_cats_test_does_not_trip_pytests_assertion_rewrite_warning(
    cats_yaml: Path,
    tmp_path: Path,
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An in-process `pytest.main()` found the `cats` package already imported by the CLI and warned
    `Module already imported so cannot be rewritten; cats` on every run; under `-W error` (common in CI)
    that was a hard failure. The child process imports nothing before pytest's hook is installed."""
    # arrange
    monkeypatch.chdir(tmp_path)
    write_config(cats_yaml, make_config())
    passing_test = _write_passing_test(tmp_path)

    # act
    result = cli_runner.invoke(cats_cli, ["test", "-W", "error::pytest.PytestAssertRewriteWarning", str(passing_test)])

    # assert
    assert result.exit_code == pytest.ExitCode.OK


@pytest.mark.usefixtures("nested_run_environment")
def test_cats_test_runs_the_installed_pytest_not_a_file_of_that_name_in_the_project(
    cats_yaml: Path,
    tmp_path: Path,
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`python -m pytest` prepends the working directory to sys.path, which the `pytest` console script does not:
    a project file named pytest.py would be run in place of pytest. `-P` keeps the child's path the script's."""
    # arrange
    monkeypatch.chdir(tmp_path)
    write_config(cats_yaml, make_config())
    passing_test = _write_passing_test(tmp_path)
    (tmp_path / "pytest.py").write_text('raise SystemExit("a project file shadowed pytest")\n')

    # act
    result = cli_runner.invoke(cats_cli, ["test", str(passing_test)])

    # assert
    assert result.exit_code == pytest.ExitCode.OK


def test_cats_test_runs_pytest_in_a_child_of_this_interpreter_with_its_plugin_forced(mock_popen: MagicMock, cli_runner: CliRunner) -> None:
    """-p cats is always first, so PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 in CI still yields fixtures and reports."""
    # act
    cli_runner.invoke(cats_cli, ["test", "tests/", "-k", "smoke"])

    # assert
    assert mock_popen.call_args == call([sys.executable, "-P", "-m", "pytest", "-p", "cats", "tests/", "-k", "smoke"])


def test_cats_test_defaults_to_verbose_with_summary(mock_popen: MagicMock, cli_runner: CliRunner) -> None:
    # act
    cli_runner.invoke(cats_cli, ["test"])

    # assert
    assert mock_popen.call_args == call([sys.executable, "-P", "-m", "pytest", "-p", "cats", "-vv", "-ra"])


def test_cats_test_exit_code_is_pytests(mock_popen: MagicMock, cli_runner: CliRunner) -> None:
    """The documented contract: pytest's code, unchanged (5 is 'no tests collected')."""
    # arrange
    mock_popen.return_value.wait.return_value = int(pytest.ExitCode.NO_TESTS_COLLECTED)

    # act
    result = cli_runner.invoke(cats_cli, ["test"])

    # assert
    assert result.exit_code == pytest.ExitCode.NO_TESTS_COLLECTED


def test_cats_test_lets_pytest_finish_its_own_interrupt_handling(mock_popen: MagicMock, cli_runner: CliRunner) -> None:
    """Ctrl+C reaches both processes: pytest prints its summary and runs finalizers, then exits 2; this process waits for that."""
    # arrange
    mock_popen.return_value.wait.side_effect = [KeyboardInterrupt(), int(pytest.ExitCode.INTERRUPTED)]

    # act
    result = cli_runner.invoke(cats_cli, ["test"])

    # assert
    assert (result.exit_code, mock_popen.return_value.wait.call_count) == (pytest.ExitCode.INTERRUPTED, 2)


def test_cats_test_reports_a_signal_killed_child_as_a_shell_would(mock_popen: MagicMock, cli_runner: CliRunner) -> None:
    """A child killed before pytest could exit has no pytest code; 128 + the signal number is the shell convention (SIGKILL is 9)."""
    # arrange
    mock_popen.return_value.wait.return_value = -9

    # act
    result = cli_runner.invoke(cats_cli, ["test"])

    # assert
    assert result.exit_code == 137


def test_cats_test_passes_a_termination_signal_to_the_child_unchanged(
    mock_popen: MagicMock,
    cli_runner: CliRunner,
    mocker: MockerFixture,
) -> None:
    """`kill <pid>` signals `cats` alone; passing the signal on keeps pytest from running on as an orphan, and passing it
    unchanged means the run ends exactly as `pytest` ends under that signal."""
    # arrange
    installed: dict[int, Callable[[int, FrameType | None], None]] = {}
    restored: list[tuple[int, object]] = []

    def record(signum: int, handler: object) -> str:
        if getattr(handler, "__name__", None) == "forward":
            installed[signum] = cast(Callable[[int, FrameType | None], None], handler)
        else:
            restored.append((signum, handler))
        return f"previous-{signum}"

    mocker.patch("cats.cli.testing.signal.signal", side_effect=record)

    def deliver_sigterm_while_waiting() -> int:
        installed[signal.SIGTERM](signal.SIGTERM, None)
        return -signal.SIGTERM  # the child died by the signal, as pytest does under SIGTERM

    mock_popen.return_value.wait.side_effect = deliver_sigterm_while_waiting

    # act
    result = cli_runner.invoke(cats_cli, ["test"])

    # assert - the child received SIGTERM itself, the death is reported as a shell would, every handler was put back
    expected_restored = [(signum, f"previous-{signum}") for signum in installed]
    assert (mock_popen.return_value.send_signal.call_args_list, result.exit_code, restored) == ([call(signal.SIGTERM)], 143, expected_restored)


def test_cats_test_installs_the_handlers_once_the_child_exists(mock_popen: MagicMock, cli_runner: CliRunner, mocker: MockerFixture) -> None:
    """The handler forwards to the child, so it is installed after Popen returns, by which time pytest is already running;
    a signal aimed at this process in those microseconds ends it and leaves pytest running, as `kill` on a shell leaves its child."""
    # arrange
    order: list[str] = []

    def record(signum: int, handler: object) -> None:
        if getattr(handler, "__name__", None) == "forward":
            order.append("install")

    mocker.patch("cats.cli.testing.signal.signal", side_effect=record)

    def start_child(*args: object, **kwargs: object) -> object:
        order.append("popen")
        return mock_popen.return_value

    mock_popen.side_effect = start_child

    # act
    result = cli_runner.invoke(cats_cli, ["test"])

    # assert
    expected_order = ["popen"] + ["install"] * len(_FORWARDED_SIGNALS)
    assert (order, result.exit_code) == (expected_order, 0)


def test_cats_test_leaves_a_handler_it_cannot_restore_alone(mock_popen: MagicMock, cli_runner: CliRunner, mocker: MockerFixture) -> None:
    """signal.signal reports None for a handler that was not installed from Python; passing None back would raise TypeError."""
    # arrange
    mock_signal = mocker.patch("cats.cli.testing.signal.signal", return_value=None)

    # act
    result = cli_runner.invoke(cats_cli, ["test"])

    # assert - one install per forwarded signal, no restore attempted
    assert (len(mock_signal.call_args_list), result.exit_code) == (len(_FORWARDED_SIGNALS), 0)


def test_cats_test_config_option_sets_the_config_path_for_workers(mock_popen: MagicMock, cli_runner: CliRunner, tmp_path: Path) -> None:
    """--config (long form only) pins cats.yaml through CATS_CONFIG so the child and its pytest-xdist workers read the same file."""
    # arrange
    config_file = tmp_path / "other.yaml"
    config_file.write_text("accounts_under_test: {}\n")

    # act
    cli_runner.invoke(cats_cli, ["test", "--config", str(config_file)])

    # assert
    assert os.environ["CATS_CONFIG"] == str(config_file.resolve())


def test_cats_test_leaves_dash_c_to_pytest(mock_popen: MagicMock, cli_runner: CliRunner) -> None:
    """pytest owns -c FILE; cats test must not claim it."""
    # act
    cli_runner.invoke(cats_cli, ["test", "-c", "pytest.ini"])

    # assert
    assert mock_popen.call_args == call([sys.executable, "-P", "-m", "pytest", "-p", "cats", "-c", "pytest.ini"])
