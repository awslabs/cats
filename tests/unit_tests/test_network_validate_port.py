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

"""Tests for cats/resources/network_interact.py _validate_port.

The deployed probe trusts its caller: the library validates before any transport is
invoked, so a caller's mistake raises here and can never surface as a network category.
"""

import re

import pytest

from cats.exceptions import CatsUsageError
from cats.resources.network_interact import _validate_port


@pytest.mark.parametrize("port", [1, 8080, 65535])
def test_valid_port_passes(port: int) -> None:
    # act & assert - no exception
    _validate_port(port)


@pytest.mark.parametrize(
    ("port", "expected_message"),
    [
        pytest.param(0, "port must be an integer in 1..65535, got 0", id="port_zero"),
        pytest.param(65536, "port must be an integer in 1..65535, got 65536", id="port_too_high"),
        pytest.param(-443, "port must be an integer in 1..65535, got -443", id="port_negative"),
        pytest.param("8080", "port must be an integer in 1..65535, got '8080'", id="port_not_int"),
        pytest.param(True, "port must be an integer in 1..65535, got True", id="port_bool"),
    ],
)
def test_invalid_port_raises_usage_error(port: object, expected_message: str) -> None:
    # act & assert
    with pytest.raises(CatsUsageError, match=f"^{re.escape(expected_message)}$"):
        _validate_port(port)  # type: ignore[arg-type]
