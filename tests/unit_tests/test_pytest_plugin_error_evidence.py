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

"""Compliance evidence must record every test pytest reports, with the outcome pytest reports.

Regression tests: errored tests once vanished from report.json, a teardown error was
recorded as ``passed``, strict-xfail XPASS was recorded as ``xfailed``, a directly applied
custom marker raised ``KeyError`` and dropped the entry, and a non-JSON metadata value
truncated the whole evidence file at session end.
"""

import json
from typing import Any

import pytest
import yaml

from tests.utils import make_config


@pytest.fixture
def cats_config_content() -> str:
    """Minimal cats.yaml config as string."""
    return yaml.dump(make_config(accounts={"testaccount": "234567890123"}))


def write_project(pytester: pytest.Pytester, cats_config_content: str, test_source: str) -> None:
    """Write cats.yaml, a conftest pointing CATS_CONFIG at it, and the test file."""
    config_path = pytester.path / "cats.yaml"
    config_path.write_text(cats_config_content)
    pytester.makeconftest(f'''
import os
os.environ["CATS_CONFIG"] = "{config_path}"
''')
    pytester.makepyfile(test_source)


def read_report(pytester: pytest.Pytester) -> list[dict[str, Any]]:
    """The recorded entries without ``duration_s`` and ``recorded_at``.

    Those two vary run to run, so they are asserted on their own in
    test_pytest_plugin_report_files.py and stripped here to keep exact-entry assertions.
    """
    entries: list[dict[str, Any]] = json.loads((pytester.path / "report.json").read_text())["tests"]
    for entry in entries:
        del entry["duration_s"]
        del entry["recorded_at"]
    return entries


def test_setup_error_is_recorded_as_error(pytester: pytest.Pytester, cats_config_content: str) -> None:
    """A fixture error must appear in the evidence, not vanish."""
    # arrange
    write_project(
        pytester,
        cats_config_content,
        """
import pytest

@pytest.fixture
def broken_fixture():
    raise RuntimeError("fixture exploded")

def test_needs_broken_fixture(broken_fixture):
    assert True
""",
    )

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly")

    # assert
    result.assert_outcomes(errors=1)
    expected: list[dict[str, Any]] = [
        {
            "result": "error",
            "accounts_in_test": [],
            "test_name": "test_needs_broken_fixture",
            "nodeid": "test_setup_error_is_recorded_as_error.py::test_needs_broken_fixture",
            "test_docstring": None,
            "standards": [],
            "standard_metadata": {},
            "custom_markers": {},
            "additional_metadata": {},
            "xfail": False,
            "error_message": "RuntimeError: fixture exploded",
            "reason": None,
        }
    ]
    assert expected == read_report(pytester)


def test_teardown_error_upgrades_passed_entry_to_error(pytester: pytest.Pytester, cats_config_content: str) -> None:
    """pytest reports this test as ERROR; the evidence must not say passed."""
    # arrange
    write_project(
        pytester,
        cats_config_content,
        """
import pytest

@pytest.fixture
def broken_teardown():
    yield "value"
    raise RuntimeError("teardown exploded")

def test_passes_then_teardown_errors(broken_teardown):
    assert True
""",
    )

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly")

    # assert
    result.assert_outcomes(passed=1, errors=1)
    expected: list[dict[str, Any]] = [
        {
            "result": "error",
            "accounts_in_test": [],
            "test_name": "test_passes_then_teardown_errors",
            "nodeid": "test_teardown_error_upgrades_passed_entry_to_error.py::test_passes_then_teardown_errors",
            "test_docstring": None,
            "standards": [],
            "standard_metadata": {},
            "custom_markers": {},
            "additional_metadata": {},
            "xfail": False,
            "error_message": "RuntimeError: teardown exploded",
            "reason": None,
        }
    ]
    assert expected == read_report(pytester)


def test_strict_xfail_xpass_is_recorded_as_failed(pytester: pytest.Pytester, cats_config_content: str) -> None:
    """pytest fails a strict-xfail test that passes; recording it xfailed misstates the evidence."""
    # arrange
    write_project(
        pytester,
        cats_config_content,
        """
import pytest

@pytest.mark.xfail(strict=True, reason="expected to fail")
def test_strict_xpass():
    assert True
""",
    )

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly")

    # assert
    result.assert_outcomes(failed=1)
    expected: list[dict[str, Any]] = [
        {
            "result": "failed",
            "accounts_in_test": [],
            "test_name": "test_strict_xpass",
            "nodeid": "test_strict_xfail_xpass_is_recorded_as_failed.py::test_strict_xpass",
            "test_docstring": None,
            "standards": [],
            "standard_metadata": {},
            "custom_markers": {},
            "additional_metadata": {},
            "xfail": True,
            "error_message": "[XPASS(strict)] expected to fail",
            "reason": None,
        }
    ]
    assert expected == read_report(pytester)


def test_xfail_raises_mismatch_is_recorded_as_failed(pytester: pytest.Pytester, cats_config_content: str) -> None:
    """A different exception than xfail(raises=...) declares is a failure in pytest's summary."""
    # arrange
    write_project(
        pytester,
        cats_config_content,
        """
import pytest

@pytest.mark.xfail(raises=ValueError)
def test_wrong_exception():
    raise RuntimeError("not a ValueError")
""",
    )

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly")

    # assert
    result.assert_outcomes(failed=1)
    entries = read_report(pytester)
    expected = ("failed", "RuntimeError: not a ValueError")
    assert expected == (entries[0]["result"], entries[0]["error_message"])


def test_imperative_xfail_is_recorded_as_xfailed(pytester: pytest.Pytester, cats_config_content: str) -> None:
    """pytest.xfail() carries wasxfail but no marker; it must still record as xfailed."""
    # arrange
    write_project(
        pytester,
        cats_config_content,
        """
import pytest

def test_imperative_xfail():
    pytest.xfail("known broken")
""",
    )

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly")

    # assert
    result.assert_outcomes(xfailed=1)
    entries = read_report(pytester)
    expected = ("xfailed", True)
    assert expected == (entries[0]["result"], entries[0]["xfail"])


def test_directly_applied_custom_marker_is_recorded(pytester: pytest.Pytester) -> None:
    """@pytest.mark.team("payments") once raised KeyError('kwarg') and dropped the entry."""
    # arrange
    config = make_config(accounts={"testaccount": "234567890123"})
    config["reporting"] = {"custom_markers": ["team"]}
    write_project(
        pytester,
        yaml.dump(config),
        """
import pytest

@pytest.mark.team("payments")
def test_with_direct_marker():
    assert True
""",
    )

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly")

    # assert
    result.assert_outcomes(passed=1)
    entries = read_report(pytester)
    expected = ("passed", {"team": ["payments"]})
    assert expected == (entries[0]["result"], entries[0]["custom_markers"])


def test_custom_marker_with_multiple_positional_values_records_each(pytester: pytest.Pytester) -> None:
    """@pytest.mark.team("a", "b") records every positional value, in order."""
    # arrange
    config = make_config(accounts={"testaccount": "234567890123"})
    config["reporting"] = {"custom_markers": ["team"]}
    write_project(
        pytester,
        yaml.dump(config),
        """
import pytest

@pytest.mark.team("payments", "identity")
def test_with_two_values():
    assert True
""",
    )

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly")

    # assert
    result.assert_outcomes(passed=1)
    entries = read_report(pytester)
    expected = {"team": ["payments", "identity"]}
    assert expected == entries[0]["custom_markers"]


def test_unserializable_additional_metadata_fails_the_test_loudly(pytester: pytest.Pytester, cats_config_content: str) -> None:
    """A datetime in metadata once truncated report.json at session end. It must instead
    fail the offending test with the culprit named, leaving the evidence file whole."""
    # arrange
    write_project(
        pytester,
        cats_config_content,
        """
import datetime

def test_bad_metadata(cats_report):
    cats_report.add_report_metadata({"when": datetime.datetime(2026, 1, 1)})
""",
    )

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly")

    # assert
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*additional_metadata value for key 'when' is not JSON-serializable (type datetime)*"])
    entries = read_report(pytester)
    expected = ("failed", 1)
    assert expected == (entries[0]["result"], len(entries))


def test_class_level_cats_marker_records_standards(pytester: pytest.Pytester, cats_config_content: str) -> None:
    """@cats on a test class once yielded standards: [] (own_markers misses class markers)."""
    # arrange
    write_project(
        pytester,
        cats_config_content,
        """
from cats import cats

@cats(standard_name="SOC2-CC6.6")
class TestGroup:
    def test_in_class(self):
        assert True
""",
    )

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly")

    # assert
    result.assert_outcomes(passed=1)
    entries = read_report(pytester)
    expected = ["SOC2-CC6.6"]
    assert entries[0]["standards"] == expected


def test_foreign_standard_marker_is_not_cats_evidence(pytester: pytest.Pytester, cats_config_content: str) -> None:
    """CATS reads `cats_standard` only: a user's or another plugin's `standard` marker is not a standard attestation."""
    # arrange
    write_project(
        pytester,
        cats_config_content,
        """
import pytest

@pytest.mark.standard(name="SOC2-CC6.6")
def test_with_foreign_marker():
    assert True
""",
    )

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly")

    # assert
    result.assert_outcomes(passed=1)
    entries = read_report(pytester)
    expected: list[str] = []
    assert entries[0]["standards"] == expected


def test_skip_reason_is_recorded(pytester: pytest.Pytester, cats_config_content: str) -> None:
    # arrange
    write_project(
        pytester,
        cats_config_content,
        """
import pytest

@pytest.mark.skip(reason="account not deployed")
def test_skipped():
    assert True
""",
    )

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly")

    # assert
    result.assert_outcomes(skipped=1)
    entries = read_report(pytester)
    expected = ("skipped", "account not deployed")
    assert expected == (entries[0]["result"], entries[0]["reason"])


def test_xfail_reason_is_recorded(pytester: pytest.Pytester, cats_config_content: str) -> None:
    # arrange
    write_project(
        pytester,
        cats_config_content,
        """
import pytest

@pytest.mark.xfail(reason="known broken")
def test_expected_failure():
    assert False
""",
    )

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly")

    # assert
    result.assert_outcomes(xfailed=1)
    entries = read_report(pytester)
    expected = ("xfailed", "known broken")
    assert expected == (entries[0]["result"], entries[0]["reason"])


def test_stacked_duplicate_standard_records_once(pytester: pytest.Pytester, cats_config_content: str) -> None:
    """Stacking the same standard_name twice once duplicated report entries and props."""
    # arrange
    write_project(
        pytester,
        cats_config_content,
        """
from cats import cats

@cats(standard_name="SOC2-CC6.6", control_ids=["AC-3"])
@cats(standard_name="SOC2-CC6.6", control_ids=["SC-7"])
def test_stacked():
    assert True
""",
    )

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly")

    # assert
    result.assert_outcomes(passed=1)
    entries = read_report(pytester)
    expected = ["SOC2-CC6.6"]
    assert entries[0]["standards"] == expected


def test_report_dir_is_created_when_missing(pytester: pytest.Pytester, cats_config_content: str) -> None:
    """--cats-report-dir pointing at a missing nested directory once raised FileNotFoundError."""
    # arrange
    write_project(
        pytester,
        cats_config_content,
        """
def test_ok():
    assert True
""",
    )

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly", "--cats-report-dir", "out/nested")

    # assert
    result.assert_outcomes(passed=1)
    entries = json.loads((pytester.path / "out" / "nested" / "report.json").read_text())["tests"]
    assert entries[0]["result"] == "passed"


def test_setup_phase_skip_still_records_the_requested_account(pytester: pytest.Pytester, cats_config_content: str) -> None:
    """A skipped test never built its fixtures; the account comes from the fixture name and cats.yaml."""
    # arrange
    write_project(
        pytester,
        cats_config_content,
        """
import pytest

@pytest.mark.skip(reason="account not deployed")
def test_skipped(testaccount_account):
    assert True
""",
    )

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly")

    # assert
    result.assert_outcomes(skipped=1)
    entries = read_report(pytester)
    expected = [{"account_name": "testaccount", "account_id": "234567890123", "tags": {}}]
    assert expected == entries[0]["accounts_in_test"]
