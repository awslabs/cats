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

"""Tests for cats.pytest_plugin._run_report_writers - writer isolation and loud failure."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from cats.config import ReportFormat
from cats.exceptions import CatsError
from cats.pytest_plugin import _run_report_writers
from cats.report_run import ReportFile, ReportRun
from tests.utils import make_run_manifest

RUN_ID = "20260924T105317Z"
MANIFEST = make_run_manifest(run_id=RUN_ID)


def _run(tmp_path: Path, latest_files: bool = True, timestamped_files: bool = False) -> ReportRun:
    return ReportRun(
        report_dir=tmp_path,
        run_id=RUN_ID,
        started_at=datetime(2026, 9, 24, 10, 53, 17, tzinfo=UTC),
        latest_files=latest_files,
        timestamped_files=timestamped_files,
    )


def _fake_writer(mocker: MockerFixture, stem: str) -> MagicMock:
    writer: MagicMock = mocker.MagicMock()
    writer.file = ReportFile(stem=stem, suffix=".json")
    return writer


def test_all_writers_succeeding_raises_nothing(mocker: MockerFixture, tmp_path: Path) -> None:
    # arrange
    json_writer = _fake_writer(mocker, "report")
    oscal_writer = _fake_writer(mocker, "assessment-results")
    mocker.patch("cats.pytest_plugin.get_writer", side_effect=[json_writer, oscal_writer])

    # act
    _run_report_writers([ReportFormat.CATS_JSON, ReportFormat.OSCAL_AR], _run(tmp_path), MANIFEST, None)

    # assert
    expected = (1, 1)
    assert expected == (json_writer.write.call_count, oscal_writer.write.call_count)


def test_writer_receives_the_primary_path_and_manifest(mocker: MockerFixture, tmp_path: Path) -> None:
    """With timestamped files on, the writer is told to write the timestamped file, not the fixed name."""
    # arrange
    json_writer = _fake_writer(mocker, "report")
    mocker.patch("cats.pytest_plugin.get_writer", side_effect=[json_writer])
    run = _run(tmp_path, latest_files=True, timestamped_files=True)

    # act
    _run_report_writers([ReportFormat.CATS_JSON], run, MANIFEST, None)

    # assert
    expected = (tmp_path / "report-20260924T105317Z.json", MANIFEST)
    assert expected == (json_writer.write.call_args.args[1], json_writer.write.call_args.args[2])


def test_no_file_naming_enabled_still_calls_every_writer_with_no_path(mocker: MockerFixture, tmp_path: Path) -> None:
    """latest_files and timestamped_files both false: the writer decides, not the plugin.

    A file-only writer returns at once; a writer that also delivers elsewhere still delivers.
    """
    # arrange
    json_writer = _fake_writer(mocker, "report")
    mocker.patch("cats.pytest_plugin.get_writer", side_effect=[json_writer])

    # act
    _run_report_writers([ReportFormat.CATS_JSON], _run(tmp_path, latest_files=False, timestamped_files=False), MANIFEST, None)

    # assert
    expected = (None, MANIFEST)
    assert expected == (json_writer.write.call_args.args[1], json_writer.write.call_args.args[2])


def test_writer_notes_are_collected_in_writer_order(mocker: MockerFixture, tmp_path: Path) -> None:
    """A writer's returned lines reach the terminal summary; a writer returning None adds nothing."""
    # arrange
    silent_writer = _fake_writer(mocker, "report")
    silent_writer.write.return_value = None
    talking_writer = _fake_writer(mocker, "securityhub-findings")
    talking_writer.write.return_value = ["Security Hub (eu-west-1): prod 2 findings imported"]
    mocker.patch("cats.pytest_plugin.get_writer", side_effect=[silent_writer, talking_writer])

    # act
    notes = _run_report_writers([ReportFormat.CATS_JSON, ReportFormat.SECURITYHUB], _run(tmp_path), MANIFEST, None)

    # assert
    expected = ["Security Hub (eu-west-1): prod 2 findings imported"]
    assert expected == notes


def test_junit_format_is_not_dispatched_to_a_writer(mocker: MockerFixture, tmp_path: Path) -> None:
    """junit.xml was written by pytest's junitxml plugin during the run; there is nothing to call."""
    # arrange
    get_writer = mocker.patch("cats.pytest_plugin.get_writer")

    # act
    notes = _run_report_writers([ReportFormat.JUNIT], _run(tmp_path), MANIFEST, None)

    # assert
    expected: tuple[int, list[str]] = (0, [])
    assert expected == (get_writer.call_count, notes)


def test_failing_writer_does_not_prevent_later_writers(mocker: MockerFixture, tmp_path: Path) -> None:
    """One writer crashing must not lose the other writer's evidence file."""
    # arrange
    failing_writer = _fake_writer(mocker, "report")
    failing_writer.write.side_effect = RuntimeError("disk full")
    succeeding_writer = _fake_writer(mocker, "assessment-results")
    mocker.patch("cats.pytest_plugin.get_writer", side_effect=[failing_writer, succeeding_writer])

    # act
    with pytest.raises(CatsError) as excinfo:
        _run_report_writers([ReportFormat.CATS_JSON, ReportFormat.OSCAL_AR], _run(tmp_path), MANIFEST, None)

    # assert
    expected = (1, "Report writing failed for: cats_json: RuntimeError('disk full')")
    assert expected == (succeeding_writer.write.call_count, str(excinfo.value))
