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

"""Tests for cats/cfn/ec2_server/probe.py main and __main__ - the SSM entrypoint.

SSM supplies every document parameter as a string in an SSM_-prefixed environment
variable and runs the probe as `python3 -` with the source on stdin. These tests
cover the normalization in main() and run the real file that way.
"""

import errno
import json
import socket
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

import cats.cfn.ec2_server.probe as probe_module
from cats.cfn.ec2_server.probe import main

TARGET_URL = "target.internal"
PROBE_PATH = Path(probe_module.__file__)


@pytest.fixture
def mock_probe(mocker: MockerFixture) -> MagicMock:
    mock: MagicMock = mocker.patch("cats.cfn.ec2_server.probe.probe")
    mock.return_value = {"result": "SUCCESS", "reason": None, "http_status": None}
    return mock


def _env(protocol: str = "tcp", url: str = TARGET_URL, port: str = "8080", message: str = "") -> dict[str, str]:
    return {"SSM_Protocol": protocol, "SSM_TargetUrl": url, "SSM_TargetPort": port, "SSM_TargetMessage": message}


# =============================================================================
# main() - normalization of SSM string parameters
# =============================================================================


def test_main_converts_port_string_to_int(mock_probe: MagicMock) -> None:
    # act
    main(_env(port="8080"))

    # assert
    mock_probe.assert_called_once_with("tcp", TARGET_URL, 8080, None)


def test_main_passes_empty_message_as_none(mock_probe: MagicMock) -> None:
    # arrange - the document's TargetMessage default is the empty string

    # act
    main(_env(message=""))

    # assert
    mock_probe.assert_called_once_with("tcp", TARGET_URL, 8080, None)


def test_main_passes_non_empty_message_through(mock_probe: MagicMock) -> None:
    # act
    main(_env(protocol="udp", port="5000", message="tok:3f2a"))

    # assert
    mock_probe.assert_called_once_with("udp", TARGET_URL, 5000, "tok:3f2a")


def test_main_returns_probe_result(mock_probe: MagicMock) -> None:
    # arrange
    expected = {"result": "SUCCESS", "reason": None, "http_status": None}

    # act
    result = main(_env())

    # assert
    assert result == expected


def test_main_non_integer_port_is_error(mock_probe: MagicMock, capsys: pytest.CaptureFixture[str]) -> None:
    # arrange
    expected = {"result": "ERROR", "reason": "invalid literal for int() with base 10: 'eighty'", "http_status": None}

    # act
    result = main(_env(port="eighty"))

    # assert
    assert result == expected
    mock_probe.assert_not_called()
    capsys.readouterr()


def test_main_missing_variable_is_error(mock_probe: MagicMock, capsys: pytest.CaptureFixture[str]) -> None:
    # arrange
    env = _env()
    del env["SSM_TargetUrl"]
    expected = {"result": "ERROR", "reason": "'SSM_TargetUrl'", "http_status": None}

    # act
    result = main(env)

    # assert
    assert result == expected
    mock_probe.assert_not_called()
    capsys.readouterr()


# =============================================================================
# __main__ - the real file, run exactly as the SSM document runs it
# =============================================================================


def _run_as_ssm(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Feed probe.py to `python -` on stdin with only the SSM_* variables set, as the heredoc does."""
    return subprocess.run(
        [sys.executable, "-"],
        input=PROBE_PATH.read_text(),
        env={**env, "PATH": ""},
        capture_output=True,
        text=True,
        check=False,
    )


def _closed_local_port() -> int:
    """A port on loopback that nothing is listening on right now."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_dunder_main_prints_one_json_line_for_a_refused_tcp_probe() -> None:
    # arrange
    port = _closed_local_port()
    expected = {"result": "REFUSED", "reason": f"[Errno {errno.ECONNREFUSED}] Connection refused", "http_status": None}

    # act
    completed = _run_as_ssm(_env(protocol="tcp", url="127.0.0.1", port=str(port)))

    # assert
    assert completed.returncode == 0
    assert completed.stdout.count("\n") == 1
    assert json.loads(completed.stdout) == expected


def test_dunder_main_non_integer_port_prints_error_line_not_traceback_on_stdout() -> None:
    # arrange
    expected = {"result": "ERROR", "reason": "invalid literal for int() with base 10: 'eighty'", "http_status": None}

    # act
    completed = _run_as_ssm(_env(port="eighty"))

    # assert - exit 0 with a JSON ERROR line on stdout; the traceback is on stderr for the SSM log
    assert completed.returncode == 0
    assert json.loads(completed.stdout) == expected
    assert "ValueError" in completed.stderr


def test_dunder_main_missing_variable_prints_error_line() -> None:
    # arrange
    env = _env()
    del env["SSM_Protocol"]
    expected = {"result": "ERROR", "reason": "'SSM_Protocol'", "http_status": None}

    # act
    completed = _run_as_ssm(env)

    # assert
    assert completed.returncode == 0
    assert json.loads(completed.stdout) == expected
