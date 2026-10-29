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

"""Tests for cats/cfn/ec2_server/probe.py _attempt_http - TCP-first classification, status capture, no redirects, no proxies, IPv6 literals."""

import errno
import http.client
import socket
import ssl
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from threading import Thread
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

import cats.cfn.ec2_server.probe as probe_module
from cats.cfn.ec2_server.probe import HTTP_TIMEOUT, NoHttpReply, ResolutionFailure, _attempt_http

TARGET_URL = "target.internal"
TARGET_PORT = 8080


@pytest.fixture
def mock_resolve(mocker: MockerFixture) -> MagicMock:
    """Patch the preflight _resolve; its own behavior is covered in test_probe_resolve.py."""
    mock: MagicMock = mocker.patch("cats.cfn.ec2_server.probe._resolve")
    return mock


@pytest.fixture
def mock_tcp(mocker: MockerFixture) -> MagicMock:
    """Patch the TCP-first connect; its own behavior is covered by the tcp tests."""
    mock: MagicMock = mocker.patch("cats.cfn.ec2_server.probe._attempt_tcp")
    return mock


@pytest.fixture
def mock_http_connection(mocker: MockerFixture, mock_tcp: MagicMock) -> MagicMock:
    """Patch http.client.HTTPConnection as the probe module sees it; returns the class mock."""
    mock: MagicMock = mocker.patch("http.client.HTTPConnection")
    mock.return_value.getresponse.return_value = MagicMock(status=200)
    return mock


@pytest.fixture
def mock_https_connection(mocker: MockerFixture, mock_tcp: MagicMock) -> MagicMock:
    mock: MagicMock = mocker.patch("http.client.HTTPSConnection")
    mock.return_value.getresponse.return_value = MagicMock(status=200)
    return mock


def test_resolution_failure_raises_before_any_connection_is_built(mock_resolve: MagicMock, mocker: MockerFixture) -> None:
    # arrange - resolution happens first (inside the TCP preflight), so a resolver failure never reaches http.client
    mock_connection = mocker.patch("http.client.HTTPConnection")
    mock_resolve.side_effect = ResolutionFailure("[Errno 16] Device or resource busy")

    # act & assert
    with pytest.raises(ResolutionFailure):
        _attempt_http(TARGET_URL, TARGET_PORT, "http")
    assert mock_connection.called is False


def test_tcp_refusal_raises_before_any_connection_is_built(mock_http_connection: MagicMock, mock_tcp: MagicMock) -> None:
    # arrange - SYN-level outcomes (REFUSED/TIMEOUT) classify from the TCP preflight
    mock_tcp.side_effect = ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")

    # act & assert
    with pytest.raises(ConnectionRefusedError):
        _attempt_http(TARGET_URL, TARGET_PORT, "http")
    assert mock_http_connection.called is False


@pytest.mark.parametrize("status", [200, 302, 403, 503])
def test_any_response_status_is_returned(mock_http_connection: MagicMock, status: int) -> None:
    # arrange - http.client never raises on a status, and redirects are not followed
    mock_http_connection.return_value.getresponse.return_value = MagicMock(status=status)

    # act
    result = _attempt_http(TARGET_URL, TARGET_PORT, "http")

    # assert
    assert result == status


def test_http_builds_a_plain_connection_with_the_http_timeout(mock_http_connection: MagicMock) -> None:
    # act
    _attempt_http(TARGET_URL, TARGET_PORT, "http")

    # assert
    mock_http_connection.assert_called_once_with(TARGET_URL, TARGET_PORT, timeout=HTTP_TIMEOUT)


def test_https_builds_a_tls_connection_with_the_http_timeout(mock_https_connection: MagicMock) -> None:
    # act
    _attempt_http(TARGET_URL, 443, "https")

    # assert
    mock_https_connection.assert_called_once_with(TARGET_URL, 443, timeout=HTTP_TIMEOUT)


def test_request_is_a_get_for_the_root_path(mock_http_connection: MagicMock) -> None:
    # act
    _attempt_http(TARGET_URL, TARGET_PORT, "http")

    # assert
    mock_http_connection.return_value.request.assert_called_once_with("GET", "/")


def test_connection_is_closed_after_a_response(mock_http_connection: MagicMock) -> None:
    # act
    _attempt_http(TARGET_URL, TARGET_PORT, "http")

    # assert
    mock_http_connection.return_value.close.assert_called_once_with()


def test_connection_is_closed_after_an_exception(mock_http_connection: MagicMock) -> None:
    # arrange
    mock_http_connection.return_value.getresponse.side_effect = ConnectionResetError(errno.ECONNRESET, "Connection reset by peer")

    # act
    with pytest.raises(ConnectionResetError):
        _attempt_http(TARGET_URL, TARGET_PORT, "http")

    # assert
    mock_http_connection.return_value.close.assert_called_once_with()


def test_post_connect_timeout_raises_no_http_reply(mock_http_connection: MagicMock) -> None:
    """TCP connected (preflight succeeded); HTTP silence must not masquerade as TIMEOUT."""
    # arrange - on 3.9 this arrives as socket.timeout, a plain OSError subclass; the except clause naming it is pinned by AST
    mock_http_connection.return_value.getresponse.side_effect = TimeoutError("timed out")

    # act & assert
    with pytest.raises(NoHttpReply, match=f"^connected; no HTTP reply in {HTTP_TIMEOUT}s: timed out$"):
        _attempt_http(TARGET_URL, TARGET_PORT, "http")


def test_second_connect_timeout_is_not_a_reply_failure(mock_http_connection: MagicMock) -> None:
    """http.client connects again inside the request; a device that admits one connection times this one out, and nothing connected."""
    # arrange
    original = TimeoutError("timed out")
    mock_http_connection.return_value.connect.side_effect = original

    # act & assert
    with pytest.raises(TimeoutError) as exc_info:
        _attempt_http(TARGET_URL, TARGET_PORT, "http")
    assert (exc_info.value is original, mock_http_connection.return_value.request.call_count) == (True, 0)


def test_tls_error_propagates_unwrapped(mock_https_connection: MagicMock) -> None:
    # arrange - http.client raises socket-level errors raw; classify must see them as-is
    original = ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number")
    mock_https_connection.return_value.request.side_effect = original

    # act & assert
    with pytest.raises(ssl.SSLError) as exc_info:
        _attempt_http(TARGET_URL, 443, "https")
    assert exc_info.value is original


# =============================================================================
# End to end against real local listeners
# =============================================================================


class _RedirectingHandler(BaseHTTPRequestHandler):
    """Answers every request with a 302 back to itself and counts the hits."""

    hits = 0

    def do_GET(self) -> None:
        type(self).hits += 1
        self.send_response(302)
        self.send_header("Location", "/followed")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def redirecting_server() -> Generator[int, None, None]:
    _RedirectingHandler.hits = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectingHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def test_real_302_is_reported_on_the_first_hop_and_not_followed(redirecting_server: int) -> None:
    # act
    result = _attempt_http("127.0.0.1", redirecting_server, "http")

    # assert
    assert (result, _RedirectingHandler.hits) == (302, 1)


class _IPv6HTTPServer(HTTPServer):
    address_family = socket.AF_INET6


@pytest.fixture
def redirecting_ipv6_server() -> Generator[int, None, None]:
    _RedirectingHandler.hits = 0
    try:
        server = _IPv6HTTPServer(("::1", 0), _RedirectingHandler)
    except OSError as e:
        pytest.skip(f"IPv6 loopback is not available here: {e}")
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def test_ipv6_literal_host_is_probed_over_http(redirecting_ipv6_server: int) -> None:
    """An unbracketed IPv6 literal reaches the listener; http.client brackets it in the Host header."""
    # act
    result = probe_module.probe("http", "::1", redirecting_ipv6_server)

    # assert
    expected = {"result": "SUCCESS", "reason": None, "http_status": 302}
    assert result == expected


@pytest.fixture
def hanging_server() -> Generator[int, None, None]:
    """Accepts TCP connections and never sends a byte - the slow/hung backend case."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(5)
    yield server.getsockname()[1]
    server.close()


@pytest.fixture
def fast_http_timeout(monkeypatch: pytest.MonkeyPatch) -> float:
    monkeypatch.setattr(probe_module, "HTTP_TIMEOUT", 0.3)
    return 0.3


def test_accept_and_hang_http_classifies_failure_not_timeout(hanging_server: int, fast_http_timeout: float) -> None:
    """The denial false-pass: a hung-but-open backend must never satisfy `result == TIMEOUT`."""
    # act
    result = probe_module.probe("http", "127.0.0.1", hanging_server)

    # assert
    expected = {"result": "FAILURE", "reason": "connected; no HTTP reply in 0.3s: timed out", "http_status": None}
    assert result == expected


def test_accept_and_hang_https_classifies_failure(hanging_server: int, fast_http_timeout: float) -> None:
    """The TLS-handshake-timeout variant of the same silence."""
    # act
    result = probe_module.probe("https", "127.0.0.1", hanging_server)

    # assert
    expected = ("FAILURE", None)
    assert (result["result"], result["http_status"]) == expected


def test_http_probe_ignores_proxy_environment(redirecting_server: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """http_proxy in the handler's environment (e.g. via the SSM agent unit) must not
    turn a direct probe into a probe of the proxy."""
    # arrange - a dead proxy address; honoring it would fail the call
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:1")

    # act
    result = probe_module.probe("http", "127.0.0.1", redirecting_server)

    # assert
    expected = {"result": "SUCCESS", "reason": None, "http_status": 302}
    assert result == expected


def test_closed_port_classifies_refused_from_the_tcp_preflight() -> None:
    # arrange - bind then close, so the port is known-closed
    placeholder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    placeholder.bind(("127.0.0.1", 0))
    closed_port = placeholder.getsockname()[1]
    placeholder.close()

    # act
    result = probe_module.probe("http", "127.0.0.1", closed_port)

    # assert
    assert result["result"] == "REFUSED"


@pytest.fixture
def non_http_server() -> Generator[int, None, None]:
    """Accepts, reads the request, and answers with a banner that is not HTTP (an SSH daemon, say)."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(5)

    def answer() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                conn.recv(1024)
                conn.sendall(b"SSH-2.0-OpenSSH_9.6\r\n")

    Thread(target=answer, daemon=True).start()
    yield server.getsockname()[1]
    server.close()


def test_non_http_reply_classifies_failure_with_the_reply_as_evidence(non_http_server: int) -> None:
    """The path is open and something answered, but not HTTP: FAILURE with the raw line, never ERROR."""
    # act
    result = probe_module.probe("http", "127.0.0.1", non_http_server)

    # assert
    expected = {"result": "FAILURE", "reason": "SSH-2.0-OpenSSH_9.6\r\n", "http_status": None}
    assert result == expected


@pytest.fixture
def accept_and_close_server() -> Generator[int, None, None]:
    """Accepts and closes immediately without sending a byte."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(5)

    def closer() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            conn.close()

    Thread(target=closer, daemon=True).start()
    yield server.getsockname()[1]
    server.close()


def test_accept_then_close_classifies_refused(accept_and_close_server: int) -> None:
    """Accept-then-close stays REFUSED: RemoteDisconnected is a ConnectionResetError (see the probe README)."""
    # act
    result = probe_module.probe("http", "127.0.0.1", accept_and_close_server)

    # assert
    assert (result["result"], result["http_status"]) == ("REFUSED", None)


def test_remote_disconnected_is_a_connection_reset() -> None:
    """Pins the MRO fact the accept-then-close classification relies on."""
    assert issubclass(http.client.RemoteDisconnected, ConnectionResetError) is True
