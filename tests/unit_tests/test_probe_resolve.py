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

"""Tests for cats/cfn/ec2_server/probe.py _resolve - the one place a ResolutionFailure can arise.

DNS_FAILURE is claimed two ways, both indisputable: by type (a gaierror carrying a resolver
code, wherever it arises) or by position (any other failure inside this resolve step). These
tests pin the positional half.
"""

import errno
import socket
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from cats.cfn.ec2_server.probe import ResolutionFailure, _resolve

TARGET_URL = "target.internal"
TARGET_PORT = 8080
# EAI_ADDRFAMILY is not defined on every platform; any non-DNS gaierror code will do.
EAI_ADDRFAMILY = getattr(socket, "EAI_ADDRFAMILY", 9999)


@pytest.fixture
def mock_getaddrinfo(mocker: MockerFixture) -> MagicMock:
    mock: MagicMock = mocker.patch("socket.getaddrinfo")
    mock.return_value = [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fd00::5", TARGET_PORT, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.1.5", TARGET_PORT)),
    ]
    return mock


def test_returns_every_addrinfo_result_in_resolver_order(mock_getaddrinfo: MagicMock) -> None:
    # act
    result = _resolve(TARGET_URL, TARGET_PORT, socket.SOCK_STREAM)

    # assert - the TCP attempt tries them in this order; the UDP attempt takes the first
    expected = [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fd00::5", TARGET_PORT, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.1.5", TARGET_PORT)),
    ]
    assert result == expected


def test_passes_url_port_and_socktype_to_getaddrinfo(mock_getaddrinfo: MagicMock) -> None:
    # act
    _resolve(TARGET_URL, TARGET_PORT, socket.SOCK_DGRAM)

    # assert
    mock_getaddrinfo.assert_called_once_with(TARGET_URL, TARGET_PORT, type=socket.SOCK_DGRAM)


def test_resolver_gaierror_reraises_the_gaierror(mock_getaddrinfo: MagicMock) -> None:
    # arrange - classify decides gaierrors by errno wherever they arise; _resolve does not wrap them
    original = socket.gaierror(socket.EAI_NONAME, "Name or service not known")
    mock_getaddrinfo.side_effect = original

    # act & assert
    with pytest.raises(socket.gaierror) as exc_info:
        _resolve(TARGET_URL, TARGET_PORT, socket.SOCK_STREAM)
    assert exc_info.value is original


def test_non_resolver_gaierror_reraises_the_gaierror(mock_getaddrinfo: MagicMock) -> None:
    # arrange - the resolver answered; the local stack could not use the address family
    original = socket.gaierror(EAI_ADDRFAMILY, "Address family for hostname not supported")
    mock_getaddrinfo.side_effect = original

    # act & assert
    with pytest.raises(socket.gaierror) as exc_info:
        _resolve(TARGET_URL, TARGET_PORT, socket.SOCK_STREAM)
    assert exc_info.value is original


def test_bare_oserror_raises_resolution_failure_preserving_the_message(mock_getaddrinfo: MagicMock) -> None:
    # arrange - resolver EAI_SYSTEM arrives as a bare OSError carrying the system errno;
    # Lambda's resolver reports NXDOMAIN and throttling this way, as OSError(EBUSY)
    mock_getaddrinfo.side_effect = OSError(errno.EBUSY, "Device or resource busy")

    # act & assert
    with pytest.raises(ResolutionFailure) as exc_info:
        _resolve(TARGET_URL, TARGET_PORT, socket.SOCK_STREAM)
    assert str(exc_info.value) == f"[Errno {errno.EBUSY}] Device or resource busy"
