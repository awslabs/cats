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

"""Tests for cats.report_writers — registry and writer dispatch."""

import json
from pathlib import Path

import pytest

from cats.config import ReportFormat
from cats.hookspecs import StandardMetadata
from cats.pytest_plugin import _report_files
from cats.report_run import ReportFile
from cats.report_writers import get_writer
from cats.report_writers.cats_json import CatsJsonWriter
from cats.report_writers.oscal_ar import OscalArWriter
from cats.report_writers.securityhub import SecurityHubWriter
from cats.reporting import ReportEntry
from cats.user_agent import CatsTestPack
from cats.version import __version__
from tests.utils import make_run_manifest

RUN_ID = "20260924T105317Z"
MANIFEST = make_run_manifest(run_id=RUN_ID)
# The pack make_run_manifest declares, as CatsTestPack renders it into the User-Agent.
PACK = CatsTestPack(name="acme-controls", version="0.99.4", metadata={"framework": "NIST-800-53r5"})


def _make_entry() -> ReportEntry:
    """Build a minimal ReportEntry for testing."""
    return {
        "result": "passed",
        "accounts_in_test": [{"account_name": "Test", "account_id": "111111111111", "tags": {}}],
        "test_name": "test_example",
        "nodeid": "tests/test_module.py::test_example",
        "test_docstring": "An example test.",
        "standards": [],
        "custom_markers": {},
        "standard_metadata": {},
        "additional_metadata": {},
        "xfail": False,
        "error_message": None,
        "reason": None,
        "duration_s": 0.5,
        "recorded_at": "2026-09-24T10:53:17+00:00",
    }


def test_get_writer_returns_cats_json_writer() -> None:
    """get_writer(CATS_JSON) returns a CatsJsonWriter instance."""
    # act
    writer = get_writer(ReportFormat.CATS_JSON)

    # assert
    assert isinstance(writer, CatsJsonWriter)


def test_get_writer_returns_oscal_ar_writer() -> None:
    """get_writer(OSCAL_AR) returns an OscalArWriter instance."""
    # act
    writer = get_writer(ReportFormat.OSCAL_AR)

    # assert
    assert isinstance(writer, OscalArWriter)


def test_junit_has_no_writer() -> None:
    """pytest's junitxml plugin writes junit.xml; the registry holds only CATS writers."""
    # act
    with pytest.raises(KeyError):
        get_writer(ReportFormat.JUNIT)


def test_get_writer_returns_securityhub_writer() -> None:
    # act
    writer = get_writer(ReportFormat.SECURITYHUB)

    # assert
    assert isinstance(writer, SecurityHubWriter)


def test_every_format_names_its_report_file() -> None:
    """The plugin names files from these; they are the fixed names every consumer knows."""
    # act
    files = _report_files(list(ReportFormat))

    # assert
    expected = [
        ReportFile(stem="report", suffix=".json"),
        ReportFile(stem="assessment-results", suffix=".json"),
        ReportFile(stem="junit", suffix=".xml"),
        ReportFile(stem="securityhub-findings", suffix=".json"),
    ]
    assert expected == files


def test_report_files_follow_report_format_order() -> None:
    """junit.xml is listed where the user listed it, not appended last."""
    # act
    files = _report_files([ReportFormat.JUNIT, ReportFormat.CATS_JSON])

    # assert
    expected = [ReportFile(stem="junit", suffix=".xml"), ReportFile(stem="report", suffix=".json")]
    assert expected == files


def test_cats_json_writer_writes_to_the_given_path(tmp_path: Path) -> None:
    """CatsJsonWriter writes exactly where the plugin tells it to."""
    # arrange
    entry = _make_entry()
    output_path = tmp_path / "report-20260924T105317Z.json"

    # act
    CatsJsonWriter().write([entry], output_path, MANIFEST, None)

    # assert
    expected = {"schema_version": 1, "run": MANIFEST, "tests": [entry]}
    assert expected == json.loads(output_path.read_text())


def test_cats_json_writer_output_is_indented_json(tmp_path: Path) -> None:
    """report.json is human-readable: four-space indented, schema_version, then run, then tests."""
    # arrange
    entry = _make_entry()
    output_path = tmp_path / "report.json"

    # act
    CatsJsonWriter().write([entry], output_path, MANIFEST, None)
    actual = output_path.read_text()

    # assert — compare against direct json.dump of the document
    expected = json.dumps({"schema_version": 1, "run": MANIFEST, "tests": [entry]}, indent=4)
    assert actual == expected


def test_both_writers_produce_separate_files(tmp_path: Path) -> None:
    """Running both writers produces report.json and assessment-results.json."""
    # arrange
    entry = _make_entry()
    entry["standards"] = ["SC-7"]

    # act
    for fmt in [ReportFormat.CATS_JSON, ReportFormat.OSCAL_AR]:
        writer = get_writer(fmt)
        writer.write([entry], tmp_path / writer.file.fixed_name(), MANIFEST, None)

    # assert
    expected = {"assessment-results.json", "report.json"}
    assert expected == {path.name for path in tmp_path.iterdir()}


def test_cats_json_writer_ignores_standards_registry(tmp_path: Path) -> None:
    """CatsJsonWriter produces the same output regardless of the standards_registry."""
    # arrange
    entry = _make_entry()
    writer = CatsJsonWriter()
    registry = {"unused": StandardMetadata(control_ids=["SC-7"], control_group="Governance")}

    # act
    writer.write([entry], tmp_path / "with.json", MANIFEST, registry)
    with_registry = (tmp_path / "with.json").read_text()

    writer.write([entry], tmp_path / "without.json", MANIFEST, None)
    without_registry = (tmp_path / "without.json").read_text()

    # assert
    assert with_registry == without_registry


def test_oscal_writer_records_the_run_facts_in_metadata_props(tmp_path: Path) -> None:
    """The run id ties assessment-results.json to report.json; the session name and caller ARN tie both to CloudTrail;
    the commit, dirty flag, config hash and pack token say what code and configuration produced them."""
    # arrange
    output_path = tmp_path / "assessment-results.json"

    # act
    OscalArWriter().write([_make_entry()], output_path, MANIFEST, None)
    props = json.loads(output_path.read_text())["assessment-results"]["metadata"]["props"]

    # assert
    expected = [
        {"name": "tool-version", "value": __version__},
        {"name": "cats-run-id", "value": RUN_ID},
        {"name": "cats-role-session-name", "value": f"cats-{RUN_ID}"},
        {"name": "cats-caller-identity-arn", "value": "arn:aws:sts::999999999999:assumed-role/ci-runner/job-8812"},
        {"name": "cats-git-commit", "value": "3f2a9c1e0d4b7f8a9c2e1d3f5b6a7c8d9e0f1a2b"},
        {"name": "cats-git-dirty", "value": "false"},
        {"name": "cats-config-sha256", "value": "71aa0c2d6e8f1b3a5c7d9e0f2a4b6c8d0e1f3a5b7c9d1e3f5a7b9c1d3e5f7a9b"},
        {"name": "cats-test-pack", "value": PACK.user_agent_token},
    ]
    assert expected == props


def test_cats_json_writer_with_no_path_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Report files off: the file is this writer's only product, so there is nothing to do."""
    # arrange
    monkeypatch.chdir(tmp_path)

    # act
    CatsJsonWriter().write([_make_entry()], None, MANIFEST, None)

    # assert
    expected: list[Path] = []
    assert expected == list(tmp_path.iterdir())


def test_oscal_writer_with_no_path_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # arrange
    monkeypatch.chdir(tmp_path)

    # act
    OscalArWriter().write([_make_entry()], None, MANIFEST, None)

    # assert
    expected: list[Path] = []
    assert expected == list(tmp_path.iterdir())
