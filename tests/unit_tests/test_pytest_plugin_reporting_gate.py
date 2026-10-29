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

"""Tests for the plugin's reporting gate: evidence is written only for a CATS run that ran tests."""

import pytest
import yaml

from tests.utils import make_config

TEST_SOURCE = """
def test_anything():
    assert True
"""


def _write_config(pytester: pytest.Pytester) -> None:
    config_path = pytester.path / "cats.yaml"
    config_path.write_text(yaml.dump(make_config(accounts={"testaccount": "234567890123"})))
    pytester.makeconftest(f'''
import os
os.environ["CATS_CONFIG"] = "{config_path}"
''')


def test_project_without_cats_yaml_gets_no_report_and_no_summary(pytester: pytest.Pytester) -> None:
    """An unrelated project that merely has aws-cats installed is not a CATS run."""
    # arrange
    pytester.makeconftest("""
import os
os.environ.pop("CATS_CONFIG", None)
""")
    pytester.makepyfile(TEST_SOURCE)

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly")

    # assert
    result.assert_outcomes(passed=1)
    expected: tuple[bool, list[str]] = (False, [])
    assert expected == ((pytester.path / "report.json").exists(), [line for line in result.outlines if "CATS reports" in line])


def test_collect_only_writes_no_report(pytester: pytest.Pytester) -> None:
    """Collection runs no test, so there is no evidence to write and nothing to overwrite."""
    # arrange
    _write_config(pytester)
    pytester.makepyfile(TEST_SOURCE)

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly", "--collect-only")

    # assert
    expected: tuple[int, bool, list[str]] = (0, False, [])
    assert expected == (result.ret, (pytester.path / "report.json").exists(), [line for line in result.outlines if "CATS reports" in line])


def test_cats_run_writes_the_report(pytester: pytest.Pytester) -> None:
    # arrange
    _write_config(pytester)
    pytester.makepyfile(TEST_SOURCE)

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly")

    # assert
    result.assert_outcomes(passed=1)
    assert (pytester.path / "report.json").exists() is True


def test_writer_failure_keeps_pytests_summary_and_fails_the_run(pytester: pytest.Pytester) -> None:
    """A writer that raises at session end is a red line in the CATS section under pytest's own summary, exit 1."""
    # arrange
    _write_config(pytester)
    pytester.makeconftest(f'''
import os
os.environ["CATS_CONFIG"] = "{pytester.path / "cats.yaml"}"

import cats.pytest_plugin
from cats.exceptions import CatsError
from cats.report_writers import get_writer


class ExplodingWriter:
    file = get_writer("cats_json").file

    def write(self, reports, output_path, run, standards_registry):
        raise CatsError("Security Hub refused the import")


cats.pytest_plugin.get_writer = lambda fmt: ExplodingWriter()
''')
    pytester.makepyfile(TEST_SOURCE)

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly")

    # assert
    result.assert_outcomes(passed=1)
    expected_line = "report writing failed: Report writing failed for: cats_json: CatsError('Security Hub refused the import')"
    expected: tuple[int, list[str], list[str]] = (1, [expected_line], [])
    failure_lines = [line for line in result.outlines if line.startswith("report writing failed")]
    traceback_lines = [line for line in result.errlines if "Traceback" in line]
    assert expected == (result.ret, failure_lines, traceback_lines)
