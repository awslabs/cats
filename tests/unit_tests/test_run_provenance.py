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

"""Tests for cats.run_provenance: the checkout's commit and dirty state, and the config hash, against real repositories."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest
import yaml
from pytest_mock import MockerFixture

from cats.run_provenance import GIT_TIMEOUT_SECONDS, config_sha256, git_state
from tests.utils import make_config

pytestmark = pytest.mark.usefixtures("git_ceiling")


@pytest.fixture(scope="module")
def repo_base() -> Path:
    """Where the test repositories live: the system temp root when git can work there, else /tmp.

    git resolves the real path of its working directory, stat-ing every parent on the way; an
    agent sandbox may deny that for its private temp tree while allowing /tmp. Probed once.
    """
    probe = Path(tempfile.mkdtemp(prefix="cats-git-probe-"))
    try:
        usable = subprocess.run(["git", "init", "-q"], cwd=probe, capture_output=True, check=False).returncode == 0  # noqa: S607
    finally:
        shutil.rmtree(probe)
    return Path(tempfile.gettempdir()) if usable is True else Path("/tmp")  # noqa: S108 - a fallback, not a fixed file path


@pytest.fixture
def git_ceiling(monkeypatch: pytest.MonkeyPatch, repo_base: Path) -> None:
    """Stop git walking up from a test repository looking for an enclosing one; the tests see only the repository they made."""
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(repo_base))


@pytest.fixture
def repo(repo_base: Path) -> Generator[Path, None, None]:
    """A repository with one commit holding tests/test_a.py, tests/fixture.yaml and README.md; tests/ serves as a nested rootdir."""
    root = Path(tempfile.mkdtemp(prefix="cats-git-state-", dir=repo_base))
    (root / "tests").mkdir()
    (root / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    (root / "tests" / "fixture.yaml").write_text("a: 1\n")
    (root / "README.md").write_text("# suite\n")
    git(root, "init", "-q")
    git(root, "add", ".")
    git(root, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "init")
    yield root
    shutil.rmtree(root)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout  # noqa: S603, S607


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").strip()


# =============================================================================
# git_state: the commit and the dirty flag
# =============================================================================


def test_clean_checkout_records_head_and_not_dirty(repo: Path) -> None:
    # act
    state = git_state(repo)

    # assert
    expected = {"commit": head(repo), "dirty": False}
    assert expected == state


def test_modified_tracked_file_is_dirty(repo: Path) -> None:
    # arrange
    (repo / "tests" / "test_a.py").write_text("def test_a():\n    assert False\n")

    # act
    state = git_state(repo)

    # assert
    expected = {"commit": head(repo), "dirty": True}
    assert expected == state


def test_untracked_python_file_is_dirty(repo: Path) -> None:
    """A new test module or helper that no commit holds: the run corresponds to no commit."""
    # arrange
    (repo / "tests" / "test_new.py").write_text("def test_new():\n    assert True\n")

    # act
    state = git_state(repo)

    # assert
    expected = {"commit": head(repo), "dirty": True}
    assert expected == state


def test_untracked_non_python_file_is_not_dirty(repo: Path) -> None:
    """An unignored report directory must not dirty every run that writes into it."""
    # arrange
    (repo / "reports").mkdir()
    (repo / "reports" / "report-20260924T105317Z.json").write_text("{}")
    (repo / "notes.txt").write_text("scratch")

    # act
    state = git_state(repo)

    # assert
    expected = {"commit": head(repo), "dirty": False}
    assert expected == state


def test_change_outside_rootdir_is_not_dirty(repo: Path) -> None:
    """In a monorepo the checkout holds more than the suite; only the tree under pytest's rootdir counts."""
    # arrange - rootdir is tests/; the change is a sibling of it
    (repo / "README.md").write_text("# suite, edited\n")

    # act
    state = git_state(repo / "tests")

    # assert
    expected = {"commit": head(repo), "dirty": False}
    assert expected == state


def test_change_under_a_nested_rootdir_is_dirty(repo: Path) -> None:
    # arrange
    (repo / "tests" / "fixture.yaml").write_text("a: 2\n")

    # act
    state = git_state(repo / "tests")

    # assert
    expected = {"commit": head(repo), "dirty": True}
    assert expected == state


def test_untracked_python_file_with_a_space_in_its_name_is_dirty(repo: Path) -> None:
    """git quotes such a path in porcelain output; the suffix must still be read."""
    # arrange
    (repo / "tests" / "test new.py").write_text("def test_new():\n    assert True\n")

    # act
    state = git_state(repo)

    # assert
    expected = {"commit": head(repo), "dirty": True}
    assert expected == state


# =============================================================================
# git_state: the null cases, each with its warning
# =============================================================================


def test_directory_outside_any_work_tree_is_null_with_a_warning(repo: Path) -> None:
    # arrange
    outside = repo.parent / f"{repo.name}-outside"
    outside.mkdir()
    try:
        # act
        with pytest.warns(
            UserWarning,
            match=rf"^git could not describe {outside} \(fatal: not a git repository \(or any of the parent directories\): \.git\), "
            + r"so run\.git is null$",
        ):
            state = git_state(outside)
    finally:
        outside.rmdir()

    # assert
    assert state is None


def test_repository_without_a_commit_is_null_with_a_warning(repo: Path) -> None:
    # arrange
    empty = repo.parent / f"{repo.name}-empty"
    empty.mkdir()
    git(empty, "init", "-q")
    try:
        # act
        with pytest.warns(
            UserWarning,
            match=rf"^git could not describe {empty} \(fatal: ambiguous argument 'HEAD': unknown revision or path not in the working tree\.\), "
            + r"so run\.git is null$",
        ):
            state = git_state(empty)
    finally:
        shutil.rmtree(empty)

    # assert
    assert state is None


def test_missing_git_binary_is_null_with_a_warning(repo: Path) -> None:
    # act
    with pytest.warns(UserWarning, match=r"^'/nonexistent/git' is not on PATH, so run\.git is null$"):
        state = git_state(repo, git="/nonexistent/git")

    # assert
    assert state is None


def test_slow_git_is_null_with_a_warning(repo: Path, mocker: MockerFixture) -> None:
    # arrange
    mocker.patch("cats.run_provenance.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["git"], timeout=GIT_TIMEOUT_SECONDS))

    # act
    with pytest.warns(UserWarning, match=rf"^git did not answer within 10s for {repo}, so run\.git is null$"):
        state = git_state(repo)

    # assert
    assert state is None


# =============================================================================
# config_sha256
# =============================================================================


def test_config_hash_is_what_sha256sum_prints(tmp_path: Path) -> None:
    # arrange
    config_path = tmp_path / "cats.yaml"
    config_path.write_bytes(b"cats_deployment_settings:\n  parent_account_id: '999999999999'\n")

    # act
    digest = config_sha256(config_path)

    # assert
    assert digest == hashlib.sha256(config_path.read_bytes()).hexdigest()


def test_unreadable_config_is_null_with_a_warning(tmp_path: Path) -> None:
    # arrange
    missing = tmp_path / "gone.yaml"

    # act
    with pytest.warns(UserWarning, match=rf"^{missing} could not be read \(No such file or directory\), so run\.config_sha256 is null$"):
        digest = config_sha256(missing)

    # assert
    assert digest is None


# =============================================================================
# Through the plugin: a parallel run records what a serial run records
# =============================================================================

PACK_CONFTEST = """
from cats import CatsTestPack


def pytest_cats_test_pack():
    return CatsTestPack(name="acme-controls", version="0.99.4", metadata={"framework": "NIST-800-53r5"})
"""


def run_pytest(project: Path, config_path: Path, *args: str) -> None:
    """One pytest session in ``project`` with CATS pointed at ``config_path``; fails the test if the session fails."""
    subprocess.run(  # noqa: S603 - fixed argv, this interpreter, no shell
        [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", "-p", "no:cacheprovider", *args],
        cwd=project,
        env={**os.environ, "CATS_CONFIG": str(config_path)},
        capture_output=True,
        text=True,
        check=True,
    )


def test_xdist_run_records_the_same_provenance_as_a_serial_run(repo_base: Path) -> None:
    """test_packs, git and config_sha256 are controller-only facts, so ``-n 2`` records exactly what a serial run does.

    The project is a real checkout that declares a pack with metadata and is dirtied by an untracked test module.
    """
    # arrange
    project = Path(tempfile.mkdtemp(prefix="cats-provenance-parity-", dir=repo_base))
    try:
        config = make_config(accounts={"testaccount": "234567890123"})
        config["reporting"] = {"timestamped_files": True}
        config_path = project / "cats.yaml"
        config_path.write_text(yaml.dump(config))
        (project / "conftest.py").write_text(PACK_CONFTEST)
        (project / "test_alpha.py").write_text("def test_alpha():\n    assert True\n")
        git(project, "init", "-q")
        git(project, "add", "cats.yaml", "conftest.py", "test_alpha.py")
        git(project, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "init")
        (project / "test_beta.py").write_text("def test_beta():\n    assert True\n")

        # act
        run_pytest(project, config_path)
        run_pytest(project, config_path, "-n", "2")
        serial, parallel = (
            {key: json.loads(path.read_text())["run"][key] for key in ("test_packs", "git", "config_sha256")}
            for path in sorted(project.glob("report-*.json"))
        )

        # assert - test_beta.py is an untracked .py, so both runs must say dirty
        expected = {
            "test_packs": [{"name": "acme-controls", "version": "0.99.4", "metadata": {"framework": "NIST-800-53r5"}}],
            "git": {"commit": head(project), "dirty": True},
            "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        }
        assert (expected, expected) == (serial, parallel)
    finally:
        shutil.rmtree(project)
