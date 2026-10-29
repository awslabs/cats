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

"""A ``pytest_cats_standards`` conftest that only collection loads, under pytest-xdist.

The controller resolves the registry and writes the reports, but under xdist it never
collects, so a conftest pytest first finds during collection (``tests/deep/conftest.py``
on ``pytest tests``) is loaded by the workers only. Its registry never reaches the
reports; the run must say so rather than write the standard name as the OSCAL target-id.
A serial run collects on the process that writes the reports, so it is unaffected.
"""

import json

import pytest
import yaml

from tests.utils import make_config

LATE_WARNING = (
    "*tests/deep/conftest.py implements pytest_cats_standards but was loaded during collection, which the pytest-xdist "
    + "controller does not run, so its registry is not in the reports.*"
)


def write_project(pytester: pytest.Pytester, registry_conftest: str) -> None:
    """A project whose only standards registry is the conftest at ``registry_conftest`` (relative to the project)."""
    config = make_config(accounts={"testaccount": "234567890123"})
    config["reporting"] = {"report_formats": ["oscal_ar"]}
    config_path = pytester.path / "cats.yaml"
    config_path.write_text(yaml.dump(config))
    pytester.makeconftest(f'''
import os
os.environ["CATS_CONFIG"] = "{config_path}"
''')
    registry_path = pytester.path / registry_conftest
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text("""
from cats import StandardMetadata


def pytest_cats_standards():
    return {"STD-1": StandardMetadata(control_ids=["AC-3"])}
""")
    (registry_path.parent / "test_mapped.py").write_text("""
from cats import cats


@cats(standard_name="STD-1")
def test_one():
    assert True


@cats(standard_name="STD-1")
def test_two():
    assert True
""")


def read_target_ids(pytester: pytest.Pytester) -> list[str]:
    results = json.loads((pytester.path / "assessment-results.json").read_text())["assessment-results"]["results"]
    return sorted(finding["target"]["target-id"] for finding in results[0]["findings"])


def test_xdist_run_warns_that_a_collection_time_registry_is_not_in_the_reports(pytester: pytest.Pytester) -> None:
    # arrange
    write_project(pytester, "tests/deep/conftest.py")

    # act
    result = pytester.runpytest_subprocess("tests", "-n", "2", "-p", "no:randomly", "-W", "default")

    # assert
    result.assert_outcomes(passed=2)
    result.stdout.fnmatch_lines([LATE_WARNING])


def test_serial_run_uses_a_collection_time_registry_without_warning(pytester: pytest.Pytester) -> None:
    # arrange
    write_project(pytester, "tests/deep/conftest.py")

    # act
    result = pytester.runpytest_subprocess("tests", "-p", "no:randomly", "-p", "no:xdist", "-W", "default")

    # assert
    result.stdout.no_fnmatch_line("*implements pytest_cats_standards but was loaded during collection*")
    assert read_target_ids(pytester) == ["AC-3", "AC-3"]


def test_xdist_run_uses_an_initial_conftest_registry_without_warning(pytester: pytest.Pytester) -> None:
    """``tests/conftest.py`` on ``pytest tests`` is loaded before configure, on the controller too."""
    # arrange
    write_project(pytester, "tests/conftest.py")

    # act
    result = pytester.runpytest_subprocess("tests", "-n", "2", "-p", "no:randomly", "-W", "default")

    # assert
    result.stdout.no_fnmatch_line("*implements pytest_cats_standards but was loaded during collection*")
    assert read_target_ids(pytester) == ["AC-3", "AC-3"]
