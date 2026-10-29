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

"""Where report files land and how they are named, through real pytest runs.

``reporting.report_dir``, ``latest_files`` and ``timestamped_files`` are resolved once in
pytest_configure; the writers, pytest's junitxml plugin, the fixed-name copies and the
terminal summary all read that one resolution.
"""

import hashlib
import json
import platform
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from cats.report_run import RUN_ID_FORMAT, ResultCounts
from cats.version import __version__
from tests.utils import cats_summary, make_config

TEST_SOURCE = """
def test_ok():
    assert True

def test_boom():
    assert False
"""


def write_project(pytester: pytest.Pytester, reporting: dict[str, Any], test_source: str = TEST_SOURCE) -> Path:
    """Write cats.yaml with the given reporting block, a conftest pointing CATS_CONFIG at it, and a test file.

    The conftest also stops git looking above the project directory, so ``run.git`` is null
    for these projects wherever pytester's temp dir lives (it may sit inside a checkout).
    """
    config = make_config(accounts={"testaccount": "234567890123"})
    config["reporting"] = reporting
    config_path = pytester.path / "cats.yaml"
    config_path.write_text(yaml.dump(config))
    pytester.makeconftest(f'''
import os
os.environ["CATS_CONFIG"] = "{config_path}"
os.environ["GIT_CEILING_DIRECTORIES"] = "{pytester.path.parent}"
''')
    pytester.makepyfile(test_source)
    return config_path


def config_sha256_of(config_path: Path) -> str:
    """What ``sha256sum cats.yaml`` prints: the value run.config_sha256 must equal."""
    return hashlib.sha256(config_path.read_bytes()).hexdigest()


def report_files_in(directory: Path) -> set[str]:
    """Names of the report files in a directory (pytester's own stdout/stderr captures excluded)."""
    return {path.name for path in directory.iterdir() if path.suffix in {".json", ".xml"}}


def run_id_from(report_dir: Path) -> str:
    """The run id a timestamped report file carries in its name."""
    [timestamped] = [path for path in report_dir.iterdir() if path.name.startswith("report-")]
    return timestamped.stem.removeprefix("report-")


def test_default_config_writes_only_the_fixed_names(pytester: pytest.Pytester) -> None:
    """latest_files defaults on and timestamped_files defaults off: today's files, nothing more."""
    # arrange
    write_project(pytester, reporting={"report_formats": ["cats_json", "junit"]})

    # act
    pytester.runpytest_subprocess("-p", "no:randomly")

    # assert
    expected = {"junit.xml", "report.json"}
    assert expected == report_files_in(pytester.path)


def test_timestamped_files_add_one_immutable_set_per_run(pytester: pytest.Pytester) -> None:
    # arrange
    write_project(pytester, reporting={"report_dir": "reports", "timestamped_files": True, "report_formats": ["cats_json", "oscal_ar", "junit"]})
    report_dir = pytester.path / "reports"

    # act
    pytester.runpytest_subprocess("-p", "no:randomly")
    run_id = run_id_from(report_dir)

    # assert
    expected = {
        "assessment-results.json",
        f"assessment-results-{run_id}.json",
        "junit.xml",
        f"junit-{run_id}.xml",
        "report.json",
        f"report-{run_id}.json",
    }
    assert expected == report_files_in(report_dir)


def test_fixed_name_files_are_copies_of_the_timestamped_files(pytester: pytest.Pytester) -> None:
    # arrange
    write_project(pytester, reporting={"report_dir": "reports", "timestamped_files": True, "report_formats": ["cats_json", "junit"]})
    report_dir = pytester.path / "reports"

    # act
    pytester.runpytest_subprocess("-p", "no:randomly")
    run_id = run_id_from(report_dir)

    # assert
    expected = [
        (report_dir / f"report-{run_id}.json").read_bytes(),
        (report_dir / f"junit-{run_id}.xml").read_bytes(),
    ]
    assert expected == [(report_dir / "report.json").read_bytes(), (report_dir / "junit.xml").read_bytes()]


def test_run_id_is_utc_basic_iso_8601(pytester: pytest.Pytester) -> None:
    # arrange
    write_project(pytester, reporting={"timestamped_files": True})

    # act
    pytester.runpytest_subprocess("-p", "no:randomly")
    run_id = run_id_from(pytester.path)

    # assert - round-trips through the format exactly
    assert run_id == datetime.strptime(run_id, RUN_ID_FORMAT).strftime(RUN_ID_FORMAT)


def test_timestamped_only_writes_no_fixed_names(pytester: pytest.Pytester) -> None:
    # arrange
    write_project(
        pytester, reporting={"report_dir": "reports", "latest_files": False, "timestamped_files": True, "report_formats": ["cats_json", "junit"]}
    )
    report_dir = pytester.path / "reports"

    # act
    pytester.runpytest_subprocess("-p", "no:randomly")
    run_id = run_id_from(report_dir)

    # assert
    expected = {f"junit-{run_id}.xml", f"report-{run_id}.json"}
    assert expected == report_files_in(report_dir)


def test_both_naming_flags_off_writes_no_files_and_says_so(pytester: pytest.Pytester) -> None:
    """Distinct from enabled: false, which would also silence non-file writers."""
    # arrange
    write_project(
        pytester, reporting={"report_dir": "reports", "latest_files": False, "timestamped_files": False, "report_formats": ["cats_json", "junit"]}
    )

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly")
    _, body = cats_summary(result)

    # assert
    expected = (False, ["no report files written: reporting.latest_files and reporting.timestamped_files are both false"])
    assert expected == ((pytester.path / "reports").exists(), body)


def test_both_naming_flags_off_still_runs_every_writer_with_no_path(pytester: pytest.Pytester) -> None:
    """No file does not mean no writer: one that delivers elsewhere must still run, and its note must still print.

    The conftest replaces the cats_json writer's write() with one that records the path it was given and
    returns a summary line, so the assertion sees both through a real pytest run.
    """
    # arrange
    config_path = write_project(
        pytester,
        reporting={"report_dir": "reports", "latest_files": False, "timestamped_files": False},
        test_source="def test_ok():\n    assert True\n",
    )
    pytester.makeconftest(f'''
import os
os.environ["CATS_CONFIG"] = "{config_path}"

from cats.report_writers import cats_json

def spy_write(self, reports, output_path, run, standards_registry):
    return [f"spy: output_path={{output_path!r}}, run_id={{run['run_id']}}, tests={{len(reports)}}"]

cats_json.CatsJsonWriter.write = spy_write
''')

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly")
    header, body = cats_summary(result)
    run_id = header.removeprefix("CATS reports: run ")

    # assert
    expected = [
        "no report files written: reporting.latest_files and reporting.timestamped_files are both false",
        f"spy: output_path=None, run_id={run_id}, tests=1",
    ]
    assert expected == body


def test_relative_report_dir_resolves_against_the_cats_yaml_directory(pytester: pytest.Pytester) -> None:
    """cats.yaml lives in config/; pytest runs from the project root; the evidence follows cats.yaml."""
    # arrange
    config = make_config(accounts={"testaccount": "234567890123"})
    config["reporting"] = {"report_dir": "evidence"}
    config_path = pytester.mkdir("config") / "cats.yaml"
    config_path.write_text(yaml.dump(config))
    pytester.makeconftest(f'''
import os
os.environ["CATS_CONFIG"] = "{config_path}"
''')
    pytester.makepyfile(TEST_SOURCE)

    # act
    pytester.runpytest_subprocess("-p", "no:randomly")

    # assert
    expected = (True, False)
    assert expected == ((pytester.path / "config" / "evidence" / "report.json").exists(), (pytester.path / "evidence").exists())


def test_cli_report_dir_beats_configured_report_dir(pytester: pytest.Pytester) -> None:
    # arrange
    write_project(pytester, reporting={"report_dir": "configured", "report_formats": ["cats_json", "junit"]})

    # act
    pytester.runpytest_subprocess("-p", "no:randomly", "--cats-report-dir", "from-cli")

    # assert
    expected = ({"junit.xml", "report.json"}, False)
    assert expected == (report_files_in(pytester.path / "from-cli"), (pytester.path / "configured").exists())


def test_junit_testsuite_carries_the_run_facts(pytester: pytest.Pytester) -> None:
    """The id inside junit.xml is what ties it to report.json from the same run; the session name ties both to CloudTrail;
    the config hash says what the run was configured with."""
    # arrange
    config_path = write_project(pytester, reporting={"timestamped_files": True, "report_formats": ["cats_json", "junit"]})

    # act
    pytester.runpytest_subprocess("-p", "no:randomly")
    run_id = run_id_from(pytester.path)
    testsuite = ET.parse(pytester.path / "junit.xml").getroot().find("./testsuite")  # noqa: S314 - XML written by this test's own pytest run
    assert testsuite is not None
    suite_properties = {prop.get("name"): prop.get("value") for prop in testsuite.findall("./properties/property")}

    # assert - no cats:caller_identity_arn (no test used an account), no cats:git_* (the project is outside any checkout), no pack
    expected = {"cats:run_id": run_id, "cats:role_session_name": f"cats-{run_id}", "cats:config_sha256": config_sha256_of(config_path)}
    assert expected == suite_properties


def test_oscal_document_carries_the_run_facts(pytester: pytest.Pytester) -> None:
    # arrange
    config_path = write_project(pytester, reporting={"timestamped_files": True, "report_formats": ["cats_json", "oscal_ar"]})

    # act
    pytester.runpytest_subprocess("-p", "no:randomly")
    run_id = run_id_from(pytester.path)
    props = json.loads((pytester.path / "assessment-results.json").read_text())["assessment-results"]["metadata"]["props"]

    # assert
    expected = [
        {"name": "tool-version", "value": __version__},
        {"name": "cats-run-id", "value": run_id},
        {"name": "cats-role-session-name", "value": f"cats-{run_id}"},
        {"name": "cats-config-sha256", "value": config_sha256_of(config_path)},
    ]
    assert expected == props


def test_reporting_disabled_also_disables_junit(pytester: pytest.Pytester) -> None:
    """enabled: false is documented to disable every writer including junit."""
    # arrange
    write_project(pytester, reporting={"enabled": False, "report_formats": ["cats_json", "junit"]})

    # act
    pytester.runpytest_subprocess("-p", "no:randomly")

    # assert
    expected: set[str] = set()
    assert expected == report_files_in(pytester.path)


def test_entries_record_duration_and_recording_time(pytester: pytest.Pytester) -> None:
    # arrange
    write_project(pytester, reporting={}, test_source="def test_ok():\n    assert True\n")

    # act
    pytester.runpytest_subprocess("-p", "no:randomly")
    [entry] = json.loads((pytester.path / "report.json").read_text())["tests"]
    recorded_at = datetime.fromisoformat(entry["recorded_at"])

    # assert - a real duration, and a timezone-aware UTC timestamp
    expected = (float, True, "UTC")
    assert expected == (type(entry["duration_s"]), entry["duration_s"] >= 0, recorded_at.tzname())


def test_every_entry_has_every_key(pytester: pytest.Pytester) -> None:
    """A passing test with no metadata still carries null/empty values, never absent keys."""
    # arrange
    write_project(pytester, reporting={}, test_source="def test_ok():\n    assert True\n")

    # act
    pytester.runpytest_subprocess("-p", "no:randomly")
    [entry] = json.loads((pytester.path / "report.json").read_text())["tests"]
    del entry["duration_s"], entry["recorded_at"]

    # assert
    expected: dict[str, Any] = {
        "result": "passed",
        "accounts_in_test": [],
        "test_name": "test_ok",
        "nodeid": "test_every_entry_has_every_key.py::test_ok",
        "test_docstring": None,
        "standards": [],
        "standard_metadata": {},
        "custom_markers": {},
        "additional_metadata": {},
        "xfail": False,
        "error_message": None,
        "reason": None,
    }
    assert expected == entry


def test_run_block_describes_the_session(pytester: pytest.Pytester) -> None:
    """The run block is the provenance of the evidence: id, command line, config, outcome, outputs."""
    # arrange
    config_path = write_project(pytester, reporting={"report_dir": "reports", "timestamped_files": True, "report_formats": ["cats_json", "junit"]})
    report_dir = pytester.path / "reports"

    # act
    pytester.runpytest_subprocess("-p", "no:randomly", "-k", "test_ok")
    run_id = run_id_from(report_dir)
    run = json.loads((report_dir / "report.json").read_text())["run"]
    started_at, finished_at = datetime.fromisoformat(run.pop("started_at")), datetime.fromisoformat(run.pop("finished_at"))
    pytest_args = run.pop("pytest_args")

    # assert
    expected_counts: ResultCounts = {"total": 1, "passed": 1, "failed": 0, "error": 0, "skipped": 0, "xfailed": 0, "xpassed": 0, "not_run": 0}
    expected = {
        "run_id": run_id,
        "role_session_name": f"cats-{run_id}",
        "caller_identity": None,  # no test used an account, so no role was assumed and STS was not asked
        "test_packs": [],
        "git": None,  # the project is outside any checkout
        "config_sha256": config_sha256_of(config_path),
        "cats_version": __version__,
        "python_version": platform.python_version(),
        "pytest_version": pytest.__version__,
        "config_path": str(config_path),
        "aws_region": "us-east-1",
        "partition": "aws",
        "cfn_stack_prefix": "CATS-test-infra-",
        "exit_status": 0,
        "selection": {
            "keyword": "test_ok",
            "markexpr": "",
            "last_failed": False,
            "deselect": [],
            "explicit_args": [],
            "stopped_early": "",
            "provided": [],
            "config_excluded": [],
            "unmatched_exclusions": [],
        },
        "counts": expected_counts,
        "accounts": [],
        "outputs": [f"report-{run_id}.json", "report.json", f"junit-{run_id}.xml", "junit.xml"],
        "writers": [
            {"format": "cats_json", "file": f"report-{run_id}.json", "status": "ok", "detail": ""},
            {"format": "junit", "file": f"junit-{run_id}.xml", "status": "ok", "detail": ""},
        ],
    }
    assert expected == run
    assert (started_at <= finished_at, pytest_args[-4:]) == (True, ["-p", "no:randomly", "-k", "test_ok"])


def test_summary_names_the_run_the_counts_and_every_file(pytester: pytest.Pytester) -> None:
    # arrange
    write_project(pytester, reporting={"report_dir": "reports", "timestamped_files": True, "report_formats": ["cats_json", "junit"]})
    report_dir = pytester.path / "reports"

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly")
    run_id = run_id_from(report_dir)
    header, body = cats_summary(result)

    # assert
    expected = (
        f"CATS reports: run {run_id}",
        [
            f"2 tests (1 passed, 1 failed) written to {report_dir.resolve()}",
            f"  report-{run_id}.json, report.json",
            f"  junit-{run_id}.xml, junit.xml",
        ],
    )
    assert expected == (header, body)


def test_run_without_cacheprovider_plugin_still_prints_the_summary(pytester: pytest.Pytester) -> None:
    """-p no:cacheprovider removes the --lf option; reading the selection must not raise and the summary must still print."""
    # arrange
    write_project(pytester, reporting={})

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly", "-p", "no:cacheprovider")
    _, body = cats_summary(result)

    # assert
    expected = [f"2 tests (1 passed, 1 failed) written to {pytester.path.resolve()}", "  report.json"]
    assert expected == body


def test_usage_error_before_the_first_test_prints_no_cats_section(pytester: pytest.Pytester) -> None:
    """A run refused before any test (an unknown option here) wrote nothing, so there is nothing to tell and no file to overwrite."""
    # arrange
    write_project(pytester, reporting={})

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly", "--no-such-option")

    # assert
    expected: tuple[pytest.ExitCode, list[str], set[str]] = (pytest.ExitCode.USAGE_ERROR, [], set())
    assert expected == (result.ret, [line for line in result.stdout.lines if "CATS reports" in line], report_files_in(pytester.path))


def test_path_argument_is_recorded_and_not_judged(pytester: pytest.Pytester) -> None:
    """``pytest tests/`` is how CI runs a whole suite: the summary is the counts and the files, and run.partial does not exist.

    CATS cannot see what should have run, so it records the arguments in ``run.selection`` and makes no completeness claim.
    """
    # arrange
    write_project(pytester, reporting={})

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly", "test_path_argument_is_recorded_and_not_judged.py")
    _, body = cats_summary(result)
    run = json.loads((pytester.path / "report.json").read_text())["run"]

    # assert
    expected_body = [f"2 tests (1 passed, 1 failed) written to {pytester.path.resolve()}", "  report.json"]
    expected_selection = {
        "keyword": "",
        "markexpr": "",
        "last_failed": False,
        "deselect": [],
        "explicit_args": ["test_path_argument_is_recorded_and_not_judged.py"],
        "stopped_early": "",
        "provided": [],
        "config_excluded": [],
        "unmatched_exclusions": [],
    }
    assert (expected_body, False, expected_selection) == (body, "partial" in run, run["selection"])


def test_summary_of_a_keyword_selected_run_is_the_counts_and_the_files(pytester: pytest.Pytester) -> None:
    """pytest already prints its deselected count; the CATS section repeats nothing and judges nothing."""
    # arrange
    write_project(pytester, reporting={})

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly", "-k", "test_ok")
    _, body = cats_summary(result)

    # assert
    expected = [f"1 test (1 passed) written to {pytester.path.resolve()}", "  report.json"]
    assert expected == body


def test_summary_of_an_early_stop_is_the_counts_and_the_files(pytester: pytest.Pytester) -> None:
    """-x stops the session; pytest says so itself, and run.selection.stopped_early records it."""
    # arrange
    write_project(
        pytester, reporting={"timestamped_files": True}, test_source="def test_boom():\n    assert False\n\ndef test_ok():\n    assert True\n"
    )

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly", "-x")
    run_id = run_id_from(pytester.path)
    _, body = cats_summary(result)
    stopped_early = json.loads((pytester.path / "report.json").read_text())["run"]["selection"]["stopped_early"]

    # assert
    expected = [f"1 test (1 failed) written to {pytester.path.resolve()}", f"  report-{run_id}.json, report.json"]
    assert (expected, "stopping after 1 failures") == (body, stopped_early)


def test_run_records_each_writers_outcome_after_they_ran(pytester: pytest.Pytester) -> None:
    """report.json handed to an auditor on its own says which sibling files exist and whether each writer succeeded."""
    # arrange
    write_project(pytester, reporting={"timestamped_files": True, "report_formats": ["cats_json", "oscal_ar", "junit"]})

    # act
    pytester.runpytest_subprocess("-p", "no:randomly")
    run_id = run_id_from(pytester.path)
    report = json.loads((pytester.path / "report.json").read_text())

    # assert
    expected = [
        {"format": "cats_json", "file": f"report-{run_id}.json", "status": "ok", "detail": ""},
        {"format": "oscal_ar", "file": f"assessment-results-{run_id}.json", "status": "ok", "detail": ""},
        {"format": "junit", "file": f"junit-{run_id}.xml", "status": "ok", "detail": ""},
    ]
    assert expected == report["run"]["writers"]


def test_run_records_a_failed_writer_with_its_error(pytester: pytest.Pytester) -> None:
    """The failed sibling is named in report.json, not only in the terminal."""
    # arrange
    write_project(pytester, reporting={"report_formats": ["cats_json", "oscal_ar"]})
    pytester.makeconftest(f'''
import os
os.environ["CATS_CONFIG"] = "{pytester.path / "cats.yaml"}"

import cats.report_writers.oscal_ar


def _boom(self, reports, output_path, run, standards_registry):
    raise RuntimeError("no disk")


cats.report_writers.oscal_ar.OscalArWriter.write = _boom
''')

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly")
    report = json.loads((pytester.path / "report.json").read_text())

    # assert
    expected = (
        1,
        [
            {"format": "cats_json", "file": "report.json", "status": "ok", "detail": ""},
            {"format": "oscal_ar", "file": None, "status": "failed", "detail": "RuntimeError('no disk')"},
        ],
    )
    assert expected == (result.ret, report["run"]["writers"])


def test_files_off_records_the_file_only_writers_as_skipped(pytester: pytest.Pytester) -> None:
    # arrange
    write_project(pytester, reporting={"latest_files": False, "timestamped_files": False, "report_formats": ["cats_json", "junit"]})
    pytester.makeconftest(f'''
import os
os.environ["CATS_CONFIG"] = "{pytester.path / "cats.yaml"}"

import cats.pytest_plugin

_original = cats.pytest_plugin._run_report_writers


def _capture(formats, run, manifest, standards_registry):
    outcome = _original(formats, run, manifest, standards_registry)
    (run.report_dir / "writers.json").write_text(__import__("json").dumps(manifest["writers"]))
    return outcome


cats.pytest_plugin._run_report_writers = _capture
''')

    # act
    pytester.runpytest_subprocess("-p", "no:randomly")
    writers = json.loads((pytester.path / "writers.json").read_text())

    # assert
    expected = [
        {"format": "cats_json", "file": None, "status": "skipped", "detail": ""},
        {"format": "junit", "file": None, "status": "skipped", "detail": ""},
    ]
    assert expected == writers
