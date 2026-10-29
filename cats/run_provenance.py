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

"""What code and configuration a run tested with: the ``run.test_packs``, ``run.git`` and ``run.config_sha256`` facts.

Gathered once on the xdist controller in ``pytest_sessionstart`` (see the pytest plugin), so a
parallel run records exactly what a single-process run records. Nothing here raises: a fact
that cannot be established is ``None`` with one warning, and the run goes on.
"""

import hashlib
import subprocess
import warnings
from pathlib import Path

from cats.report_run import RunGit, RunProvenance, RunTestPack
from cats.user_agent import registered_test_packs

# git answers rev-parse and status in milliseconds on any checkout a test suite lives in; a
# repository that takes longer than this is reported as null rather than holding the run.
GIT_TIMEOUT_SECONDS = 10.0


def gather_provenance(rootdir: Path, config_path: Path) -> RunProvenance:
    """The provenance facts of a run starting now: packs as registered, the checkout at ``rootdir``, the config's hash."""
    return RunProvenance(
        test_packs=[RunTestPack(name=pack.name, version=pack.version, metadata=dict(pack.metadata)) for pack in registered_test_packs()],
        git=git_state(rootdir),
        config_sha256=config_sha256(config_path),
    )


def git_state(rootdir: Path, git: str = "git") -> RunGit | None:
    """The commit of the checkout holding ``rootdir`` and whether the tree under ``rootdir`` differs from it.

    ``dirty`` is true for any change to a tracked file under ``rootdir`` and for an untracked
    ``.py`` file under it. An untracked file of any other kind never dirties the run: a report
    directory that is not ignored would otherwise dirty every run that writes into it, and a
    data file matters only through the code that reads it, which is tracked or is ``.py``. The
    ``-- .`` pathspec keeps a change elsewhere in a monorepo out of the answer.

    ``None`` with one warning when ``git`` is not on ``PATH``, ``rootdir`` is not inside a work
    tree, ``HEAD`` has no commit, or ``git`` does not answer within GIT_TIMEOUT_SECONDS.
    """
    try:
        head = _git(git, rootdir, "rev-parse", "HEAD")
        status = _git(git, rootdir, "status", "--porcelain", "--untracked-files=all", "--", ".")
    except FileNotFoundError:
        warnings.warn(f"'{git}' is not on PATH, so run.git is null", stacklevel=2)
        return None
    except subprocess.TimeoutExpired:
        warnings.warn(f"git did not answer within {GIT_TIMEOUT_SECONDS:g}s for {rootdir}, so run.git is null", stacklevel=2)
        return None
    except subprocess.CalledProcessError as e:
        reason = e.stderr.strip().splitlines()[0] if e.stderr.strip() else f"exit status {e.returncode}"
        warnings.warn(f"git could not describe {rootdir} ({reason}), so run.git is null", stacklevel=2)
        return None
    return RunGit(commit=head.strip(), dirty=_is_dirty(status))


def _git(git: str, rootdir: Path, *args: str) -> str:
    """Run one git command in ``rootdir`` and return its stdout; raises for a missing binary, a timeout, or a non-zero exit."""
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell; the only variable part is the git executable name
        [git, *args], cwd=rootdir, capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS, check=True
    )
    return completed.stdout


def _is_dirty(porcelain_status: str) -> bool:
    """Read ``git status --porcelain`` output: any tracked change, or an untracked ``.py`` file, is dirty.

    A path holding a space or a quote arrives quoted (``?? "my test.py"``), so the quotes are
    stripped before the suffix is read.
    """
    for line in porcelain_status.splitlines():
        if line.startswith("?? "):
            if line[3:].strip('"').endswith(".py"):
                return True
            continue
        if line.strip():
            return True
    return False


def config_sha256(config_path: Path) -> str | None:
    """Lowercase hex SHA-256 of the bytes of ``cats.yaml``; ``sha256sum cats.yaml`` checks it. ``None`` with one warning when unreadable."""
    try:
        digest = hashlib.sha256(config_path.read_bytes()).hexdigest()
    except OSError as e:
        warnings.warn(f"{config_path} could not be read ({e.strerror}), so run.config_sha256 is null", stacklevel=2)
        return None
    return digest
