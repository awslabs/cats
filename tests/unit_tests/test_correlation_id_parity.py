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

"""Coupling test: the client correlation ID regex and the server receipt key regex must be identical.

The UDP listener is stdlib-only Python 3.9 shipped to the instance, so it cannot import
the client pattern - it is defined twice by design.

If these tests fail, the two patterns have drifted apart - fix the pattern, not the test.
"""

import pytest

from cats.cfn.ec2_server.udpserver import RECEIPT_KEY_REGEX, extract_receipt_key
from cats.resources.network_interact import CORRELATION_ID_REGEX, _validate_correlation_id

TOKEN = "0123456789abcdef0123456789abcdef"  # noqa: S105


def test_client_pattern_matches_server_regex() -> None:
    # assert
    assert RECEIPT_KEY_REGEX.pattern == CORRELATION_ID_REGEX.pattern


@pytest.mark.parametrize(
    ("payload", "accepted"),
    [
        ("d5e2d1f0-3b0a-4f6e-9c1d-2a7b8c9d0e1f", True),
        ("a" * 128, True),
        ("a.B_c-d", True),
        ("a" * 129, False),
        ("junk key!", False),
        ("a:b", False),
        ("", False),
    ],
)
def test_client_and_server_agree_on_payload(payload: str, accepted: bool) -> None:
    # act
    server_accepted = extract_receipt_key(f"{TOKEN}:{payload}", TOKEN) is not None
    try:
        _validate_correlation_id(payload)
        client_accepted = True
    except ValueError:
        client_accepted = False

    # assert
    assert (client_accepted, server_accepted) == (accepted, accepted)
