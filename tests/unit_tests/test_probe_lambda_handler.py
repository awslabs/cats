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

"""Tests for cats/cfn/ec2_server/probe.py lambda_handler - event parsing, payload is probe() with no envelope."""

from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from cats.cfn.ec2_server.probe import lambda_handler

TARGET_URL = "target.internal"
MESSAGE = "0123456789abcdef0123456789abcdef:3f2a"


@pytest.fixture
def mock_probe(mocker: MockerFixture) -> MagicMock:
    mock: MagicMock = mocker.patch("cats.cfn.ec2_server.probe.probe")
    mock.return_value = {"result": "SUCCESS", "reason": None, "http_status": 200}
    return mock


@pytest.mark.parametrize("protocol", ["http", "https", "tcp"])
def test_handler_passes_protocol_url_port_and_no_message(mock_probe: MagicMock, protocol: str) -> None:
    # arrange
    event = {"PROTOCOL": protocol, "TARGET_URL": TARGET_URL, "TARGET_PORT": 8080}

    # act
    lambda_handler(event, None)

    # assert
    mock_probe.assert_called_once_with(protocol, TARGET_URL, 8080, None)


def test_handler_passes_message_for_udp(mock_probe: MagicMock) -> None:
    # arrange
    event = {"PROTOCOL": "udp", "TARGET_URL": TARGET_URL, "TARGET_PORT": 5000, "TARGET_MESSAGE": MESSAGE}

    # act
    lambda_handler(event, None)

    # assert
    mock_probe.assert_called_once_with("udp", TARGET_URL, 5000, MESSAGE)


def test_handler_returns_probe_dict_directly_with_no_envelope(mock_probe: MagicMock) -> None:
    # arrange
    event = {"PROTOCOL": "http", "TARGET_URL": TARGET_URL, "TARGET_PORT": 8080}
    expected = {"result": "SUCCESS", "reason": None, "http_status": 200}

    # act
    result = lambda_handler(event, None)

    # assert
    assert result == expected


def test_handler_preserves_native_port_type(mock_probe: MagicMock) -> None:
    # arrange - the Lambda event carries native JSON types; no string conversion happens here
    event = {"PROTOCOL": "tcp", "TARGET_URL": TARGET_URL, "TARGET_PORT": "8080"}

    # act
    lambda_handler(event, None)

    # assert - probe() itself rejects the string as ERROR
    mock_probe.assert_called_once_with("tcp", TARGET_URL, "8080", None)


def test_handler_end_to_end_error_for_bad_protocol(capsys: pytest.CaptureFixture[str]) -> None:
    # arrange - unpatched probe: an unknown protocol is rejected as ERROR, never treated as a probe
    event = {"PROTOCOL": "ftp", "TARGET_URL": TARGET_URL, "TARGET_PORT": 21}
    expected = {"result": "ERROR", "reason": "Unsupported protocol: 'ftp'", "http_status": None}

    # act
    result = lambda_handler(event, None)

    # assert
    assert result == expected
    capsys.readouterr()
