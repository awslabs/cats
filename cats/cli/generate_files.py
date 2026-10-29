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

import configparser
import tomllib
from pathlib import Path

from cats.cli.console import fail_marker, ok_marker, stderr_console, stdout_console
from cats.exceptions import CatsConfigError
from cats.filepath import get_default_output_dir


def create_example_test(initial_account_name: str, output_dir: Path | None = None) -> Path | None:
    """Create example test file.

    Defaults to config file's directory if output_dir not specified.
    Returns the path if created, None otherwise.
    """
    target_dir = output_dir or get_default_output_dir()
    tests_dir = target_dir / "tests"
    test_path = tests_dir / "example_test.py"

    fixture_name = f"{initial_account_name}_account"
    example_test = f"""from cats import AccountUnderTest


def test_simple({fixture_name}: AccountUnderTest):
    sts = {fixture_name}.session.client("sts")
    caller_id = sts.get_caller_identity()
    assert {fixture_name}.account_id == caller_id["Account"]
"""

    if tests_dir.exists() is False:
        tests_dir.mkdir()
        with test_path.open("w", encoding="utf-8") as f:
            f.write(example_test)
        stdout_console.print(f"{ok_marker(stdout_console)} Created example test at {test_path}")
        return test_path
    else:
        stderr_console.print(f"{fail_marker(stderr_console)} '{tests_dir}' directory already exists: skipping creating example test")
    return None


PYPROJECT_PYTEST_SECTION = '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
PYTEST_INI = "[pytest]\ntestpaths = tests\n"


def pin_pytest_rootdir(output_dir: Path | None = None) -> Path | None:
    """Pin pytest's rootdir at the project root, so every test's nodeid has one frame of reference.

    The nodeid is the entry key of report.json and the seed of every OSCAL and Security Hub
    identifier, and pytest derives it relative to its rootdir, which moves with the arguments
    it is given unless an ini file fixes it. A ``pytest.ini``, ``.pytest.ini``, ``pytest.toml`` or
    ``.pytest.toml``, a ``tox.ini`` with a ``[pytest]`` section or a ``setup.cfg`` with a
    ``[tool:pytest]`` section already pins it and is left alone.
    Otherwise an existing ``pyproject.toml`` gains ``[tool.pytest.ini_options]`` unless it already
    configures pytest, else ``pytest.ini`` is written. Never both: ``pytest.ini`` would override
    the project's own pyproject settings.

    Returns the file written to, or None when pytest was already configured. Raises
    ``CatsConfigError`` when ``pyproject.toml`` is not valid TOML.
    """
    target_dir = output_dir or get_default_output_dir()
    pyproject = target_dir / "pyproject.toml"
    pytest_ini = target_dir / "pytest.ini"
    if any((target_dir / name).exists() for name in ("pytest.ini", ".pytest.ini", "pytest.toml", ".pytest.toml")):
        return None
    content = pyproject.read_text(encoding="utf-8") if pyproject.exists() is True else None
    if content is not None and _configures_pytest(pyproject, content) is True:
        return None
    # pytest reads pyproject.toml ahead of tox.ini and setup.cfg, so a project configured there must
    # be left alone before anything is added to pyproject.toml.
    if _ini_has_section(target_dir / "tox.ini", "pytest") is True or _ini_has_section(target_dir / "setup.cfg", "tool:pytest") is True:
        return None
    if content is not None:
        separator = "" if content.endswith("\n") or content == "" else "\n"
        pyproject.write_text(f"{content}{separator}\n{PYPROJECT_PYTEST_SECTION}", encoding="utf-8")
        stdout_console.print(f"{ok_marker(stdout_console)} Added [tool.pytest.ini_options] to {pyproject}", markup=False)
        return pyproject
    pytest_ini.write_text(PYTEST_INI, encoding="utf-8")
    stdout_console.print(f"{ok_marker(stdout_console)} Created {pytest_ini}")
    return pytest_ini


def _configures_pytest(pyproject: Path, content: str) -> bool:
    """Whether the file has a ``[tool.pytest]`` table (``[tool.pytest.ini_options]`` lives inside it)."""
    try:
        parsed = tomllib.loads(content)
    except tomllib.TOMLDecodeError as e:
        raise CatsConfigError(f"{pyproject} is not valid TOML: {e}. pytest cannot read it either; fix the file and run cats init again.") from e
    return "pytest" in parsed.get("tool", {})


def _ini_has_section(ini_file: Path, section: str) -> bool:
    """Whether an ini-style file exists and has ``[section]``; pytest reads tox.ini's ``[pytest]`` and setup.cfg's ``[tool:pytest]``.

    A file configparser cannot parse is judged by its header lines alone: the file may have nothing to
    do with pytest, and an unrelated malformed tox.ini is no reason to refuse ``cats init``.
    """
    if ini_file.is_file() is False:
        return False
    # errors="replace": older projects have Latin-1 setup.cfg files, and the headers compared below are ASCII.
    text = ini_file.read_text(encoding="utf-8", errors="replace")
    parser = configparser.RawConfigParser(strict=False)
    try:
        parser.read_string(text, source=str(ini_file))
    except configparser.Error:
        return any(line.strip() == f"[{section}]" for line in text.splitlines())
    return parser.has_section(section)
