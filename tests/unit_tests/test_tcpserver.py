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

"""Tests for cats/cfn/ec2_server/tcpserver.py - empty 200 response, no banner, and one caller's
exit never taking the listener down.

The peer-gone tests are regression tests: a TLS client rejecting the plaintext reply closes
with unread bytes, so its kernel answers with RST, and if that lands before the listener's
shutdown() the call raised ENOTCONN and the unhandled OSError killed the process. Every
later probe of the port then saw REFUSED.
"""

import selectors
import types
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from cats.cfn.ec2_server.tcpserver import parse_ports, service_connection

EXPECTED_RESPONSE = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"


def make_key(sock: MagicMock, outb: bytes) -> selectors.SelectorKey:
    """Build a SelectorKey as the selector loop would hand to service_connection."""
    data = types.SimpleNamespace(addr=("10.0.1.20", 41234), inb=b"", outb=outb)
    return selectors.SelectorKey(fileobj=sock, fd=7, events=selectors.EVENT_READ | selectors.EVENT_WRITE, data=data)


@pytest.fixture
def mock_sel(mocker: MockerFixture) -> MagicMock:
    mock: MagicMock = mocker.patch("cats.cfn.ec2_server.tcpserver.sel")
    return mock


def test_received_data_answered_with_empty_200(mock_sel: MagicMock) -> None:
    # arrange
    sock = MagicMock()
    sock.send.return_value = len(EXPECTED_RESPONSE)
    key = make_key(sock, outb=b"GET / HTTP/1.1\r\n\r\n")

    # act
    service_connection(key, selectors.EVENT_WRITE)

    # assert
    sock.send.assert_called_once_with(EXPECTED_RESPONSE)


def test_connect_only_probe_gets_no_response(mock_sel: MagicMock) -> None:
    # arrange - a connection that sent no bytes has an empty outb buffer
    sock = MagicMock()
    key = make_key(sock, outb=b"")

    # act
    service_connection(key, selectors.EVENT_WRITE)

    # assert
    sock.send.assert_not_called()


def test_peer_close_on_read_closes_connection(mock_sel: MagicMock) -> None:
    # arrange - recv returning no bytes is the peer's FIN
    sock = MagicMock()
    sock.recv.return_value = b""
    key = make_key(sock, outb=b"")

    # act
    service_connection(key, selectors.EVENT_READ)

    # assert
    sock.close.assert_called_once_with()


def test_peer_close_on_read_sends_nothing(mock_sel: MagicMock) -> None:
    # arrange - FIN arrives with bytes still buffered from an earlier read; the socket is gone, so no reply
    sock = MagicMock()
    sock.recv.return_value = b""
    key = make_key(sock, outb=b"GET / HTTP/1.1\r\n\r\n")

    # act
    service_connection(key, selectors.EVENT_READ | selectors.EVENT_WRITE)

    # assert
    sock.send.assert_not_called()


def test_peer_reset_during_shutdown_closes_connection(mock_sel: MagicMock) -> None:
    # arrange - the TLS-client case: RST processed before shutdown(), which then fails with ENOTCONN
    sock = MagicMock()
    sock.send.return_value = len(EXPECTED_RESPONSE)
    sock.shutdown.side_effect = OSError(107, "Transport endpoint is not connected")
    key = make_key(sock, outb=b"\x16\x03\x01\x02\x00\x01\x00\x01\xfc\x03\x03")

    # act
    service_connection(key, selectors.EVENT_WRITE)

    # assert
    sock.close.assert_called_once_with()


def test_peer_reset_during_shutdown_unregisters_once(mock_sel: MagicMock) -> None:
    # arrange
    sock = MagicMock()
    sock.send.return_value = len(EXPECTED_RESPONSE)
    sock.shutdown.side_effect = OSError(107, "Transport endpoint is not connected")
    key = make_key(sock, outb=b"\x16\x03\x01\x02\x00\x01\x00\x01\xfc\x03\x03")

    # act
    service_connection(key, selectors.EVENT_WRITE)

    # assert
    mock_sel.unregister.assert_called_once_with(sock)


def test_peer_gone_before_send_closes_connection(mock_sel: MagicMock) -> None:
    # arrange - the peer reset between our recv and our send
    sock = MagicMock()
    sock.send.side_effect = BrokenPipeError(32, "Broken pipe")
    key = make_key(sock, outb=b"GET / HTTP/1.1\r\n\r\n")

    # act
    service_connection(key, selectors.EVENT_WRITE)

    # assert
    sock.close.assert_called_once_with()


def test_peer_reset_during_recv_closes_connection(mock_sel: MagicMock) -> None:
    # arrange - a caller that sent bytes and aborted before we read them
    sock = MagicMock()
    sock.recv.side_effect = ConnectionResetError(104, "Connection reset by peer")
    key = make_key(sock, outb=b"")

    # act
    service_connection(key, selectors.EVENT_READ)

    # assert
    sock.close.assert_called_once_with()


# =============================================================================
# parse_ports: the argument the systemd unit passes from CATS_TCP_PORTS
# =============================================================================


def test_parse_ports_splits_a_comma_separated_list() -> None:
    # act
    result = parse_ports("8080,443,8443")

    # assert
    assert result == [8080, 443, 8443]


def test_parse_ports_accepts_a_single_port() -> None:
    # act
    result = parse_ports("8080")

    # assert
    assert result == [8080]


def test_parse_ports_rejects_a_space_separated_list() -> None:
    """systemd would have split a space-separated value into several arguments before it reached us; one arriving whole is not a port list."""
    # act & assert
    with pytest.raises(ValueError, match=r"invalid literal for int\(\) with base 10: '8080 443'"):
        parse_ports("8080 443")
