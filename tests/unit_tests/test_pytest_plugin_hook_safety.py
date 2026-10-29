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

"""Tests for pytest_runtest_makereport hook exception safety.

Verifies that exceptions in post-yield hook code don't silently swallow test results.
Without the guard, CATS can swallow tests entirely on library failure.
"""

import pytest
import yaml

from tests.utils import make_config


@pytest.fixture
def cats_config_content() -> str:
    """Minimal cats.yaml config as string."""
    return yaml.dump(make_config(accounts={"testaccount": "234567890123"}))


def test_hook_exception_reports_failure_not_swallowed(
    pytester: pytest.Pytester,
    cats_config_content: str,
) -> None:
    """When a helper function raises in the hook, test is reported as failed, not lost."""
    # arrange
    config_path = pytester.path / "cats.yaml"
    config_path.write_text(cats_config_content)

    pytester.makeconftest(f'''
import os
os.environ["CATS_CONFIG"] = "{config_path}"

from unittest.mock import patch
patch("cats.pytest_plugin.get_cats_reporter", side_effect=RuntimeError("Simulated CATS error")).start()
''')
    pytester.makepyfile("def test_should_pass(): assert True")

    # act
    result = pytester.runpytest_subprocess("-v")

    # assert
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*CATS reporting error*"])


def test_hook_exception_preserves_test_count(
    pytester: pytest.Pytester,
    cats_config_content: str,
) -> None:
    """All tests are counted even when hook raises - none are silently dropped."""
    # arrange
    config_path = pytester.path / "cats.yaml"
    config_path.write_text(cats_config_content)

    pytester.makeconftest(f'''
import os
os.environ["CATS_CONFIG"] = "{config_path}"

from unittest.mock import patch
patch("cats.pytest_plugin.get_cats_reporter", side_effect=ValueError("Config error")).start()
''')
    pytester.makepyfile("""
def test_one(): assert True
def test_two(): assert True
def test_three(): assert True
""")

    # act
    result = pytester.runpytest_subprocess("-v")

    # assert - all 3 tests reported (as failed due to hook error), none silently dropped
    result.assert_outcomes(failed=3)
