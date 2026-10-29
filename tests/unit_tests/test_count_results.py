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

"""Tests for cats.report_run.count_results, and the pin between ResultType, RESULT_ORDER and ResultCounts."""

from typing import get_args, get_type_hints

from cats.report_run import RESULT_ORDER, ResultCounts, count_results
from cats.reporting import ReportEntry, ResultType


def make_entry(result: ResultType) -> ReportEntry:
    return {
        "result": result,
        "accounts_in_test": [],
        "test_name": f"test_{result}",
        "nodeid": f"tests/test_a.py::test_{result}",
        "test_docstring": None,
        "standards": [],
        "standard_metadata": {},
        "custom_markers": {},
        "additional_metadata": {},
        "xfail": False,
        "error_message": None,
        "reason": None,
        "duration_s": 0.1,
        "recorded_at": "2026-09-24T10:53:17+00:00",
    }


def test_no_entries_yields_every_key_at_zero() -> None:
    # act
    counts = count_results([])

    # assert
    expected: ResultCounts = {"total": 0, "passed": 0, "failed": 0, "error": 0, "skipped": 0, "xfailed": 0, "xpassed": 0, "not_run": 0}
    assert expected == counts


def test_every_result_is_tallied_and_total_is_the_sum() -> None:
    """A not_run entry is an entry: it counts in total, as a skipped one does."""
    # arrange
    entries = [
        make_entry("passed"),
        make_entry("passed"),
        make_entry("failed"),
        make_entry("error"),
        make_entry("skipped"),
        make_entry("xfailed"),
        make_entry("xpassed"),
        make_entry("not_run"),
    ]

    # act
    counts = count_results(entries)

    # assert
    expected: ResultCounts = {"total": 8, "passed": 2, "failed": 1, "error": 1, "skipped": 1, "xfailed": 1, "xpassed": 1, "not_run": 1}
    assert expected == counts


def test_keys_come_in_the_documented_order() -> None:
    """The order the terminal summary prints them in: what failed before what did not run."""
    # act
    counts = count_results([make_entry("skipped")])

    # assert
    expected = ["total", "passed", "failed", "error", "skipped", "xfailed", "xpassed", "not_run"]
    assert expected == list(counts)


def test_result_order_is_every_result_type_once() -> None:
    # arrange
    expected = sorted(get_args(ResultType))

    # act
    ordered = sorted(RESULT_ORDER)

    # assert
    assert expected == ordered


def test_result_counts_keys_are_total_then_result_order() -> None:
    # arrange
    expected = ["total", *RESULT_ORDER]

    # act
    keys = list(get_type_hints(ResultCounts))

    # assert
    assert expected == keys
