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

"""Config-driven test selection through real pytest runs: ``test_selection`` in cats.yaml, ``@cats_requires`` on tests,
the ``pytest_cats_provides`` hook, and ``not_run`` in every report file.

Each test runs a pytest subprocess over a small project and reads the report files it wrote.
"""

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
import yaml

from cats.report_run import ResultCounts
from tests.utils import cats_summary, make_config

TEST_SOURCE = """
from cats import cats, cats_requires


def test_base():
    '''Always runs.'''


@cats(standard_name="ACME-G-SCP-34")
def test_rds_control(testaccount_account):
    '''Excluded by standard.'''


@cats(standard_name="ACME-X-1", mutating=True)
def test_writes_something():
    '''Excluded by a boolean marker.'''


@cats(standard_name="ACME-X-2", mutating="true")
def test_string_true_is_not_bool():
    '''A string "true" is not the boolean true.'''


@cats_requires("container-deployment")
@cats(standard_name="ACME-C-DEP-1")
def test_container_only():
    '''Runs when container-deployment is provided.'''


@cats_requires("uc-plus", "container-deployment")
@cats(standard_name="ACME-UC-1", control_ids=["SC-7"])
def test_uc_plus_container():
    '''Held back for want of uc-plus.'''
"""

# For the no-block case: nothing here asks for an account, so nothing can reach AWS when it runs.
BASE_SOURCE = """
from cats import cats_requires


def test_base():
    '''Always runs.'''


@cats_requires("container-deployment")
def test_container_only():
    '''Needs a provider.'''
"""


TEST_SELECTION: dict[str, Any] = {
    "provides": ["container-deployment"],
    "excluded": [
        {"marker": "standard_name", "values": ["ACME-G-SCP-34"], "reason": "RDS not in use; accepted by CISO 2026-09-01"},
        {"marker": "mutating", "values": [True], "reason": "Change freeze; read-only run until 2026-10-20"},
        {"marker": "team", "values": ["nobody"], "reason": "matches no test"},
    ],
}

# provides nothing, excludes as the full block does: the account-fixture test stays held back.
PROVIDES_NOTHING: dict[str, Any] = {"provides": [], "excluded": TEST_SELECTION["excluded"]}


def write_project(
    pytester: pytest.Pytester,
    test_selection: dict[str, Any] | None = TEST_SELECTION,
    conftest_body: str = "",
    test_source: str = TEST_SOURCE,
    report_formats: list[str] | None = None,
) -> Path:
    """cats.yaml with the selection block, a conftest pointing CATS_CONFIG at it (plus ``conftest_body``), and the test file."""
    config = make_config(
        accounts={"testaccount": "234567890123"},
        reporting={"report_formats": report_formats or ["cats_json", "oscal_ar", "junit"], "custom_markers": ["mutating", "team"]},
    )
    if test_selection is not None:
        config["test_selection"] = test_selection
    config_path = pytester.path / "cats.yaml"
    config_path.write_text(yaml.dump(config))
    pytester.makeconftest(f'''
import os
os.environ["CATS_CONFIG"] = "{config_path}"
os.environ["GIT_CEILING_DIRECTORIES"] = "{pytester.path.parent}"
{conftest_body}
''')
    pytester.makepyfile(test_source)
    return config_path


def run(pytester: pytest.Pytester, *args: str) -> pytest.RunResult:
    return pytester.runpytest_subprocess("-p", "no:randomly", "-p", "no:cacheprovider", *args)


def report(pytester: pytest.Pytester) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((pytester.path / "report.json").read_text())
    return loaded


def results_by_name(document: dict[str, Any]) -> dict[str, tuple[str, str | None]]:
    """``{test_name: (result, reason)}`` for every entry."""
    return {entry["test_name"]: (entry["result"], entry["reason"]) for entry in document["tests"]}


def test_held_back_tests_are_not_run_entries_and_the_run_is_complete(pytester: pytest.Pytester) -> None:
    """The acceptance case in full: three ways to be held back, each recorded with its reason; the run is judged by nothing."""
    # arrange
    write_project(pytester)

    # act
    result = run(pytester)
    document = report(pytester)

    # assert
    result.assert_outcomes(passed=3, deselected=3)
    expected_results = {
        "test_base": ("passed", None),
        "test_container_only": ("passed", None),
        "test_string_true_is_not_bool": ("passed", None),
        "test_rds_control": ("not_run", "RDS not in use; accepted by CISO 2026-09-01"),
        "test_writes_something": ("not_run", "Change freeze; read-only run until 2026-10-20"),
        "test_uc_plus_container": ("not_run", "requires {container-deployment, uc-plus}; not provided {uc-plus}"),
    }
    expected_counts: ResultCounts = {"total": 6, "passed": 3, "failed": 0, "error": 0, "skipped": 0, "xfailed": 0, "xpassed": 0, "not_run": 3}
    assert (expected_results, False, expected_counts) == (results_by_name(document), "partial" in document["run"], document["run"]["counts"])


def test_run_selection_records_the_scope_each_exclusion_and_the_unmatched_entry(pytester: pytest.Pytester) -> None:
    # arrange
    write_project(pytester)

    # act
    run(pytester)
    selection = report(pytester)["run"]["selection"]

    # assert
    expected = {
        "keyword": "",
        "markexpr": "",
        "last_failed": False,
        "deselect": [],
        "explicit_args": [],
        "stopped_early": "",
        "provided": ["container-deployment"],
        "config_excluded": [
            {
                "nodeid": "test_run_selection_records_the_scope_each_exclusion_and_the_unmatched_entry.py::test_rds_control",
                "standards": ["ACME-G-SCP-34"],
                "requires": [],
                "kind": "excluded",
                "excluded_index": 0,
                "marker": "standard_name",
                "value": "ACME-G-SCP-34",
                "not_provided": [],
                "reason": "RDS not in use; accepted by CISO 2026-09-01",
            },
            {
                "nodeid": "test_run_selection_records_the_scope_each_exclusion_and_the_unmatched_entry.py::test_uc_plus_container",
                "standards": ["ACME-UC-1"],
                "requires": ["container-deployment", "uc-plus"],
                "kind": "not_provided",
                "excluded_index": None,
                "marker": None,
                "value": None,
                "not_provided": ["uc-plus"],
                "reason": "requires {container-deployment, uc-plus}; not provided {uc-plus}",
            },
            {
                "nodeid": "test_run_selection_records_the_scope_each_exclusion_and_the_unmatched_entry.py::test_writes_something",
                "standards": ["ACME-X-1"],
                "requires": [],
                "kind": "excluded",
                "excluded_index": 1,
                "marker": "mutating",
                "value": True,
                "not_provided": [],
                "reason": "Change freeze; read-only run until 2026-10-20",
            },
        ],
        "unmatched_exclusions": [{"index": 2, "marker": "team", "values": ["nobody"], "reason": "matches no test"}],
    }
    assert expected == selection


def test_not_run_entry_carries_its_evidence_from_collection(pytester: pytest.Pytester) -> None:
    """Accounts from the fixtures it asks for, standards and metadata from its marks, zero duration."""
    # arrange
    write_project(pytester)

    # act
    run(pytester)
    [entry] = [entry for entry in report(pytester)["tests"] if entry["test_name"] == "test_rds_control"]
    entry.pop("recorded_at")

    # assert
    expected = {
        "result": "not_run",
        "accounts_in_test": [{"account_name": "testaccount", "account_id": "234567890123", "tags": {}}],
        "test_name": "test_rds_control",
        "nodeid": "test_not_run_entry_carries_its_evidence_from_collection.py::test_rds_control",
        "test_docstring": "Excluded by standard.",
        "standards": ["ACME-G-SCP-34"],
        "standard_metadata": {"ACME-G-SCP-34": {"control_ids": [], "control_group": None}},
        "custom_markers": {},
        "additional_metadata": {},
        "xfail": False,
        "error_message": None,
        "reason": "RDS not in use; accepted by CISO 2026-09-01",
        "duration_s": 0.0,
    }
    assert expected == entry


def test_terminal_summary_names_the_scope_the_counts_and_the_unmatched_entry(pytester: pytest.Pytester) -> None:
    # arrange
    write_project(pytester)

    # act
    result = run(pytester)

    # assert
    result.stdout.fnmatch_lines(
        [
            "6 tests (3 passed, 3 not_run) written to *",
            "test_selection: provides container-deployment; 3 tests not run (2 excluded, 1 not provided), recorded as not_run with their reasons",
            "test_selection.excluded[2] (team: nobody) matched 0 tests",
        ]
    )


def test_junit_has_a_skipped_testcase_for_every_not_run_test(pytester: pytest.Pytester) -> None:
    """The format has no closer state: a testcase with the CATS properties and <skipped type="cats.not_run" message="not run: ...">."""
    # arrange
    write_project(pytester)

    # act
    run(pytester)
    suite = ET.parse(pytester.path / "junit.xml").getroot().find("testsuite")  # noqa: S314 - XML written by this test's own pytest run
    assert suite is not None
    not_run = {
        case.get("name"): (
            case.find("skipped").get("type"),  # type: ignore[union-attr]
            case.find("skipped").get("message"),  # type: ignore[union-attr]
            case.get("file"),
            case.get("time"),
            [(prop.get("name"), prop.get("value")) for prop in case.iter("property")],
        )
        for case in suite.findall("testcase")
        if case.find("skipped") is not None
    }

    # assert
    expected_counts = ("6", "3", "0", "0")
    expected_not_run = {
        "test_rds_control": (
            "cats.not_run",
            "not run: RDS not in use; accepted by CISO 2026-09-01",
            "test_junit_has_a_skipped_testcase_for_every_not_run_test.py",
            "0.000",
            [("cats:standard", "ACME-G-SCP-34"), ("cats:account", "testaccount (234567890123)"), ("cats:description", "Excluded by standard.")],
        ),
        "test_writes_something": (
            "cats.not_run",
            "not run: Change freeze; read-only run until 2026-10-20",
            "test_junit_has_a_skipped_testcase_for_every_not_run_test.py",
            "0.000",
            [("cats:standard", "ACME-X-1"), ("cats:description", "Excluded by a boolean marker."), ("mutating", "true")],
        ),
        "test_uc_plus_container": (
            "cats.not_run",
            "not run: requires {container-deployment, uc-plus}; not provided {uc-plus}",
            "test_junit_has_a_skipped_testcase_for_every_not_run_test.py",
            "0.000",
            [("cats:standard", "ACME-UC-1"), ("cats:description", "Held back for want of uc-plus.")],
        ),
    }
    assert (expected_counts, expected_not_run) == ((suite.get("tests"), suite.get("skipped"), suite.get("failures"), suite.get("errors")), not_run)


def test_oscal_has_an_observation_and_no_finding_for_a_not_run_test(pytester: pytest.Pytester) -> None:
    # arrange
    write_project(pytester)

    # act
    run(pytester)
    result = json.loads((pytester.path / "assessment-results.json").read_text())["assessment-results"]["results"][0]
    observations = {
        observation["title"]: (next(prop["value"] for prop in observation["props"] if prop["name"] == "result"), observation.get("remarks"))
        for observation in result["observations"]
    }

    # assert
    expected_observations = {
        "test_base": ("Pass", None),
        "test_container_only": ("Pass", "ACME-C-DEP-1"),
        "test_string_true_is_not_bool": ("Pass", "ACME-X-2"),
        "test_rds_control": ("Not run", "RDS not in use; accepted by CISO 2026-09-01"),
        "test_writes_something": ("Not run", "Change freeze; read-only run until 2026-10-20"),
        "test_uc_plus_container": ("Not run", "requires {container-deployment, uc-plus}; not provided {uc-plus}"),
    }
    expected_findings = ["ACME-C-DEP-1", "ACME-X-2"]
    assert (expected_observations, expected_findings) == (observations, [finding["target"]["target-id"] for finding in result["findings"]])


def test_marker_expression_on_cats_requires_still_works_and_is_recorded(pytester: pytest.Pytester) -> None:
    """``-m "cats_requires(name='x')"`` is pytest's own selection: it runs, and the run records it as ``markexpr`` like any -m."""
    # arrange
    write_project(pytester)

    # act
    result = run(pytester, "-m", "cats_requires(name='container-deployment')")
    document = report(pytester)

    # assert
    result.assert_outcomes(passed=1, deselected=5)
    expected = (
        "cats_requires(name='container-deployment')",
        {
            "test_container_only": ("passed", None),
            "test_uc_plus_container": ("not_run", "requires {container-deployment, uc-plus}; not provided {uc-plus}"),
        },
    )
    assert expected == (document["run"]["selection"]["markexpr"], results_by_name(document))


def test_keyword_selection_and_held_back_tests_share_pytest_deselected_count(pytester: pytest.Pytester) -> None:
    """-k deselected 4 and test_selection held back 1 of the survivors: pytest counts 5 deselected, CATS records 1 not_run and says nothing of -k.

    Every ``excluded`` entry matched nothing because -k removed its tests first: reported, since the user cannot otherwise tell.
    """
    # arrange
    write_project(pytester)

    # act
    result = run(pytester, "-k", "container")
    _, body = cats_summary(result)
    document = report(pytester)

    # assert
    result.assert_outcomes(passed=1, deselected=5)
    expected_body = [
        f"2 tests (1 passed, 1 not_run) written to {pytester.path.resolve()}",
        "  report.json",
        "  assessment-results.json",
        "  junit.xml",
        "test_selection: provides container-deployment; 1 test not run (0 excluded, 1 not provided), recorded as not_run with their reasons",
        "test_selection.excluded[0] (standard_name: ACME-G-SCP-34) matched 0 tests",
        "test_selection.excluded[1] (mutating: true) matched 0 tests",
        "test_selection.excluded[2] (team: nobody) matched 0 tests",
    ]
    assert (expected_body, "container") == (body, document["run"]["selection"]["keyword"])


def test_hook_output_is_unioned_with_provides(pytester: pytest.Pytester) -> None:
    """The pack provides uc-plus from its conftest; with container-deployment in the config, every cats_requires test runs."""
    # arrange
    write_project(
        pytester,
        conftest_body="""
from cats.config import CatsConfig

def pytest_cats_provides(cats_config: CatsConfig) -> set[str]:
    assert "container-deployment" in cats_config.test_selection.provides
    return {"uc-plus"}
""",
    )

    # act
    result = run(pytester)
    document = report(pytester)

    # assert
    result.assert_outcomes(passed=4, deselected=2)
    expected = (["container-deployment", "uc-plus"], ("passed", None))
    assert expected == (document["run"]["selection"]["provided"], results_by_name(document)["test_uc_plus_container"])


def test_hook_without_the_parameter_is_called_without_it(pytester: pytest.Pytester) -> None:
    # arrange
    write_project(
        pytester,
        conftest_body="""
def pytest_cats_provides():
    return ["uc-plus"]
""",
    )

    # act
    run(pytester)

    # assert
    assert report(pytester)["run"]["selection"]["provided"] == ["container-deployment", "uc-plus"]


def test_raising_hook_warns_naming_the_conftest_and_provides_nothing(pytester: pytest.Pytester) -> None:
    """The run goes on without the hook's requirements, visibly: the warning names the file and the tests it affected are not_run."""
    # arrange
    write_project(
        pytester,
        test_selection=PROVIDES_NOTHING,
        conftest_body="""
def pytest_cats_provides(cats_config):
    raise RuntimeError("workbook missing")
""",
    )

    # act
    result = run(pytester, "-W", "default")
    document = report(pytester)

    # assert
    result.assert_outcomes(passed=2, deselected=4)
    result.stdout.fnmatch_lines(["*pytest_cats_provides in conftest.py raised RuntimeError('workbook missing') - it provides nothing*"])
    expected: tuple[list[str], tuple[str, str]] = ([], ("not_run", "requires {container-deployment}; not provided {container-deployment}"))
    assert expected == (document["run"]["selection"]["provided"], results_by_name(document)["test_container_only"])


def test_wrong_return_type_warns_and_provides_nothing(pytester: pytest.Pytester) -> None:
    # arrange
    write_project(
        pytester,
        test_selection=PROVIDES_NOTHING,
        conftest_body="""
def pytest_cats_provides(cats_config):
    return "container-deployment"
""",
    )

    # act
    result = run(pytester, "-W", "default")

    # assert
    result.assert_outcomes(passed=2, deselected=4)
    result.stdout.fnmatch_lines(
        [
            "*pytest_cats_provides in conftest.py returned 'container-deployment', "
            + "expected a set, list or tuple of non-empty strings - it provides nothing*"
        ]
    )


def test_no_test_selection_block_runs_every_test_that_needs_nothing(pytester: pytest.Pytester) -> None:
    """Without the block, a base test runs and a cats_requires test is not_run: there is nothing to provide it."""
    # arrange
    write_project(pytester, test_selection=None, test_source=BASE_SOURCE)

    # act
    result = run(pytester)
    document = report(pytester)

    # assert
    result.assert_outcomes(passed=1, deselected=1)
    expected = ("not_run", "requires {container-deployment}; not provided {container-deployment}")
    assert expected == results_by_name(document)["test_container_only"]
    result.stdout.fnmatch_lines(
        ["test_selection: provides nothing; 1 test not run (0 excluded, 1 not provided), recorded as not_run with their reasons"]
    )


def test_xdist_workers_yield_one_record_per_held_back_test(pytester: pytest.Pytester) -> None:
    """Every worker collects the whole suite and holds the same tests back; the controller merges them by nodeid."""
    # arrange
    write_project(pytester)

    # act
    result = run(pytester, "-n", "2")
    document = report(pytester)

    # assert
    result.assert_outcomes(passed=3)
    expected = (3, 3, ["container-deployment"], False)
    assert expected == (
        document["run"]["counts"]["not_run"],
        len(document["run"]["selection"]["config_excluded"]),
        document["run"]["selection"]["provided"],
        "partial" in document["run"],
    )


def test_collect_only_applies_the_selection(pytester: pytest.Pytester) -> None:
    """``cats test --collect-only`` shows what would run; no report is written."""
    # arrange
    write_project(pytester)

    # act
    result = run(pytester, "--collect-only", "-q")

    # assert
    result.stdout.fnmatch_lines(["3/6 tests collected (3 deselected)*"])
    assert (pytester.path / "report.json").exists() is False
