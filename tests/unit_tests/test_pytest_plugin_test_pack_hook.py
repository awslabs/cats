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

"""Tests for the ``pytest_cats_test_pack`` hook wiring in cats.pytest_plugin.

Each test runs a real pytest subprocess with a consumer conftest, the way a test pack
would use the hook, and reads the resulting User-Agent suffix back from inside that run.
"""

import pytest
import yaml

from cats.version import __version__
from tests.utils import make_config

CATS_TOKEN = f"AwsCats/{__version__}"

# A test that writes the process's suffix to a file the outer test can read exactly.
WRITE_SUFFIX_TEST = """
from pathlib import Path
from cats.user_agent import user_agent_extra

def test_write_suffix(worker_id="main"):
    Path(f"suffix-{worker_id}.txt").write_text(user_agent_extra())
"""


def write_project(pytester: pytest.Pytester, conftest_body: str, test_source: str = WRITE_SUFFIX_TEST) -> None:
    config_path = pytester.path / "cats.yaml"
    config_path.write_text(yaml.dump(make_config(accounts={"testaccount": "234567890123"})))
    pytester.makeconftest(f'''
import os
os.environ["CATS_CONFIG"] = "{config_path}"
{conftest_body}
''')
    pytester.makepyfile(test_source)


def read_suffix(pytester: pytest.Pytester, worker_id: str = "main") -> str:
    return (pytester.path / f"suffix-{worker_id}.txt").read_text()


def test_conftest_hook_puts_the_pack_in_the_suffix(pytester: pytest.Pytester) -> None:
    # arrange
    write_project(
        pytester,
        """
from cats import CatsTestPack

def pytest_cats_test_pack():
    return CatsTestPack(name="acme-controls", version="0.99.4")
""",
    )
    expected = f"{CATS_TOKEN} AwsCatsPack/acme-controls#0.99.4"

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly")

    # assert
    result.assert_outcomes(passed=1)
    assert expected == read_suffix(pytester)


def test_no_hook_leaves_the_cats_token_alone(pytester: pytest.Pytester) -> None:
    # arrange
    write_project(pytester, "")
    expected = CATS_TOKEN

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly")

    # assert
    result.assert_outcomes(passed=1)
    assert expected == read_suffix(pytester)


def test_hook_returning_none_leaves_the_cats_token_alone(pytester: pytest.Pytester) -> None:
    # arrange
    write_project(
        pytester,
        """
def pytest_cats_test_pack():
    return None
""",
    )
    expected = CATS_TOKEN

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly")

    # assert
    result.assert_outcomes(passed=1)
    assert expected == read_suffix(pytester)


def test_raising_hook_warns_and_keeps_the_run_and_the_cats_token(pytester: pytest.Pytester) -> None:
    # arrange
    write_project(
        pytester,
        """
def pytest_cats_test_pack():
    raise RuntimeError("workbook missing")
""",
    )
    expected = CATS_TOKEN

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly", "-W", "default")

    # assert - a warning raised inside pytest_configure predates pytest's warning capture, so it goes to stderr
    result.assert_outcomes(passed=1)
    assert expected == read_suffix(pytester)
    result.stderr.fnmatch_lines(["*pytest_cats_test_pack hook raised RuntimeError('workbook missing') - no test pack recorded in the User-Agent*"])


def test_wrong_return_type_warns_and_keeps_the_cats_token(pytester: pytest.Pytester) -> None:
    # arrange
    write_project(
        pytester,
        """
def pytest_cats_test_pack():
    return "acme-controls/0.99.4"
""",
    )
    expected = CATS_TOKEN

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly", "-W", "default")

    # assert - a warning raised inside pytest_configure predates pytest's warning capture, so it goes to stderr
    result.assert_outcomes(passed=1)
    assert expected == read_suffix(pytester)
    result.stderr.fnmatch_lines(["*pytest_cats_test_pack returned str, expected CatsTestPack - ignoring it*"])


def test_every_xdist_worker_registers_the_pack(pytester: pytest.Pytester) -> None:
    # arrange
    write_project(
        pytester,
        """
from cats import CatsTestPack

def pytest_cats_test_pack():
    return CatsTestPack(name="acme-controls", version="0.99.4")
""",
        test_source="""
from pathlib import Path
from cats.user_agent import user_agent_extra

def test_one(worker_id):
    Path(f"suffix-{worker_id}.txt").write_text(user_agent_extra())

def test_two(worker_id):
    Path(f"suffix-{worker_id}.txt").write_text(user_agent_extra())
""",
    )
    expected = {f"{CATS_TOKEN} AwsCatsPack/acme-controls#0.99.4"}

    # act
    result = pytester.runpytest_subprocess("-n", "2", "--dist", "loadfile", "-p", "no:randomly")

    # assert
    result.assert_outcomes(passed=2)
    suffixes = {path.read_text() for path in pytester.path.glob("suffix-gw*.txt")}
    assert expected == suffixes


def test_hook_in_a_late_conftest_warns_with_the_fix(pytester: pytest.Pytester) -> None:
    """A conftest under a directory pytest only discovers during collection is too late."""
    # arrange
    write_project(pytester, "")
    late_dir = pytester.mkpydir("late")
    (late_dir / "conftest.py").write_text(
        """
from cats import CatsTestPack

def pytest_cats_test_pack():
    return CatsTestPack(name="late-pack", version="1")
"""
    )
    (late_dir / "test_late.py").write_text("def test_late(): assert True\n")
    expected = CATS_TOKEN

    # act
    result = pytester.runpytest_subprocess("-p", "no:randomly", "-W", "default")

    # assert
    result.assert_outcomes(passed=2)
    assert expected == read_suffix(pytester)
    result.stdout.fnmatch_lines(
        ["*late/conftest.py implements pytest_cats_test_pack but was loaded after pytest_configure, so its test pack is not in the User-Agent.*"]
    )
