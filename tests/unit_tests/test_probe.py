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

"""Tests for cats/cfn/ec2_server/probe.py probe - the three-key wire contract per outcome per protocol."""

import errno
import socket
import ssl
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from cats.cfn.ec2_server.probe import TIMEOUT_SECONDS, probe

TARGET_URL = "target.internal"
TARGET_PORT = 8080
MESSAGE = "0123456789abcdef0123456789abcdef:3f2a"


@pytest.fixture
def mock_attempt_http(mocker: MockerFixture) -> MagicMock:
    mock: MagicMock = mocker.patch("cats.cfn.ec2_server.probe._attempt_http")
    return mock


@pytest.fixture
def mock_getaddrinfo(mocker: MockerFixture) -> MagicMock:
    mock: MagicMock = mocker.patch("socket.getaddrinfo")
    mock.return_value = [(socket.AF_INET, socket.SOCK_DGRAM, 17, "", ("10.0.1.5", TARGET_PORT))]
    return mock


@pytest.fixture
def mock_getaddrinfo_stream(mock_getaddrinfo: MagicMock) -> MagicMock:
    mock_getaddrinfo.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.1.5", TARGET_PORT))]
    return mock_getaddrinfo


@pytest.fixture
def mock_socket(mocker: MockerFixture) -> MagicMock:
    mock: MagicMock = mocker.patch("socket.socket")
    return mock


@pytest.fixture
def mock_connect(mock_getaddrinfo_stream: MagicMock, mock_socket: MagicMock) -> MagicMock:
    """The connect() of the TCP attempt's socket, with resolution succeeding before it."""
    connect: MagicMock = mock_socket.return_value.__enter__.return_value.connect
    return connect


# =============================================================================
# HTTP / HTTPS
# =============================================================================


def test_http_200_is_success_with_status(mock_attempt_http: MagicMock) -> None:
    # arrange
    mock_attempt_http.return_value = 200
    expected = {"result": "SUCCESS", "reason": None, "http_status": 200}

    # act
    result = probe("http", TARGET_URL, TARGET_PORT)

    # assert
    assert result == expected


def test_http_503_is_success_with_status(mock_attempt_http: MagicMock) -> None:
    # arrange
    mock_attempt_http.return_value = 503
    expected = {"result": "SUCCESS", "reason": None, "http_status": 503}

    # act
    result = probe("http", TARGET_URL, TARGET_PORT)

    # assert
    assert result == expected


def test_https_passes_scheme_to_attempt(mock_attempt_http: MagicMock) -> None:
    # arrange
    mock_attempt_http.return_value = 200

    # act
    probe("https", TARGET_URL, 443)

    # assert
    mock_attempt_http.assert_called_once_with(TARGET_URL, 443, "https")


def test_https_ssl_error_is_tls_failure_without_status(mock_attempt_http: MagicMock) -> None:
    # arrange
    mock_attempt_http.side_effect = ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number")
    expected = {"result": "TLS_FAILURE", "reason": "[SSL: WRONG_VERSION_NUMBER] wrong version number", "http_status": None}

    # act
    result = probe("https", TARGET_URL, 443)

    # assert
    assert result == expected


def test_http_refused_carries_reason(mock_attempt_http: MagicMock) -> None:
    # arrange
    mock_attempt_http.side_effect = ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")
    expected = {"result": "REFUSED", "reason": f"[Errno {errno.ECONNREFUSED}] Connection refused", "http_status": None}

    # act
    result = probe("http", TARGET_URL, TARGET_PORT)

    # assert
    assert result == expected


def test_http_dns_failure(mock_attempt_http: MagicMock) -> None:
    # arrange
    mock_attempt_http.side_effect = socket.gaierror(socket.EAI_NONAME, "Name or service not known")
    expected = {"result": "DNS_FAILURE", "reason": f"[Errno {socket.EAI_NONAME}] Name or service not known", "http_status": None}

    # act
    result = probe("http", TARGET_URL, TARGET_PORT)

    # assert
    assert result == expected


# =============================================================================
# TCP
# =============================================================================


def test_tcp_connect_is_success_without_status(mock_connect: MagicMock) -> None:
    # arrange
    expected = {"result": "SUCCESS", "reason": None, "http_status": None}

    # act
    result = probe("tcp", TARGET_URL, TARGET_PORT)

    # assert
    assert result == expected


def test_tcp_connects_to_the_resolved_address_with_probe_timeout(mock_connect: MagicMock, mock_socket: MagicMock) -> None:
    # act
    probe("tcp", TARGET_URL, TARGET_PORT)

    # assert - the socket is built from the resolved family/type/proto, timed, and connected once
    mock_socket.assert_called_once_with(socket.AF_INET, socket.SOCK_STREAM, 6)
    mock_socket.return_value.__enter__.return_value.settimeout.assert_called_once_with(TIMEOUT_SECONDS)
    mock_connect.assert_called_once_with(("10.0.1.5", TARGET_PORT))


def test_tcp_refused(mock_connect: MagicMock) -> None:
    # arrange
    mock_connect.side_effect = ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")
    expected = {"result": "REFUSED", "reason": f"[Errno {errno.ECONNREFUSED}] Connection refused", "http_status": None}

    # act
    result = probe("tcp", TARGET_URL, TARGET_PORT)

    # assert
    assert result == expected


def test_tcp_timeout(mock_connect: MagicMock) -> None:
    # arrange
    mock_connect.side_effect = TimeoutError("timed out")
    expected = {"result": "TIMEOUT", "reason": "timed out", "http_status": None}

    # act
    result = probe("tcp", TARGET_URL, TARGET_PORT)

    # assert
    assert result == expected


def test_tcp_dns_failure(mock_getaddrinfo: MagicMock, mock_socket: MagicMock) -> None:
    # arrange
    mock_getaddrinfo.side_effect = socket.gaierror(socket.EAI_NONAME, "Name or service not known")
    expected = {"result": "DNS_FAILURE", "reason": f"[Errno {socket.EAI_NONAME}] Name or service not known", "http_status": None}

    # act
    result = probe("tcp", TARGET_URL, TARGET_PORT)

    # assert - resolution failed, so no socket was ever opened
    assert result == expected
    mock_socket.assert_not_called()


def test_tcp_resolver_ebusy_oserror_is_dns_failure(mock_getaddrinfo: MagicMock, mock_socket: MagicMock) -> None:
    # arrange - Lambda's resolver reports NXDOMAIN and throttling as EAI_SYSTEM, which CPython
    # raises as a bare OSError(EBUSY); inside the resolve step that is a DNS claim
    mock_getaddrinfo.side_effect = OSError(errno.EBUSY, "Device or resource busy")
    expected = {"result": "DNS_FAILURE", "reason": f"[Errno {errno.EBUSY}] Device or resource busy", "http_status": None}

    # act
    result = probe("tcp", TARGET_URL, TARGET_PORT)

    # assert
    assert result == expected
    mock_socket.assert_not_called()


def test_tcp_connect_ebusy_oserror_is_failure(mock_connect: MagicMock) -> None:
    # arrange - the same errno outside the resolve step proves nothing, so it stays unclassified
    mock_connect.side_effect = OSError(errno.EBUSY, "Device or resource busy")
    expected = {"result": "FAILURE", "reason": f"[Errno {errno.EBUSY}] Device or resource busy", "http_status": None}

    # act
    result = probe("tcp", TARGET_URL, TARGET_PORT)

    # assert
    assert result == expected


def test_tcp_network_unreachable_is_failure(mock_connect: MagicMock) -> None:
    # arrange
    mock_connect.side_effect = OSError(errno.ENETUNREACH, "Network is unreachable")
    expected = {"result": "FAILURE", "reason": f"[Errno {errno.ENETUNREACH}] Network is unreachable", "http_status": None}

    # act
    result = probe("tcp", TARGET_URL, TARGET_PORT)

    # assert
    assert result == expected


# =============================================================================
# TCP against a name with several addresses: every address is tried in resolver order
# =============================================================================

DUAL_STACK = [
    (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fd00::5", TARGET_PORT, 0, 0)),
    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.1.5", TARGET_PORT)),
]


@pytest.fixture
def mock_connect_dual_stack(mock_getaddrinfo: MagicMock, mock_socket: MagicMock) -> MagicMock:
    mock_getaddrinfo.return_value = DUAL_STACK
    connect: MagicMock = mock_socket.return_value.__enter__.return_value.connect
    return connect


def test_tcp_first_address_refused_second_connects_is_success(mock_connect_dual_stack: MagicMock, mock_socket: MagicMock) -> None:
    # arrange - IPv6 is answered with RST, IPv4 accepts: the host is reachable
    mock_connect_dual_stack.side_effect = [ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"), None]
    expected = {"result": "SUCCESS", "reason": None, "http_status": None}

    # act
    result = probe("tcp", TARGET_URL, TARGET_PORT)

    # assert - one socket per address family, connected in resolver order
    assert result == expected
    assert [c.args for c in mock_socket.call_args_list] == [(socket.AF_INET6, socket.SOCK_STREAM, 6), (socket.AF_INET, socket.SOCK_STREAM, 6)]
    assert [c.args for c in mock_connect_dual_stack.call_args_list] == [(("fd00::5", TARGET_PORT, 0, 0),), (("10.0.1.5", TARGET_PORT),)]


def test_tcp_first_address_connects_stops_there(mock_connect_dual_stack: MagicMock) -> None:
    # act
    probe("tcp", TARGET_URL, TARGET_PORT)

    # assert
    mock_connect_dual_stack.assert_called_once_with(("fd00::5", TARGET_PORT, 0, 0))


def test_tcp_every_address_fails_reports_the_last_error(mock_connect_dual_stack: MagicMock) -> None:
    # arrange - IPv6 dropped, IPv4 refused: the last address decides, as urllib does for HTTP
    mock_connect_dual_stack.side_effect = [TimeoutError("timed out"), ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")]
    expected = {"result": "REFUSED", "reason": f"[Errno {errno.ECONNREFUSED}] Connection refused", "http_status": None}

    # act
    result = probe("tcp", TARGET_URL, TARGET_PORT)

    # assert
    assert result == expected


def test_tcp_every_address_times_out_is_timeout(mock_connect_dual_stack: MagicMock) -> None:
    # arrange
    mock_connect_dual_stack.side_effect = [TimeoutError("timed out"), TimeoutError("timed out")]
    expected = {"result": "TIMEOUT", "reason": "timed out", "http_status": None}

    # act
    result = probe("tcp", TARGET_URL, TARGET_PORT)

    # assert
    assert result == expected


# =============================================================================
# UDP
# =============================================================================


def test_udp_sent_with_no_reason(mock_getaddrinfo: MagicMock, mock_socket: MagicMock) -> None:
    # arrange
    expected = {"result": "SENT", "reason": None, "http_status": None}

    # act
    result = probe("udp", TARGET_URL, TARGET_PORT, MESSAGE)

    # assert
    assert result == expected


def test_udp_sends_exact_message_bytes_to_resolved_address(mock_getaddrinfo: MagicMock, mock_socket: MagicMock) -> None:
    # act
    probe("udp", TARGET_URL, TARGET_PORT, MESSAGE)

    # assert
    mock_socket.return_value.__enter__.return_value.sendto.assert_called_once_with(MESSAGE.encode(), ("10.0.1.5", TARGET_PORT))


def test_udp_resolves_for_datagram_sockets_and_uses_the_result_family(mock_getaddrinfo: MagicMock, mock_socket: MagicMock) -> None:
    # arrange
    mock_getaddrinfo.return_value = [(socket.AF_INET6, socket.SOCK_DGRAM, 17, "", ("fd00::5", TARGET_PORT, 0, 0))]

    # act
    probe("udp", TARGET_URL, TARGET_PORT, MESSAGE)

    # assert
    mock_getaddrinfo.assert_called_once_with(TARGET_URL, TARGET_PORT, type=socket.SOCK_DGRAM)
    mock_socket.assert_called_once_with(socket.AF_INET6, socket.SOCK_DGRAM)


def test_udp_dns_failure(mock_getaddrinfo: MagicMock, mock_socket: MagicMock) -> None:
    # arrange
    mock_getaddrinfo.side_effect = socket.gaierror(socket.EAI_NONAME, "Name or service not known")
    expected = {"result": "DNS_FAILURE", "reason": f"[Errno {socket.EAI_NONAME}] Name or service not known", "http_status": None}

    # act
    result = probe("udp", TARGET_URL, TARGET_PORT, MESSAGE)

    # assert
    assert result == expected
    mock_socket.assert_not_called()


def test_udp_sendto_oserror_is_failure(mock_getaddrinfo: MagicMock, mock_socket: MagicMock) -> None:
    # arrange
    mock_socket.return_value.__enter__.return_value.sendto.side_effect = OSError(errno.ENETUNREACH, "Network is unreachable")
    expected = {"result": "FAILURE", "reason": f"[Errno {errno.ENETUNREACH}] Network is unreachable", "http_status": None}

    # act
    result = probe("udp", TARGET_URL, TARGET_PORT, MESSAGE)

    # assert
    assert result == expected


# =============================================================================
# ERROR - infrastructure fault or caller bug, never a network outcome
# =============================================================================


def test_unexpected_exception_is_error_with_message_as_reason(mock_connect: MagicMock, capsys: pytest.CaptureFixture[str]) -> None:
    # arrange
    mock_connect.side_effect = RuntimeError("boom")
    expected = {"result": "ERROR", "reason": "boom", "http_status": None}

    # act
    result = probe("tcp", TARGET_URL, TARGET_PORT)

    # assert - traceback goes to stderr so stdout stays exactly one JSON document
    assert result == expected
    assert "RuntimeError: boom" in capsys.readouterr().err


def test_classified_outcome_prints_no_traceback(mock_connect: MagicMock, capsys: pytest.CaptureFixture[str]) -> None:
    # arrange
    mock_connect.side_effect = TimeoutError("timed out")

    # act
    probe("tcp", TARGET_URL, TARGET_PORT)

    # assert
    assert capsys.readouterr().err == ""


def test_unknown_protocol_is_error_and_nothing_is_attempted(
    mock_attempt_http: MagicMock, mock_getaddrinfo: MagicMock, mock_socket: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    # arrange - a malformed direct SSM/Lambda invocation must not silently become a UDP probe
    expected = {"result": "ERROR", "reason": "Unsupported protocol: 'ftp'", "http_status": None}

    # act
    result = probe("ftp", TARGET_URL, 21, "hello")

    # assert
    assert result == expected
    assert (mock_attempt_http.called, mock_getaddrinfo.called, mock_socket.called) == (False, False, False)
    capsys.readouterr()
