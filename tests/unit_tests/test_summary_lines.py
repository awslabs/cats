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

"""Tests for cats.pytest_plugin._summary_lines: the CATS section of pytest's terminal summary."""

from datetime import UTC, datetime
from pathlib import Path

from cats.pytest_plugin import _summary_lines
from cats.report_run import JUNIT_FILE, ReportFile, ReportRun, RunSelection
from cats.reporting import ReportEntry, ResultType
from cats.selection import ConfigExclusionRecord

REPORT = ReportFile(stem="report", suffix=".json")
RUN_ID = "20260924T105317Z"
# No command-line selector and no test_selection: the section is the counts and the files.
FULL_RUN = RunSelection(keyword="", markexpr="", last_failed=False, deselect=(), explicit_args=(), stopped_early="")


def make_entry(result: ResultType) -> ReportEntry:
    return {
        "result": result,
        "accounts_in_test": [],
        "test_name": f"test_{result}",
        "nodeid": f"tests/test_a.py::test_{result}",
        "test_docstring": None,
        "standards": [],
        "custom_markers": {},
        "standard_metadata": {},
        "additional_metadata": {},
        "xfail": False,
        "error_message": None,
        "reason": None,
        "duration_s": 0.1,
        "recorded_at": "2026-09-24T10:53:17+00:00",
    }


def make_run(tmp_path: Path, latest_files: bool = True, timestamped_files: bool = False) -> ReportRun:
    return ReportRun(
        report_dir=tmp_path,
        run_id=RUN_ID,
        started_at=datetime(2026, 9, 24, 10, 53, 17, tzinfo=UTC),
        latest_files=latest_files,
        timestamped_files=timestamped_files,
    )


def test_run_lists_counts_directory_and_every_file(tmp_path: Path) -> None:
    # arrange
    entries = [make_entry("passed"), make_entry("passed"), make_entry("failed"), make_entry("skipped")]
    run = make_run(tmp_path, latest_files=True, timestamped_files=True)

    # act
    lines = _summary_lines(run, FULL_RUN, entries, [REPORT, JUNIT_FILE], notes=[])

    # assert
    expected = [
        f"4 tests (2 passed, 1 failed, 1 skipped) written to {tmp_path.resolve()}",
        "  report-20260924T105317Z.json, report.json",
        "  junit-20260924T105317Z.xml, junit.xml",
    ]
    assert expected == lines


def test_one_recorded_test_is_singular(tmp_path: Path) -> None:
    # act
    lines = _summary_lines(make_run(tmp_path), FULL_RUN, [make_entry("passed")], [REPORT], notes=[])

    # assert
    expected = [f"1 test (1 passed) written to {tmp_path.resolve()}", "  report.json"]
    assert expected == lines


def test_no_recorded_tests_omits_the_breakdown(tmp_path: Path) -> None:
    # act
    lines = _summary_lines(make_run(tmp_path), FULL_RUN, [], [REPORT], notes=[])

    # assert
    expected = [f"0 tests written to {tmp_path.resolve()}", "  report.json"]
    assert expected == lines


def test_both_naming_flags_off_says_nothing_was_written(tmp_path: Path) -> None:
    # act
    lines = _summary_lines(make_run(tmp_path, latest_files=False, timestamped_files=False), FULL_RUN, [make_entry("passed")], [REPORT], notes=[])

    # assert
    expected = ["no report files written: reporting.latest_files and reporting.timestamped_files are both false"]
    assert expected == lines


def test_writer_notes_follow_the_file_list(tmp_path: Path) -> None:
    """A writer's own line (Security Hub delivery) sits with the files it describes."""
    # arrange
    notes = ["Security Hub (eu-west-1): prod 1 findings imported"]

    # act
    lines = _summary_lines(make_run(tmp_path, latest_files=True, timestamped_files=True), FULL_RUN, [make_entry("passed")], [REPORT], notes=notes)

    # assert
    expected = [
        f"1 test (1 passed) written to {tmp_path.resolve()}",
        "  report-20260924T105317Z.json, report.json",
        "Security Hub (eu-west-1): prod 1 findings imported",
    ]
    assert expected == lines


def test_writer_notes_still_print_when_no_files_were_written(tmp_path: Path) -> None:
    """Both naming flags off: the delivery happened even though no file did, and the user is told both."""
    # arrange
    run = make_run(tmp_path, latest_files=False, timestamped_files=False)
    notes = ["Security Hub (eu-west-1): prod 1 findings imported"]

    # act
    lines = _summary_lines(run, FULL_RUN, [make_entry("passed")], [REPORT], notes=notes)

    # assert
    expected = [
        "no report files written: reporting.latest_files and reporting.timestamped_files are both false",
        "Security Hub (eu-west-1): prod 1 findings imported",
    ]
    assert expected == lines


def test_tests_ran_but_no_evidence_recorded_names_the_cause(tmp_path: Path) -> None:
    """Under pytest-forked the hook runs in the child: 8 tests pass, the report records 0, and the summary says so."""
    # arrange
    run = make_run(tmp_path)

    # act
    lines = _summary_lines(run, FULL_RUN, [], [REPORT], notes=[], ran=8)

    # assert
    expected = [
        f"0 tests written to {tmp_path.resolve()}",
        "  report.json",
        "no evidence recorded for the 8 tests that ran: a plugin that runs tests out of process (other than pytest-xdist) is not supported",
    ]
    assert expected == lines


def test_one_test_ran_but_no_evidence_recorded_is_singular(tmp_path: Path) -> None:
    # act
    lines = _summary_lines(make_run(tmp_path), FULL_RUN, [], [REPORT], notes=[], ran=1)

    # assert
    expected = [
        f"0 tests written to {tmp_path.resolve()}",
        "  report.json",
        "no evidence recorded for the 1 test that ran: a plugin that runs tests out of process (other than pytest-xdist) is not supported",
    ]
    assert expected == lines


# ---------------------------------------------------------------------------
# test_selection: the scope line and the zero-match lines
# ---------------------------------------------------------------------------


def _not_run_record(nodeid: str, kind: str, excluded_index: int | None = None) -> ConfigExclusionRecord:
    return {
        "nodeid": nodeid,
        "standards": [],
        "requires": [],
        "kind": kind,  # type: ignore[typeddict-item]
        "excluded_index": excluded_index,
        "marker": None,
        "value": None,
        "not_provided": [],
        "reason": "the reason",
    }


def test_nothing_configured_and_nothing_held_back_prints_no_selection_line(tmp_path: Path) -> None:
    # act
    lines = _summary_lines(make_run(tmp_path), FULL_RUN, [make_entry("passed")], [REPORT], notes=[])

    # assert
    expected = [f"1 test (1 passed) written to {tmp_path.resolve()}", "  report.json"]
    assert expected == lines


def test_held_back_tests_are_counted_by_kind(tmp_path: Path) -> None:
    """Three tests held back by config: in the breakdown as not_run and named by kind on the selection line."""
    # arrange
    selection = RunSelection(
        keyword="",
        markexpr="",
        last_failed=False,
        deselect=(),
        explicit_args=(),
        stopped_early="",
        provided=("container-deployment",),
        config_excluded=(
            _not_run_record("a.py::t1", "excluded", excluded_index=0),
            _not_run_record("a.py::t2", "excluded", excluded_index=0),
            _not_run_record("a.py::t3", "not_provided"),
        ),
        configured_exclusions=({"marker": "standard_name", "values": ["X"], "reason": "r"},),
    )
    entries = [make_entry("passed"), make_entry("not_run"), make_entry("not_run"), make_entry("not_run")]

    # act
    lines = _summary_lines(make_run(tmp_path), selection, entries, [REPORT], notes=[])

    # assert
    expected = [
        f"4 tests (1 passed, 3 not_run) written to {tmp_path.resolve()}",
        "  report.json",
        "test_selection: provides container-deployment; 3 tests not run (2 excluded, 1 not provided), recorded as not_run with their reasons",
    ]
    assert expected == lines


def test_one_held_back_test_is_singular(tmp_path: Path) -> None:
    # arrange
    selection = RunSelection(
        keyword="",
        markexpr="",
        last_failed=False,
        deselect=(),
        explicit_args=(),
        stopped_early="",
        provided=(),
        config_excluded=(_not_run_record("a.py::t1", "not_provided"),),
    )

    # act
    lines = _summary_lines(make_run(tmp_path), selection, [make_entry("not_run")], [REPORT], notes=[])

    # assert
    expected = [
        f"1 test (1 not_run) written to {tmp_path.resolve()}",
        "  report.json",
        "test_selection: provides nothing; 1 test not run (0 excluded, 1 not provided), recorded as not_run with their reasons",
    ]
    assert expected == lines


def test_configured_selection_that_held_nothing_back_says_so(tmp_path: Path) -> None:
    # arrange
    selection = RunSelection(
        keyword="", markexpr="", last_failed=False, deselect=(), explicit_args=(), stopped_early="", provided=("a", "b"), config_excluded=()
    )

    # act
    lines = _summary_lines(make_run(tmp_path), selection, [make_entry("passed")], [REPORT], notes=[])

    # assert
    expected = [f"1 test (1 passed) written to {tmp_path.resolve()}", "  report.json", "test_selection: provides a, b; no test held back"]
    assert expected == lines


def test_each_unmatched_exclusion_gets_its_own_line_with_yaml_values(tmp_path: Path) -> None:
    """A boolean prints as YAML would (true), a string as itself; the index is the entry's position in the list."""
    # arrange
    selection = RunSelection(
        keyword="",
        markexpr="",
        last_failed=False,
        deselect=(),
        explicit_args=(),
        stopped_early="",
        provided=(),
        config_excluded=(_not_run_record("a.py::t1", "excluded", excluded_index=1),),
        configured_exclusions=(
            {"marker": "standard_name", "values": ["ACME-G-SCP-34", "ACME-G-SCP-35"], "reason": "r"},
            {"marker": "mutating", "values": [True], "reason": "r"},
            {"marker": "team", "values": ["nobody"], "reason": "r"},
        ),
    )

    # act
    lines = _summary_lines(make_run(tmp_path), selection, [make_entry("not_run")], [REPORT], notes=[])

    # assert
    expected = [
        f"1 test (1 not_run) written to {tmp_path.resolve()}",
        "  report.json",
        "test_selection: provides nothing; 1 test not run (1 excluded, 0 not provided), recorded as not_run with their reasons",
        "test_selection.excluded[0] (standard_name: ACME-G-SCP-34, ACME-G-SCP-35) matched 0 tests",
        "test_selection.excluded[2] (team: nobody) matched 0 tests",
    ]
    assert expected == lines


def test_keyword_selected_run_with_held_back_tests_adds_only_the_selection_line(tmp_path: Path) -> None:
    """-k narrowed the run and test_selection held two tests back: the section says what was held back and judges neither."""
    # arrange
    selection = RunSelection(
        keyword="ssh",
        markexpr="",
        last_failed=False,
        deselect=(),
        explicit_args=(),
        stopped_early="",
        provided=(),
        config_excluded=(_not_run_record("a.py::t1", "not_provided"), _not_run_record("a.py::t2", "not_provided")),
    )
    entries = [make_entry("passed"), make_entry("not_run"), make_entry("not_run")]

    # act
    lines = _summary_lines(make_run(tmp_path, latest_files=True, timestamped_files=True), selection, entries, [REPORT], notes=[])

    # assert
    expected = [
        f"3 tests (1 passed, 2 not_run) written to {tmp_path.resolve()}",
        "  report-20260924T105317Z.json, report.json",
        "test_selection: provides nothing; 2 tests not run (0 excluded, 2 not provided), recorded as not_run with their reasons",
    ]
    assert expected == lines
