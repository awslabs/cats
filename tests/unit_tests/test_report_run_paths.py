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

"""Tests for cats.report_run.ReportRun path selection and resolve_report_run."""

from datetime import UTC, datetime
from pathlib import Path

from cats.config import CatsReportingSettings
from cats.report_run import ReportFile, ReportRun, resolve_report_run

REPORT = ReportFile(stem="report", suffix=".json")
RUN_ID = "20260924T105317Z"
REPORT_DIR = Path("/project/reports")
STARTED_AT = datetime(2026, 9, 24, 10, 53, 17, tzinfo=UTC)


def make_run(latest_files: bool, timestamped_files: bool) -> ReportRun:
    return ReportRun(report_dir=REPORT_DIR, run_id=RUN_ID, started_at=STARTED_AT, latest_files=latest_files, timestamped_files=timestamped_files)


def test_latest_only_writes_the_fixed_name_directly() -> None:
    """Today's behavior: one file, fixed name, no copy."""
    # act
    run = make_run(latest_files=True, timestamped_files=False)

    # assert
    expected = (REPORT_DIR / "report.json", None, [REPORT_DIR / "report.json"])
    assert expected == (run.primary_path(REPORT), run.copy_path(REPORT), run.paths(REPORT))


def test_both_flags_write_the_timestamped_file_and_copy_to_the_fixed_name() -> None:
    """The timestamped file is the evidence (written once); the fixed name is the copy consumers read."""
    # act
    run = make_run(latest_files=True, timestamped_files=True)

    # assert
    expected = (
        REPORT_DIR / "report-20260924T105317Z.json",
        REPORT_DIR / "report.json",
        [REPORT_DIR / "report-20260924T105317Z.json", REPORT_DIR / "report.json"],
    )
    assert expected == (run.primary_path(REPORT), run.copy_path(REPORT), run.paths(REPORT))


def test_timestamped_only_writes_no_fixed_name() -> None:
    # act
    run = make_run(latest_files=False, timestamped_files=True)

    # assert
    expected = (REPORT_DIR / "report-20260924T105317Z.json", None, [REPORT_DIR / "report-20260924T105317Z.json"])
    assert expected == (run.primary_path(REPORT), run.copy_path(REPORT), run.paths(REPORT))


def test_neither_flag_writes_nothing() -> None:
    # act
    run = make_run(latest_files=False, timestamped_files=False)

    # assert
    expected: tuple[Path | None, Path | None, list[Path], bool] = (None, None, [], False)
    assert expected == (run.primary_path(REPORT), run.copy_path(REPORT), run.paths(REPORT), run.writes_files)


def test_writes_files_when_either_flag_is_on() -> None:
    # act
    results = [make_run(latest, timestamped).writes_files for latest, timestamped in ((True, False), (False, True), (True, True))]

    # assert
    expected = [True, True, True]
    assert expected == results


def test_resolve_report_run_combines_directory_run_id_and_flags(tmp_path: Path) -> None:
    # arrange
    settings = CatsReportingSettings(report_dir="reports", latest_files=False, timestamped_files=True)
    config_path = tmp_path / "cats.yaml"

    # act
    run = resolve_report_run(
        cli_report_dir=None,
        settings=settings,
        config_path=config_path,
        files=[REPORT],
        now=STARTED_AT,
    )

    # assert
    expected = ReportRun(report_dir=tmp_path / "reports", run_id=RUN_ID, started_at=STARTED_AT, latest_files=False, timestamped_files=True)
    assert expected == run
