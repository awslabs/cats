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

"""Tests for cats/cfn/ec2_server/probe.py classify - the exception-to-category table."""

import errno
import socket
import ssl
from http.client import BadStatusLine, HTTPException, IncompleteRead, LineTooLong, RemoteDisconnected

import pytest

from cats.cfn.ec2_server.probe import ResolutionFailure, classify

# EAI_NODATA is not defined on every platform; the probe falls back to EAI_NONAME.
EAI_NODATA = getattr(socket, "EAI_NODATA", socket.EAI_NONAME)
# EAI_ADDRFAMILY is not defined on every platform either; any non-DNS gaierror code will do.
EAI_ADDRFAMILY = getattr(socket, "EAI_ADDRFAMILY", 9999)


# =============================================================================
# TLS_FAILURE
# =============================================================================


def test_ssl_error_is_tls_failure() -> None:
    # arrange
    exc = ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number")

    # act & assert
    assert classify(exc) == "TLS_FAILURE"


def test_ssl_cert_verification_error_is_tls_failure() -> None:
    # arrange - subclasses both SSLError and ValueError; the MRO reaches SSLError first
    exc = ssl.SSLCertVerificationError(1, "certificate verify failed")

    # act & assert
    assert classify(exc) == "TLS_FAILURE"


def test_ssl_eof_error_is_tls_failure() -> None:
    # arrange
    exc = ssl.SSLEOFError(8, "EOF occurred in violation of protocol")

    # act & assert
    assert classify(exc) == "TLS_FAILURE"


# =============================================================================
# DNS_FAILURE - by gaierror errno, not by type
# =============================================================================


@pytest.mark.parametrize(
    "eai_code",
    [
        pytest.param(socket.EAI_NONAME, id="eai_noname_nxdomain"),
        pytest.param(EAI_NODATA, id="eai_nodata"),
        pytest.param(socket.EAI_AGAIN, id="eai_again_resolver_unreachable"),
        pytest.param(socket.EAI_FAIL, id="eai_fail"),
    ],
)
def test_resolver_gaierror_is_dns_failure(eai_code: int) -> None:
    # arrange
    exc = socket.gaierror(eai_code, "Name or service not known")

    # act & assert
    assert classify(exc) == "DNS_FAILURE"


def test_non_resolver_gaierror_is_failure() -> None:
    # arrange - the resolver answered; the local stack could not use the address family
    exc = socket.gaierror(EAI_ADDRFAMILY, "Address family for hostname not supported")

    # act & assert
    assert classify(exc) == "FAILURE"


def test_resolution_failure_is_dns_failure() -> None:
    # arrange - _resolve raises this for any failure inside the name-resolution step;
    # position, not exception shape, is what makes it a DNS claim
    exc = ResolutionFailure("[Errno 16] Device or resource busy")

    # act & assert
    assert classify(exc) == "DNS_FAILURE"


def test_bare_ebusy_oserror_outside_the_resolve_step_is_failure() -> None:
    # arrange - the shape alone proves nothing: only _resolve may convert EBUSY into a DNS claim
    exc = OSError(errno.EBUSY, "Device or resource busy")

    # act & assert
    assert classify(exc) == "FAILURE"


def test_ssl_error_carrying_ebusy_errno_stays_tls_failure() -> None:
    # arrange - an SSLError is a TLS outcome whatever errno it carries
    exc = ssl.SSLError(errno.EBUSY, "handshake failure")

    # act & assert
    assert classify(exc) == "TLS_FAILURE"


def test_gaierror_carrying_ebusy_errno_is_failure() -> None:
    # arrange - a gaierror whose code is not a resolver DNS_ERRNOS entry is not a DNS claim
    exc = socket.gaierror(errno.EBUSY, "Device or resource busy")

    # act & assert
    assert classify(exc) == "FAILURE"


def test_connection_refused_carrying_ebusy_errno_stays_refused() -> None:
    # arrange - OSError subclasses classify by their own table row whatever errno they carry
    exc = ConnectionRefusedError(errno.EBUSY, "Connection refused")

    # act & assert
    assert classify(exc) == "REFUSED"


# =============================================================================
# REFUSED
# =============================================================================


def test_connection_refused_is_refused() -> None:
    # arrange
    exc = ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")

    # act & assert
    assert classify(exc) == "REFUSED"


def test_connection_reset_is_refused() -> None:
    # arrange
    exc = ConnectionResetError(errno.ECONNRESET, "Connection reset by peer")

    # act & assert
    assert classify(exc) == "REFUSED"


def test_remote_disconnected_is_refused() -> None:
    # arrange - http.client.RemoteDisconnected subclasses ConnectionResetError
    exc = RemoteDisconnected("Remote end closed connection without response")

    # act & assert
    assert classify(exc) == "REFUSED"


# =============================================================================
# TIMEOUT
# =============================================================================


def test_socket_timeout_is_timeout() -> None:
    # arrange - a distinct OSError subclass on AL2023's Python 3.9; pyupgrade must not fold it into TimeoutError
    exc = socket.timeout("timed out")  # noqa: UP041

    # act & assert
    assert classify(exc) == "TIMEOUT"


def test_timeout_error_is_timeout() -> None:
    # arrange
    exc = TimeoutError("timed out")

    # act & assert
    assert classify(exc) == "TIMEOUT"


# =============================================================================
# FAILURE - unclassified OSError, errno stays visible in reason
# =============================================================================


@pytest.mark.parametrize(
    "err",
    [
        pytest.param(errno.ENETUNREACH, id="enetunreach"),
        pytest.param(errno.EHOSTUNREACH, id="ehostunreach"),
        pytest.param(errno.EADDRNOTAVAIL, id="eaddrnotavail"),
    ],
)
def test_other_oserror_is_failure(err: int) -> None:
    # arrange
    exc = OSError(err, "unreachable")

    # act & assert
    assert classify(exc) == "FAILURE"


# =============================================================================
# FAILURE - the peer answered, but not with HTTP; the reply is the evidence
# =============================================================================


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(BadStatusLine("SSH-2.0-OpenSSH_9.6\r\n"), id="bad_status_line"),
        pytest.param(LineTooLong("status line"), id="line_too_long"),
        pytest.param(IncompleteRead(b"HTTP/1.1 20"), id="incomplete_read"),
    ],
)
def test_http_protocol_exception_is_failure(exc: HTTPException) -> None:
    # act & assert
    assert classify(exc) == "FAILURE"


# =============================================================================
# ERROR - not a network outcome
# =============================================================================


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(ValueError("port must be an integer in 1..65535, got 0"), id="value_error"),
        pytest.param(KeyError("SSM_TargetUrl"), id="key_error"),
        pytest.param(Exception("boom"), id="bare_exception"),
    ],
)
def test_non_network_exception_is_error(exc: BaseException) -> None:
    # act & assert
    assert classify(exc) == "ERROR"
