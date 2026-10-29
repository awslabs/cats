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

"""Tests for cats.cli.generate_files.pin_pytest_rootdir: cats init pins the nodeid's frame of reference."""

import re
import tomllib
from pathlib import Path

import pytest

from cats.cli.generate_files import pin_pytest_rootdir
from cats.exceptions import CatsConfigError


def test_no_project_file_writes_pytest_ini(tmp_path: Path) -> None:
    # act
    written = pin_pytest_rootdir(tmp_path)

    # assert
    expected = (tmp_path / "pytest.ini", "[pytest]\ntestpaths = tests\n")
    assert expected == (written, (tmp_path / "pytest.ini").read_text())


def test_existing_pyproject_gains_the_pytest_section(tmp_path: Path) -> None:
    # arrange
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "controls"\n')

    # act
    written = pin_pytest_rootdir(tmp_path)

    # assert
    expected = (pyproject, '[project]\nname = "controls"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    assert expected == (written, pyproject.read_text())


def test_existing_pyproject_without_trailing_newline_is_separated(tmp_path: Path) -> None:
    # arrange
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "controls"')

    # act
    pin_pytest_rootdir(tmp_path)

    # assert
    assert pyproject.read_text() == '[project]\nname = "controls"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'


def test_pyproject_that_configures_pytest_is_left_alone(tmp_path: Path) -> None:
    # arrange
    pyproject = tmp_path / "pyproject.toml"
    original = '[tool.pytest.ini_options]\naddopts = "-q"\n'
    pyproject.write_text(original)

    # act
    written = pin_pytest_rootdir(tmp_path)

    # assert
    expected = (None, original, False)
    assert expected == (written, pyproject.read_text(), (tmp_path / "pytest.ini").exists())


def test_existing_pytest_ini_is_left_alone(tmp_path: Path) -> None:
    # arrange
    pytest_ini = tmp_path / "pytest.ini"
    original = "[pytest]\naddopts = -q\n"
    pytest_ini.write_text(original)

    # act
    written = pin_pytest_rootdir(tmp_path)

    # assert
    assert (written, pytest_ini.read_text()) == (None, original)


def test_pytest_ini_is_never_written_beside_a_pyproject(tmp_path: Path) -> None:
    """pytest.ini would override the project's pyproject settings, so pyproject wins whenever it exists."""
    # arrange
    (tmp_path / "pyproject.toml").write_text("")

    # act
    pin_pytest_rootdir(tmp_path)

    # assert
    assert (tmp_path / "pytest.ini").exists() is False


def test_section_appended_after_another_tool_table_is_valid_toml(tmp_path: Path) -> None:
    """The appended table is a new top-level table, so it parses even when the file ends inside [tool.ruff]."""
    # arrange
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[tool.ruff]\nline-length = 100\n")

    # act
    pin_pytest_rootdir(tmp_path)

    # assert
    expected = {"tool": {"ruff": {"line-length": 100}, "pytest": {"ini_options": {"testpaths": ["tests"]}}}}
    assert tomllib.loads(pyproject.read_text()) == expected


def test_pytest_table_mentioned_in_a_comment_is_not_configuration(tmp_path: Path) -> None:
    # arrange
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('# TODO: add [tool.pytest.ini_options]\n[project]\nname = "controls"\n')

    # act
    written = pin_pytest_rootdir(tmp_path)

    # assert
    expected_text = '# TODO: add [tool.pytest.ini_options]\n[project]\nname = "controls"\n\n' + '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
    assert (pyproject, expected_text) == (written, pyproject.read_text())


def test_native_pytest_table_is_left_alone(tmp_path: Path) -> None:
    """pytest also reads a bare [tool.pytest] table; it configures pytest as much as ini_options does."""
    # arrange
    pyproject = tmp_path / "pyproject.toml"
    original = '[tool.pytest]\ntestpaths = ["checks"]\n'
    pyproject.write_text(original)

    # act
    written = pin_pytest_rootdir(tmp_path)

    # assert
    assert (written, pyproject.read_text()) == (None, original)


def test_malformed_pyproject_is_a_configuration_error(tmp_path: Path) -> None:
    # arrange
    pyproject = tmp_path / "pyproject.toml"
    original = '[project\nname = "controls"\n'
    pyproject.write_text(original)
    expected_message = (
        f"{pyproject} is not valid TOML: Expected ']' at the end of a table declaration (at line 1, column 9). "
        + "pytest cannot read it either; fix the file and run cats init again."
    )

    # act
    with pytest.raises(CatsConfigError, match=f"^{re.escape(expected_message)}$"):
        pin_pytest_rootdir(tmp_path)

    # assert: the file it could not read is untouched
    assert pyproject.read_text() == original


def test_tox_ini_with_a_pytest_section_is_left_alone(tmp_path: Path) -> None:
    """pytest reads [pytest] from tox.ini; writing pytest.ini beside it would override the project's settings."""
    # arrange
    (tmp_path / "tox.ini").write_text("[tox]\nenvlist = py311\n\n[pytest]\ntestpaths = tests\n", encoding="utf-8")

    # act
    written = pin_pytest_rootdir(tmp_path)

    # assert
    assert written is None
    assert (tmp_path / "pytest.ini").exists() is False


def test_tox_ini_without_a_pytest_section_is_not_configuration(tmp_path: Path) -> None:
    # arrange
    (tmp_path / "tox.ini").write_text("[tox]\nenvlist = py311\n", encoding="utf-8")

    # act
    written = pin_pytest_rootdir(tmp_path)

    # assert
    assert written == tmp_path / "pytest.ini"


def test_setup_cfg_with_a_tool_pytest_section_is_left_alone(tmp_path: Path) -> None:
    # arrange
    (tmp_path / "setup.cfg").write_text("[metadata]\nname = controls\n\n[tool:pytest]\ntestpaths = tests\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "controls"\n', encoding="utf-8")

    # act
    written = pin_pytest_rootdir(tmp_path)

    # assert
    assert written is None
    assert (tmp_path / "pyproject.toml").read_text(encoding="utf-8") == '[project]\nname = "controls"\n'


def test_setup_cfg_without_a_tool_pytest_section_is_not_configuration(tmp_path: Path) -> None:
    # arrange
    (tmp_path / "setup.cfg").write_text("[metadata]\nname = controls\n", encoding="utf-8")

    # act
    written = pin_pytest_rootdir(tmp_path)

    # assert
    assert written == tmp_path / "pytest.ini"


def test_malformed_tox_ini_without_a_pytest_section_is_ignored(tmp_path: Path) -> None:
    """An unrelated tox.ini that configparser rejects is no reason to refuse cats init: pytest.ini is written as usual."""
    # arrange
    (tmp_path / "tox.ini").write_text("testpaths = tests\n", encoding="utf-8")

    # act
    written = pin_pytest_rootdir(tmp_path)

    # assert
    assert (written, sorted(path.name for path in tmp_path.iterdir())) == (tmp_path / "pytest.ini", ["pytest.ini", "tox.ini"])


def test_malformed_tox_ini_with_a_pytest_header_is_left_alone(tmp_path: Path) -> None:
    """When configparser cannot parse the file, the bare header lines decide; pytest would still read this section."""
    # arrange
    (tmp_path / "tox.ini").write_text("garbage line\n[pytest]\ntestpaths = tests\n", encoding="utf-8")

    # act
    written = pin_pytest_rootdir(tmp_path)

    # assert
    assert (written, sorted(path.name for path in tmp_path.iterdir())) == (None, ["tox.ini"])


def test_existing_dot_pytest_ini_is_left_alone(tmp_path: Path) -> None:
    """pytest reads .pytest.ini exactly like pytest.ini, so it pins the rootdir too."""
    # arrange
    (tmp_path / ".pytest.ini").write_text("[pytest]\n", encoding="utf-8")

    # act
    written = pin_pytest_rootdir(tmp_path)

    # assert
    assert (written, sorted(path.name for path in tmp_path.iterdir())) == (None, [".pytest.ini"])


def test_pyproject_that_configures_pytest_wins_over_a_malformed_tox_ini(tmp_path: Path) -> None:
    """pyproject.toml is checked first: pytest reads it ahead of tox.ini, and a broken tox.ini must not block an already configured project."""
    # arrange
    original = '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
    (tmp_path / "pyproject.toml").write_text(original, encoding="utf-8")
    (tmp_path / "tox.ini").write_text("not an ini file\n", encoding="utf-8")

    # act
    written = pin_pytest_rootdir(tmp_path)

    # assert
    assert (written, (tmp_path / "pyproject.toml").read_text(encoding="utf-8")) == (None, original)


@pytest.mark.parametrize("name", ["pytest.toml", ".pytest.toml"])
def test_existing_pytest_toml_is_left_alone(tmp_path: Path, name: str) -> None:
    """pytest 9 reads pytest.toml / .pytest.toml ahead of every other config file, so either pins the rootdir."""
    # arrange
    (tmp_path / name).write_text("[pytest]\n", encoding="utf-8")

    # act
    written = pin_pytest_rootdir(tmp_path)

    # assert
    assert (written, sorted(path.name for path in tmp_path.iterdir())) == (None, [name])


def test_latin_1_setup_cfg_without_a_pytest_section_is_ignored(tmp_path: Path) -> None:
    """A setup.cfg that is not UTF-8 (common in older projects) must not crash cats init."""
    # arrange
    (tmp_path / "setup.cfg").write_bytes(b"[metadata]\nauthor = Jos\xe9\n")

    # act
    written = pin_pytest_rootdir(tmp_path)

    # assert
    assert (written, sorted(path.name for path in tmp_path.iterdir())) == (tmp_path / "pytest.ini", ["pytest.ini", "setup.cfg"])
