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

"""The plugin's first-run behavior: where it looked for cats.yaml, what it tells a test that asks for a fixture it has not got,
and how it describes itself to `pytest --fixtures` and `pytest --markers`."""

from types import SimpleNamespace

import pytest
import yaml

from cats.pytest_plugin import _entries_by_nodeid, entries, pytest_sessionstart
from cats.reporting import ReportEntry
from tests.utils import make_config

NO_CATS_CONFIG_CONFTEST = """
import os
os.environ.pop("CATS_CONFIG", None)
"""


def _write_config(pytester: pytest.Pytester, accounts: dict[str, str]) -> None:
    (pytester.path / "cats.yaml").write_text(yaml.dump(make_config(accounts=accounts)))
    pytester.makeconftest(NO_CATS_CONFIG_CONFTEST)


def test_account_fixture_requested_without_a_config_names_the_path_it_looked_at(pytester: pytest.Pytester) -> None:
    """pytest's own 'fixture not found' lists every unrelated fixture; the CATS warning names where the config was expected."""
    # arrange
    pytester.makeconftest(NO_CATS_CONFIG_CONFTEST)
    pytester.makepyfile("def test_x(prod_account):\n    pass\n")

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly")

    # assert
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        [
            f"*CATS: 'prod_account' was requested but no such account fixture exists: {pytester.path / 'cats.yaml'} does not exist. "
            + "Account fixtures are named <keyname>_account; set CATS_CONFIG or run pytest from the directory holding cats.yaml.*"
        ]
    )


def test_account_fixture_requested_for_an_undeclared_account_lists_the_declared_ones(pytester: pytest.Pytester) -> None:
    # arrange
    _write_config(pytester, accounts={"dev": "234567890123", "staging": "234567890123"})
    pytester.makepyfile("def test_x(prod_account):\n    pass\n")

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly")

    # assert
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        [
            f"*CATS: 'prod_account' was requested but no such account fixture exists: {pytester.path / 'cats.yaml'} declares the accounts "
            + "dev, staging.*"
        ]
    )


def test_config_at_the_rootdir_is_found_when_pytest_runs_from_a_subdirectory(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    """`cd tests && pytest` finds the cats.yaml at pytest's rootdir."""
    # arrange
    _write_config(pytester, accounts={"prod": "234567890123"})
    (pytester.path / "pytest.ini").write_text("[pytest]\n")
    tests_dir = pytester.mkdir("tests")
    (tests_dir / "test_x.py").write_text("def test_x():\n    pass\n")
    monkeypatch.chdir(tests_dir)

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly", "--fixtures")

    # assert
    result.stdout.fnmatch_lines(
        ["prod_account [[]session scope[]] -- *", f"    AccountUnderTest for account 'prod' (234567890123), from {pytester.path / 'cats.yaml'}."]
    )


def test_generated_account_fixtures_carry_a_docstring(pytester: pytest.Pytester) -> None:
    """`pytest --fixtures` is how an agent discovers fixtures; each account fixture describes itself there."""
    # arrange
    _write_config(pytester, accounts={"prod": "234567890123"})

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly", "--fixtures")

    # assert
    result.stdout.fnmatch_lines(
        [
            "prod_account [[]session scope[]] -- *",
            f"    AccountUnderTest for account 'prod' (234567890123), from {pytester.path / 'cats.yaml'}.",
            "get_account_fixture -- *",
            "    Resolve an account fixture by name (get_account_fixture('prod_account')) and record the account in the test's evidence.",
            "cats_report -- *",
            "    The CatsReporter for the running test: add_account() and add_report_metadata() attach evidence to its report entry.",
        ]
    )


def test_cats_standard_marker_text_lists_its_keywords(pytester: pytest.Pytester) -> None:
    # arrange
    _write_config(pytester, accounts={"prod": "234567890123"})

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly", "--markers")

    # assert
    result.stdout.fnmatch_lines(
        [
            "@pytest.mark.cats_standard(name, control_ids=None, control_group=None): the standard a test attests to; "
            + "applied by @cats(standard_name=...)*"
        ]
    )


def _entry(nodeid: str) -> ReportEntry:
    return {
        "result": "passed",
        "accounts_in_test": [],
        "test_name": nodeid,
        "nodeid": nodeid,
        "test_docstring": None,
        "standards": [],
        "standard_metadata": {},
        "custom_markers": {},
        "additional_metadata": {},
        "xfail": False,
        "error_message": None,
        "reason": None,
        "duration_s": 0.0,
        "recorded_at": "2026-09-28T00:00:00.000000Z",
    }


def test_session_start_clears_evidence_left_by_an_earlier_session() -> None:
    """Two pytest.main() calls in one interpreter each report only their own entries."""
    # arrange
    entries.append(_entry("test_old.py::test_old"))
    _entries_by_nodeid["test_old.py::test_old"] = entries[0]
    session = SimpleNamespace(config=SimpleNamespace(stash=pytest.Stash()))

    # act
    pytest_sessionstart(session)  # type: ignore[arg-type]

    # assert
    assert (entries, _entries_by_nodeid) == ([], {})
