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

"""End-to-end regression tests for OSCAL output.

Cover two behaviors that need to hold for CATS to produce an
``assessment-results.json`` that a standard OSCAL consumer will accept:

* A consumer ``conftest.py`` implementing ``pytest_cats_standards`` is
  picked up by pytest's plugin manager and its registry drives the output.
* ``ImportAp.href`` serializes as an empty string rather than a URI fragment,
  so OSCAL tooling does not treat it as a dangling back-matter reference.
"""

import json
from pathlib import Path

import pytest
import yaml

from cats.oscal_models import AssessmentResults, ImportAp
from tests.utils import make_config


def _oscal_config() -> str:
    """cats.yaml content with reporting.report_formats set to oscal_ar only."""
    cfg = make_config(accounts={"testaccount": "234567890123"})
    cfg["reporting"] = {"report_formats": ["oscal_ar"]}
    return yaml.dump(cfg)


def _read_assessment_results(report_dir: Path) -> dict[str, object]:
    """Load the assessment-results.json wrapper dict from a report directory."""
    path = report_dir / "assessment-results.json"
    assert path.exists(), f"assessment-results.json not produced at {path}"
    parsed: dict[str, object] = json.loads(path.read_text())
    return parsed


def test_consumer_conftest_pytest_cats_standards_drives_control_ids(
    pytester: pytest.Pytester,
) -> None:
    """A pytest_cats_standards registry in a consumer conftest drives OSCAL finding target-ids."""
    # arrange
    config_path = pytester.path / "cats.yaml"
    config_path.write_text(_oscal_config())

    pytester.makeconftest(f'''
import os
os.environ["CATS_CONFIG"] = "{config_path}"

from cats.hookspecs import StandardMetadata


def pytest_cats_standards():
    return {{
        "ACME-G-DCP-1": StandardMetadata(control_ids=["SC-7", "AC-3"], control_group="Governance"),
    }}
''')
    pytester.makepyfile("""
from cats import cats


@cats(standard_name="ACME-G-DCP-1")
def test_mapped_standard():
    assert True
""")

    # act
    pytester.runpytest_subprocess("-v", "-p", "no:randomly", "-p", "no:xdist")

    # assert
    ar = _read_assessment_results(pytester.path)
    findings = ar["assessment-results"]["results"][0]["findings"]  # type: ignore[index]
    target_ids = sorted(finding["target"]["target-id"] for finding in findings)
    expected_target_ids = ["AC-3", "SC-7"]
    assert target_ids == expected_target_ids


def test_consumer_conftest_pytest_cats_standards_drives_control_group_prop(
    pytester: pytest.Pytester,
) -> None:
    """The registry's control_group populates the observation control-group prop."""
    # arrange
    config_path = pytester.path / "cats.yaml"
    config_path.write_text(_oscal_config())

    pytester.makeconftest(f'''
import os
os.environ["CATS_CONFIG"] = "{config_path}"

from cats.hookspecs import StandardMetadata


def pytest_cats_standards():
    return {{
        "ACME-G-DCP-1": StandardMetadata(control_ids=["SC-7"], control_group="Governance"),
    }}
''')
    pytester.makepyfile("""
from cats import cats


@cats(standard_name="ACME-G-DCP-1")
def test_mapped_standard():
    assert True
""")

    # act
    pytester.runpytest_subprocess("-v", "-p", "no:randomly", "-p", "no:xdist")

    # assert
    ar = _read_assessment_results(pytester.path)
    observation = ar["assessment-results"]["results"][0]["observations"][0]  # type: ignore[index]
    props = {p["name"]: p["value"] for p in observation["props"]}
    expected_value = "Governance"
    assert props["control-group"] == expected_value


def test_decorator_control_ids_win_over_registry_end_to_end(
    pytester: pytest.Pytester,
) -> None:
    """Decorator control_ids override the registry's at the full pytest session level."""
    # arrange
    config_path = pytester.path / "cats.yaml"
    config_path.write_text(_oscal_config())

    pytester.makeconftest(f'''
import os
os.environ["CATS_CONFIG"] = "{config_path}"

from cats.hookspecs import StandardMetadata


def pytest_cats_standards():
    return {{
        "ACME-G-DCP-1": StandardMetadata(control_ids=["AC-3"], control_group="Governance"),
    }}
''')
    pytester.makepyfile("""
from cats import cats


@cats(standard_name="ACME-G-DCP-1", control_ids=["SC-7"])
def test_decorator_override():
    assert True
""")

    # act
    pytester.runpytest_subprocess("-v", "-p", "no:randomly", "-p", "no:xdist")

    # assert
    ar = _read_assessment_results(pytester.path)
    findings = ar["assessment-results"]["results"][0]["findings"]  # type: ignore[index]
    target_ids = [finding["target"]["target-id"] for finding in findings]
    expected_target_ids = ["SC-7"]
    assert target_ids == expected_target_ids


def test_import_ap_default_href_is_empty_string() -> None:
    """ImportAp() defaults to an empty href string."""
    # act
    import_ap = ImportAp()

    # assert
    expected_href = ""
    assert import_ap.href == expected_href


def test_assessment_results_serializes_import_ap_href_as_empty_string() -> None:
    """AssessmentResults.model_dump emits import-ap with an empty href string."""
    # act
    ar = AssessmentResults()
    dumped = ar.model_dump(by_alias=True, exclude_none=True)

    # assert
    expected_import_ap = {"href": ""}
    assert dumped["import-ap"] == expected_import_ap


def test_non_mapping_registry_is_ignored_with_warning(pytester: pytest.Pytester) -> None:
    """A hook returning the wrong type must not crash reporting: warn and fall back."""
    # arrange
    config_path = pytester.path / "cats.yaml"
    config_path.write_text(_oscal_config())

    pytester.makeconftest(f'''
import os
os.environ["CATS_CONFIG"] = "{config_path}"


def pytest_cats_standards():
    return ["ACME-G-DCP-1"]
''')
    pytester.makepyfile("""
from cats import cats


@cats(standard_name="ACME-G-DCP-1")
def test_mapped_standard():
    assert True
""")

    # act
    result = pytester.runpytest_subprocess("-v", "-p", "no:randomly", "-p", "no:xdist")

    # assert: fallback control id is the standard name, and the run still passes
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(
        ["*pytest_cats_standards in conftest.py returned list, expected Mapping[[]str, StandardMetadata[]] - ignoring that registry*"]
    )
    ar = _read_assessment_results(pytester.path)
    findings = ar["assessment-results"]["results"][0]["findings"]  # type: ignore[index]
    expected_target_ids = ["ACME-G-DCP-1"]
    assert [finding["target"]["target-id"] for finding in findings] == expected_target_ids


def test_registries_from_two_conftests_are_merged(pytester: pytest.Pytester) -> None:
    """A shared organization conftest and a project conftest each contribute: every implementer's registry is merged."""
    # arrange
    config_path = pytester.path / "cats.yaml"
    config_path.write_text(_oscal_config())
    pytester.makeconftest(f'''
import os
os.environ["CATS_CONFIG"] = "{config_path}"

from cats import StandardMetadata


def pytest_cats_standards():
    return {{"ORG-1": StandardMetadata(control_ids=["org-1"])}}
''')
    tests_dir = pytester.mkdir("tests")
    (tests_dir / "conftest.py").write_text("""
from cats import StandardMetadata


def pytest_cats_standards():
    return {"PROJECT-1": StandardMetadata(control_ids=["project-1"])}
""")
    (tests_dir / "test_merged.py").write_text("""
from cats import cats


@cats(standard_name="ORG-1")
def test_org_standard():
    assert True


@cats(standard_name="PROJECT-1")
def test_project_standard():
    assert True
""")

    # act
    result = pytester.runpytest_subprocess("-v", "-p", "no:randomly", "-p", "no:xdist")

    # assert
    result.assert_outcomes(passed=2)
    ar = _read_assessment_results(pytester.path)
    findings = ar["assessment-results"]["results"][0]["findings"]  # type: ignore[index]
    expected_target_ids = ["org-1", "project-1"]
    assert sorted(finding["target"]["target-id"] for finding in findings) == expected_target_ids


def test_conflicting_registry_declarations_stop_the_run_before_any_test(pytester: pytest.Pytester) -> None:
    """Two registries disagreeing on a standard is a usage error naming both sources and values; no test runs, no report is written."""
    # arrange
    config_path = pytester.path / "cats.yaml"
    config_path.write_text(_oscal_config())
    pytester.makeconftest(f'''
import os
os.environ["CATS_CONFIG"] = "{config_path}"

from cats import StandardMetadata


def pytest_cats_standards():
    return {{"ACME-G-DCP-1": StandardMetadata(control_ids=["root-conftest"])}}
''')
    tests_dir = pytester.mkdir("tests")
    (tests_dir / "conftest.py").write_text("""
from cats import StandardMetadata


def pytest_cats_standards():
    return {"ACME-G-DCP-1": StandardMetadata(control_ids=["tests-conftest"], control_group="Governance")}
""")
    (tests_dir / "test_conflict.py").write_text("""
from cats import cats


@cats(standard_name="ACME-G-DCP-1")
def test_mapped_standard():
    assert True
""")

    # act
    result = pytester.runpytest_subprocess("-v", "-p", "no:randomly", "-p", "no:xdist")

    # assert
    result.stderr.fnmatch_lines(
        [
            "ERROR: pytest_cats_standards: 'ACME-G-DCP-1' is declared with different values by conftest.py "
            + "(control_ids=['root-conftest'], control_group=None) and tests/conftest.py "
            + "(control_ids=['tests-conftest'], control_group='Governance'); one registry must own each standard."
        ]
    )
    assert (result.ret, result.parseoutcomes(), (pytester.path / "assessment-results.json").exists()) == (4, {}, False)


def test_identical_redeclaration_across_registries_is_accepted(pytester: pytest.Pytester) -> None:
    """A project that repeats the organization's mapping verbatim is not in conflict with it."""
    # arrange
    config_path = pytester.path / "cats.yaml"
    config_path.write_text(_oscal_config())
    pytester.makeconftest(f'''
import os
os.environ["CATS_CONFIG"] = "{config_path}"

from cats import StandardMetadata


def pytest_cats_standards():
    return {{"ACME-G-DCP-1": StandardMetadata(control_ids=["ac-2.1"])}}
''')
    tests_dir = pytester.mkdir("tests")
    (tests_dir / "conftest.py").write_text("""
from cats import StandardMetadata


def pytest_cats_standards():
    return {"ACME-G-DCP-1": StandardMetadata(control_ids=["ac-2.1"])}
""")
    (tests_dir / "test_same.py").write_text("""
from cats import cats


@cats(standard_name="ACME-G-DCP-1")
def test_mapped_standard():
    assert True
""")

    # act
    result = pytester.runpytest_subprocess("-v", "-p", "no:randomly", "-p", "no:xdist")

    # assert
    result.assert_outcomes(passed=1)
    ar = _read_assessment_results(pytester.path)
    findings = ar["assessment-results"]["results"][0]["findings"]  # type: ignore[index]
    assert [finding["target"]["target-id"] for finding in findings] == ["ac-2.1"]


def test_registry_control_id_that_is_not_an_oscal_token_stops_the_run_before_any_test(pytester: pytest.Pytester) -> None:
    """With oscal_ar configured, AC-2(1) is refused before the first test runs; no evidence is spent on it."""
    # arrange
    config_path = pytester.path / "cats.yaml"
    config_path.write_text(_oscal_config())
    pytester.makeconftest(f'''
import os
os.environ["CATS_CONFIG"] = "{config_path}"

from cats import StandardMetadata


def pytest_cats_standards():
    return {{"ACME-G-DCP-1": StandardMetadata(control_ids=["AC-2(1)"])}}
''')
    pytester.makepyfile("""
from cats import cats


@cats(standard_name="ACME-G-DCP-1")
def test_mapped_standard():
    assert True
""")

    # act
    result = pytester.runpytest_subprocess("-v", "-p", "no:randomly", "-p", "no:xdist")

    # assert
    result.stderr.fnmatch_lines(
        [
            "ERROR: pytest_cats_standards registry: control id 'AC-2(1)' for standard 'ACME-G-DCP-1' is not an OSCAL token, which "
            + "reporting.report_formats oscal_ar needs for a finding target-id: it must start with a letter or '_' and contain only "
            + "letters, digits, '.', '-', '_'. Spaces and parentheses are not allowed - use the catalog convention (e.g. 'ac-2.1' for AC-2(1))."
        ]
    )
    assert (result.ret, result.parseoutcomes(), (pytester.path / "assessment-results.json").exists()) == (4, {}, False)


def test_registry_control_id_in_catalog_notation_is_accepted_without_oscal_output(pytester: pytest.Pytester) -> None:
    """Only the OSCAL writer has a grammar: with cats_json alone, AC-2(1) is carried into report.json as written."""
    # arrange
    config_path = pytester.path / "cats.yaml"
    cfg = make_config(accounts={"testaccount": "234567890123"})
    cfg["reporting"] = {"report_formats": ["cats_json"]}
    config_path.write_text(yaml.dump(cfg))
    pytester.makeconftest(f'''
import os
os.environ["CATS_CONFIG"] = "{config_path}"

from cats import StandardMetadata


def pytest_cats_standards():
    return {{"ACME-G-DCP-1": StandardMetadata(control_ids=["AC-2(1)", "1.1.1"])}}
''')
    pytester.makepyfile("""
from cats import cats


@cats(standard_name="ACME-G-DCP-1")
def test_mapped_standard():
    assert True
""")

    # act
    result = pytester.runpytest_subprocess("-v", "-p", "no:randomly", "-p", "no:xdist")

    # assert
    result.assert_outcomes(passed=1)
    report = json.loads((pytester.path / "report.json").read_text())
    assert report["tests"][0]["standards"] == ["ACME-G-DCP-1"]
