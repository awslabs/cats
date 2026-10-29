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

"""Smoke-test a built wheel the way an adopter meets it: install it into a clean venv and use it.

Run after ``uv build`` (and ``twine check``) in CI::

    python tests/artifact_smoke.py dist/

It asserts the wheel carries every data file the package reads at runtime (templates, prompts,
the EC2 server units, ``py.typed``), then installs the wheel into a fresh virtual environment
with no extras and, in an empty directory, runs ``cats --help``, ``cats --version`` and
``pytest --collect-only -p cats``: the entry point resolves, the plugin loads without a
``cats.yaml`` and writes nothing. A packaging regression (a template dropped from the wheel,
an extra's import leaking into the core path) fails here, not at the first adopter.

No test framework: the script is the check, so it runs identically in CI and by hand.
"""

import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

# Every non-Python file the installed package reads; a missing one is a wheel that cannot deploy or prompt.
REQUIRED_DATA_FILES = frozenset(
    {
        "cats/cfn/ec2_server/README.md",
        "cats/cfn/ec2_server/tcpserver.service",
        "cats/cfn/ec2_server/udpserver.service",
        "cats/cfn/templates/stack.yaml.j2",
        "cats/cfn/templates/targets.txt.j2",
        "cats/cfn/templates/userdata.sh.j2",
        "cats/prompts/agentic.md.j2",
        "cats/prompts/base.md.j2",
        "cats/prompts/overview.md.j2",
        "cats/prompts/setup.md.j2",
        "cats/prompts/testing.md.j2",
        "cats/py.typed",
    }
)


def the_wheel(dist: Path) -> Path:
    wheels = sorted(dist.glob("*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"expected exactly one wheel in {dist}, found {[wheel.name for wheel in wheels]}")
    return wheels[0].resolve()  # the install runs from the empty project directory, so a relative dist/ would not be found there


def check_data_files(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        shipped = {name for name in archive.namelist() if not name.endswith(".py") and ".dist-info/" not in name}
    missing = sorted(REQUIRED_DATA_FILES - shipped)
    if missing:
        raise SystemExit(f"{wheel.name} lacks data files the package reads: {missing}")
    print(f"ok: {wheel.name} carries all {len(REQUIRED_DATA_FILES)} required data files")


def run(command: list[str], cwd: Path, env: dict[str, str], ok_exit_codes: frozenset[int] = frozenset({0})) -> str:
    completed = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, check=False)  # noqa: S603 - fixed argv built here
    if completed.returncode not in ok_exit_codes:
        raise SystemExit(f"{' '.join(command)} exited {completed.returncode}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}")
    return completed.stdout


def check_clean_install(wheel: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        venv = Path(tmp) / "venv"
        empty_project = Path(tmp) / "project"
        empty_project.mkdir()
        env = {key: value for key, value in os.environ.items() if not key.startswith(("CATS_", "AWS_", "PYTEST_", "VIRTUAL_ENV"))}
        env["UV_PYTHON_DOWNLOADS"] = "never"

        run(["uv", "venv", str(venv), "--python", sys.executable], empty_project, env)
        run(["uv", "pip", "install", "--python", str(venv / "bin" / "python"), str(wheel), "pytest"], empty_project, env)
        cats = venv / "bin" / "cats"
        python = venv / "bin" / "python"

        run([str(cats), "--help"], empty_project, env)
        version = run([str(cats), "--version"], empty_project, env).strip()
        # Exit 5 is pytest's "no tests collected": the plugin loaded (a broken plugin is exit 3 or 4) and there was nothing to collect.
        run([str(python), "-m", "pytest", "--collect-only", "-q", "-p", "cats"], empty_project, env, ok_exit_codes=frozenset({5}))
        leftovers = sorted(path.name for path in empty_project.iterdir())
        if leftovers:
            raise SystemExit(f"a CATS-less pytest run left files behind: {leftovers}")
        print(f"ok: clean venv install of cats {version}; cats --help, cats --version and pytest --collect-only -p cats ran without a cats.yaml")


def main(argv: list[str]) -> None:
    if len(argv) != 2:
        raise SystemExit("usage: python tests/artifact_smoke.py <dist directory>")
    wheel = the_wheel(Path(argv[1]))
    check_data_files(wheel)
    check_clean_install(wheel)


if __name__ == "__main__":
    main(sys.argv)
