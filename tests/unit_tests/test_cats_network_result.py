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

"""Tests for cats.constants.CatsNetworkResult - category-only equality, hash, repr."""

import pytest

from cats.constants import CatsNetworkResponse, CatsNetworkResult


def make_result(
    response: CatsNetworkResponse = CatsNetworkResponse.TIMEOUT,
    reason: str | None = None,
    http_status: int | None = None,
    correlation_id: str | None = None,
) -> CatsNetworkResult:
    return CatsNetworkResult(response=response, reason=reason, http_status=http_status, correlation_id=correlation_id)


# =============================================================================
# Equality
# =============================================================================


def test_result_equals_enum_member() -> None:
    # arrange
    result = make_result(CatsNetworkResponse.TIMEOUT, reason="timed out")

    # act & assert
    assert result == CatsNetworkResponse.TIMEOUT


def test_enum_member_equals_result_reflected() -> None:
    # arrange
    result = make_result(CatsNetworkResponse.TIMEOUT, reason="timed out")

    # act & assert
    assert CatsNetworkResponse.TIMEOUT == result  # noqa: SIM300 - reflected direction is the point


def test_results_with_same_category_are_equal() -> None:
    # arrange
    left = make_result(CatsNetworkResponse.REFUSED, reason="[Errno 111] Connection refused")
    right = make_result(CatsNetworkResponse.REFUSED, reason="[Errno 104] Connection reset by peer")

    # act & assert
    assert left == right


def test_results_with_different_categories_are_not_equal() -> None:
    # arrange
    left = make_result(CatsNetworkResponse.TIMEOUT)
    right = make_result(CatsNetworkResponse.REFUSED)

    # act & assert
    assert left != right


def test_result_not_equal_to_other_enum_member() -> None:
    # arrange
    result = make_result(CatsNetworkResponse.DNS_FAILURE)

    # act & assert
    assert result != CatsNetworkResponse.TIMEOUT


def test_result_compared_to_string_is_false() -> None:
    # arrange
    result = make_result(CatsNetworkResponse.TIMEOUT)

    # act & assert
    assert (result == "TIMEOUT") is False


def test_string_compared_to_result_is_false() -> None:
    # arrange
    result = make_result(CatsNetworkResponse.TIMEOUT)

    # act & assert
    assert ("TIMEOUT" == result) is False  # noqa: SIM300 - reflected direction is the point


def test_result_compared_to_string_is_not_equal() -> None:
    # arrange
    result = make_result(CatsNetworkResponse.TIMEOUT)

    # act & assert
    assert result != "TIMEOUT"


def test_reason_is_excluded_from_equality() -> None:
    # arrange
    with_reason = make_result(CatsNetworkResponse.FAILURE, reason="[Errno 101] Network is unreachable")
    without_reason = make_result(CatsNetworkResponse.FAILURE)

    # act & assert
    assert with_reason == without_reason


def test_http_status_is_excluded_from_equality() -> None:
    # arrange
    ok = make_result(CatsNetworkResponse.SUCCESS, http_status=200)
    forbidden = make_result(CatsNetworkResponse.SUCCESS, http_status=403)

    # act & assert
    assert ok == forbidden


def test_correlation_id_is_excluded_from_equality() -> None:
    # arrange
    first = make_result(CatsNetworkResponse.SENT, correlation_id="aaa")
    second = make_result(CatsNetworkResponse.SENT, correlation_id="bbb")

    # act & assert
    assert first == second


# =============================================================================
# Hash agreement with the enum
# =============================================================================


def test_hash_equals_enum_hash() -> None:
    # arrange
    result = make_result(CatsNetworkResponse.TIMEOUT, reason="timed out")

    # act & assert
    assert hash(result) == hash(CatsNetworkResponse.TIMEOUT)


def test_dict_keyed_by_enum_matches_dict_keyed_by_result() -> None:
    # arrange
    by_enum = {CatsNetworkResponse.TIMEOUT: "blocked", CatsNetworkResponse.REFUSED: "open"}
    by_result = {make_result(CatsNetworkResponse.TIMEOUT, reason="x"): "blocked", make_result(CatsNetworkResponse.REFUSED, reason="y"): "open"}

    # act & assert
    assert by_enum == by_result


def test_result_is_member_of_enum_set() -> None:
    # arrange
    expected_outcomes = {CatsNetworkResponse.TIMEOUT, CatsNetworkResponse.DNS_FAILURE}
    result = make_result(CatsNetworkResponse.DNS_FAILURE, reason="[Errno -2] Name or service not known")

    # act & assert
    assert result in expected_outcomes


def test_enum_is_member_of_result_set() -> None:
    # arrange
    results = {make_result(CatsNetworkResponse.TIMEOUT, reason="a"), make_result(CatsNetworkResponse.TIMEOUT, reason="b")}

    # act & assert
    assert CatsNetworkResponse.TIMEOUT in results
    assert len(results) == 1


# =============================================================================
# value
# =============================================================================


def test_value_delegates_to_response_value() -> None:
    # arrange
    result = make_result(CatsNetworkResponse.TLS_FAILURE)

    # act & assert
    assert result.value == "TLS_FAILURE"


# =============================================================================
# repr - the diagnosis, None fields omitted
# =============================================================================


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        pytest.param(make_result(CatsNetworkResponse.SUCCESS), "SUCCESS()", id="no_fields"),
        pytest.param(make_result(CatsNetworkResponse.TIMEOUT, reason="timed out"), "TIMEOUT('timed out')", id="reason_only"),
        pytest.param(
            make_result(CatsNetworkResponse.DNS_FAILURE, reason="[Errno -2] Name or service not known"),
            "DNS_FAILURE('[Errno -2] Name or service not known')",
            id="dns_reason",
        ),
        pytest.param(make_result(CatsNetworkResponse.SUCCESS, http_status=403), "SUCCESS(http_status=403)", id="http_status_only"),
        pytest.param(
            make_result(CatsNetworkResponse.SENT, correlation_id="3f2a"),
            "SENT(correlation_id='3f2a')",
            id="correlation_id_only",
        ),
        pytest.param(
            make_result(CatsNetworkResponse.DNS_FAILURE, reason="[Errno -2] Name or service not known", correlation_id="3f2a"),
            "DNS_FAILURE('[Errno -2] Name or service not known', correlation_id='3f2a')",
            id="reason_and_correlation_id",
        ),
        pytest.param(
            make_result(CatsNetworkResponse.SUCCESS, reason="r", http_status=200, correlation_id="c"),
            "SUCCESS('r', http_status=200, correlation_id='c')",
            id="all_fields",
        ),
    ],
)
def test_repr_carries_the_diagnosis(result: CatsNetworkResult, expected: str) -> None:
    # act & assert
    assert repr(result) == expected


# =============================================================================
# Invariants
# =============================================================================


def test_result_is_frozen() -> None:
    # arrange
    result = make_result(CatsNetworkResponse.TIMEOUT)

    # act & assert
    with pytest.raises(AttributeError):
        result.response = CatsNetworkResponse.SUCCESS  # type: ignore[misc]


def test_error_is_not_an_enum_member() -> None:
    # act & assert - ERROR is an infrastructure fault on the wire, never a network outcome
    with pytest.raises(KeyError):
        CatsNetworkResponse["ERROR"]


def test_enum_members_are_the_documented_taxonomy() -> None:
    # arrange
    expected = ["SUCCESS", "REFUSED", "TIMEOUT", "DNS_FAILURE", "TLS_FAILURE", "FAILURE", "SENT"]

    # act
    result = [member.value for member in CatsNetworkResponse]

    # assert
    assert result == expected
