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

"""Tests for cats/resources/network_interact.py _validate_host.

The deployed probe trusts its caller: a URL, a host:port, or whitespace pasted where a
hostname belongs would be resolved as-is and misreported as DNS_FAILURE. Only hostname
characters and IPv6 literals pass; everything else is rejected before any transport is invoked.
"""

import re

import pytest

from cats.exceptions import CatsUsageError
from cats.resources.network_interact import _validate_host

NOT_A_HOST = (
    "host must be a hostname or IP literal, not a URL or host:port, got {host!r} "
    + "(the method you call chooses the protocol; pass the port as the second argument)"
)


@pytest.mark.parametrize(
    "host",
    [
        pytest.param("example.com", id="hostname"),
        pytest.param("ip-10-0-1-5.eu-west-1.compute.internal", id="ec2_private_dns"),
        pytest.param("example.com.", id="fully_qualified_with_trailing_dot"),
        pytest.param("my_host.internal", id="underscore_label"),
        pytest.param("10.0.1.5", id="ipv4_literal"),
        pytest.param("fd00::5", id="ipv6_literal"),
        pytest.param("::1", id="ipv6_loopback"),
        pytest.param("localhost", id="single_label"),
    ],
)
def test_valid_host_is_returned_unchanged(host: str) -> None:
    # act
    result = _validate_host(host)

    # assert
    assert result == host


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        pytest.param("[fd00::5]", "fd00::5", id="bracketed_ipv6"),
        pytest.param("[::1]", "::1", id="bracketed_loopback"),
    ],
)
def test_bracketed_ipv6_literal_is_returned_bare(host: str, expected: str) -> None:
    # act
    result = _validate_host(host)

    # assert
    assert result == expected


@pytest.mark.parametrize(
    "host",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace_only"),
        pytest.param("example.com ", id="trailing_space"),
        pytest.param("exam ple.com", id="inner_space"),
        pytest.param("http://example.com", id="scheme"),
        pytest.param("example.com/health", id="path"),
        pytest.param("user@example.com", id="userinfo"),
        pytest.param("example.com:443", id="host_port"),
        pytest.param("[fd00::5]:443", id="bracketed_ipv6_with_port"),
        pytest.param("[not-an-address]", id="brackets_around_a_non_literal"),
        pytest.param("[]", id="empty_brackets"),
    ],
)
def test_invalid_host_raises_usage_error(host: str) -> None:
    # act & assert
    with pytest.raises(CatsUsageError, match=f"^{re.escape(NOT_A_HOST.format(host=host))}$"):
        _validate_host(host)


def test_non_string_host_raises_usage_error() -> None:
    # act & assert
    with pytest.raises(CatsUsageError, match=r"^host must be a hostname or IP literal, got None$"):
        _validate_host(None)  # type: ignore[arg-type]
