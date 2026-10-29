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

"""Tests for cats.filepath.get_cats_config_filepath: the override, the environment variable, the default."""

from pathlib import Path

import pytest

from cats.filepath import DEFAULT_FILEPATH, get_cats_config_filepath, set_cats_config_filepath


@pytest.fixture(autouse=True)
def _no_override() -> None:
    set_cats_config_filepath(None)


def test_env_var_with_a_tilde_is_expanded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """CATS_CONFIG='~/cats.yaml' (quoted, so the shell did not expand it) is expanded before it is read."""
    # arrange
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CATS_CONFIG", "~/cats.yaml")

    # act
    result = get_cats_config_filepath()

    # assert
    assert result == tmp_path / "cats.yaml"


def test_unset_env_var_gives_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # arrange
    monkeypatch.delenv("CATS_CONFIG", raising=False)

    # act
    result = get_cats_config_filepath()

    # assert
    assert result == DEFAULT_FILEPATH


def test_override_wins_over_the_env_var(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # arrange
    override = tmp_path / "override.yaml"
    override.write_text("accounts_under_test: {}\n")
    monkeypatch.setenv("CATS_CONFIG", str(tmp_path / "env.yaml"))
    set_cats_config_filepath(override)

    # act
    result = get_cats_config_filepath()

    # assert
    assert result == override
