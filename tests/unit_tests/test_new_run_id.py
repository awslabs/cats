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

"""Tests for cats.report_run.new_run_id."""

from datetime import UTC, datetime
from pathlib import Path

from cats.report_run import JUNIT_FILE, ReportFile, new_run_id

NOW = datetime(2026, 9, 24, 10, 53, 17, 123456, tzinfo=UTC)
REPORT = ReportFile(stem="report", suffix=".json")


def test_run_id_is_utc_basic_iso_8601_to_the_second(tmp_path: Path) -> None:
    """No colons (Windows-safe), sorts lexically, sub-second part dropped."""
    # act
    run_id = new_run_id(NOW, tmp_path, [REPORT])

    # assert
    expected = "20260924T105317Z"
    assert expected == run_id


def test_existing_timestamped_file_gets_a_suffix(tmp_path: Path) -> None:
    """A second run in the same second must never overwrite the first run's evidence."""
    # arrange
    (tmp_path / "report-20260924T105317Z.json").write_text("[]")

    # act
    run_id = new_run_id(NOW, tmp_path, [REPORT])

    # assert
    expected = "20260924T105317Z-2"
    assert expected == run_id


def test_suffix_keeps_counting_past_taken_suffixes(tmp_path: Path) -> None:
    # arrange
    (tmp_path / "report-20260924T105317Z.json").write_text("[]")
    (tmp_path / "report-20260924T105317Z-2.json").write_text("[]")

    # act
    run_id = new_run_id(NOW, tmp_path, [REPORT])

    # assert
    expected = "20260924T105317Z-3"
    assert expected == run_id


def test_collision_on_any_file_kind_counts(tmp_path: Path) -> None:
    """A leftover junit file alone is enough: the id must be free for every file of the run."""
    # arrange
    (tmp_path / "junit-20260924T105317Z.xml").write_text("<testsuites/>")

    # act
    run_id = new_run_id(NOW, tmp_path, [REPORT, JUNIT_FILE])

    # assert
    expected = "20260924T105317Z-2"
    assert expected == run_id


def test_missing_report_dir_yields_the_plain_id(tmp_path: Path) -> None:
    # act
    run_id = new_run_id(NOW, tmp_path / "not-yet-created", [REPORT])

    # assert
    expected = "20260924T105317Z"
    assert expected == run_id


def test_reserving_creates_the_first_timestamped_file_so_a_concurrent_session_sees_it(tmp_path: Path) -> None:
    """Two sessions starting in one second take distinct ids: the first reserves its id by creating the timestamped file."""
    # act
    run_id = new_run_id(NOW, tmp_path, [REPORT, JUNIT_FILE], reserve=True)

    # assert
    expected = ("20260924T105317Z", True, "")
    reserved = tmp_path / "report-20260924T105317Z.json"
    assert expected == (run_id, reserved.exists(), reserved.read_text())


def test_second_reservation_in_the_same_second_takes_the_next_suffix(tmp_path: Path) -> None:
    # arrange
    new_run_id(NOW, tmp_path, [REPORT], reserve=True)

    # act
    run_id = new_run_id(NOW, tmp_path, [REPORT], reserve=True)

    # assert
    expected = "20260924T105317Z-2"
    assert expected == run_id


def test_reserving_creates_a_missing_report_dir(tmp_path: Path) -> None:
    # arrange
    report_dir = tmp_path / "reports" / "nested"

    # act
    run_id = new_run_id(NOW, report_dir, [REPORT], reserve=True)

    # assert
    assert (run_id, (report_dir / "report-20260924T105317Z.json").exists()) == ("20260924T105317Z", True)


def test_without_reserving_nothing_is_written(tmp_path: Path) -> None:
    """Fixed-name-only runs never reserve: truncating report.json at configure time would destroy the previous run's evidence."""
    # act
    new_run_id(NOW, tmp_path, [REPORT])

    # assert
    assert sorted(tmp_path.iterdir()) == []


def test_reserve_with_no_files_returns_the_id_without_touching_the_directory(tmp_path: Path) -> None:
    """No file kind means nothing to reserve with: the id is still returned instead of an IndexError."""
    # act
    run_id = new_run_id(NOW, tmp_path, [], reserve=True)

    # assert
    expected = "20260924T105317Z"
    assert expected == run_id
    assert list(tmp_path.iterdir()) == []
