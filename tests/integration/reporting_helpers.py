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

"""Nested pytest sessions over a deployed stack: the reporting path fed by real test results.

The integration suite's own session records no evidence (it has no cats.yaml at its rootdir, and
its stacks are gone by session end), so each module that already holds a deployed stack runs one
short `pytest` session over it, as a user would, with a `report_formats` shape of its own, and
reads the files back. Every shape then has one proof against real infrastructure.
"""

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tests.utils import write_config

# The nested session inherits this process's environment minus what pytest and xdist set for THIS
# session: PYTEST_CURRENT_TEST names the outer test, and the PYTEST_XDIST_* variables would make the
# child believe it is one of our workers.
_OUTER_SESSION_VARIABLES = ("PYTEST_CURRENT_TEST", "PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT", "PYTEST_XDIST_TESTRUNUID", "PYTEST_ADDOPTS")

NESTED_TEST_FILE = "test_nested.py"


@dataclass(frozen=True)
class NestedRun:
    """A finished nested session and its report directory."""

    returncode: int
    stdout: str
    stderr: str
    report_dir: Path

    def report(self) -> dict[str, Any]:
        """The fixed-name report.json."""
        document: dict[str, Any] = json.loads((self.report_dir / "report.json").read_text())
        return document

    def assessment_results(self) -> dict[str, Any]:
        """The fixed-name assessment-results.json."""
        document: dict[str, Any] = json.loads((self.report_dir / "assessment-results.json").read_text())
        return document

    def securityhub_document(self) -> dict[str, Any]:
        """The fixed-name securityhub-findings.json."""
        document: dict[str, Any] = json.loads((self.report_dir / "securityhub-findings.json").read_text())
        return document

    def junit_testsuite(self) -> ET.Element:
        """The fixed-name junit.xml's testsuite element."""
        testsuite = ET.parse(self.report_dir / "junit.xml").getroot().find("./testsuite")  # noqa: S314 - XML written by the nested pytest run
        assert testsuite is not None, "junit.xml has no testsuite element"
        return testsuite

    def file_names(self) -> list[str]:
        """Every file the session left in the report directory."""
        return sorted(path.name for path in self.report_dir.iterdir())

    def summary(self) -> str:
        """Both streams, for the assertion message when the session did not go as expected."""
        return f"exit {self.returncode}\n--- stdout ---\n{self.stdout}\n--- stderr ---\n{self.stderr}"


def junit_properties(element: ET.Element) -> dict[str, str]:
    """``{name: value}`` of the element's direct ``properties/property`` children."""
    return {str(prop.get("name")): str(prop.get("value")) for prop in element.findall("./properties/property")}


def run_nested_cats_session(
    config: dict[str, Any],
    report_formats: Sequence[str],
    test_source: str,
    workdir: Path,
    *,
    conftest_source: str | None = None,
    pytest_args: Sequence[str] = (),
    securityhub_region: str | None = None,
) -> NestedRun:
    """Run one pytest session in ``workdir`` against the stack ``config`` describes, with these report formats.

    ``config`` is the deployed module's own configuration (same prefix, keynames and Region), so
    the session's ``<keyname>_account`` fixtures reach the module's stack; only ``reporting`` is
    replaced, with both the timestamped and the fixed-name files on. The session runs as a
    subprocess, as a user's would: the plugin comes from the ``pytest11`` entry point, the config
    from ``CATS_CONFIG``, and the report directory is ``workdir / "reports"``.
    """
    reporting: dict[str, Any] = {"report_formats": list(report_formats), "report_dir": "reports", "latest_files": True, "timestamped_files": True}
    if securityhub_region is not None:
        reporting["securityhub"] = {"aws_region": securityhub_region}
    config_path = workdir / "cats.yaml"
    write_config(config_path, {**config, "reporting": reporting})
    (workdir / NESTED_TEST_FILE).write_text(test_source)
    if conftest_source is not None:
        (workdir / "conftest.py").write_text(conftest_source)

    env = {name: value for name, value in os.environ.items() if name not in _OUTER_SESSION_VARIABLES}
    env["CATS_CONFIG"] = str(config_path)
    command = [sys.executable, "-m", "pytest", "-p", "no:randomly", "-p", "no:cacheprovider", "-q", *pytest_args, NESTED_TEST_FILE]
    completed = subprocess.run(command, cwd=workdir, env=env, capture_output=True, text=True, check=False)  # noqa: S603 - fixed argv built here
    return NestedRun(completed.returncode, completed.stdout, completed.stderr, workdir / "reports")
