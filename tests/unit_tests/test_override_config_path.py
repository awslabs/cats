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

"""Tests for cats.config.override_config_path."""

from pathlib import Path

import pytest

from cats.config import Settings, override_config_path
from cats.exceptions import CatsConfigError
from cats.filepath import get_cats_config_override, set_cats_config_filepath
from tests.utils import make_config, write_config


def test_missing_explicit_path_raises(tmp_path: Path) -> None:
    # arrange
    missing = tmp_path / "nope.yaml"

    # act / assert
    with pytest.raises(CatsConfigError, match=f"Config file not found: {missing}"), override_config_path(missing):
        pass


def test_directory_path_is_named_as_a_directory(tmp_path: Path) -> None:
    """'not found' for a path that exists sends the user looking for a typo."""
    # act / assert
    with pytest.raises(CatsConfigError, match=f"Config path is a directory, expected a cats.yaml file: {tmp_path}"), override_config_path(tmp_path):
        pass


def test_settings_load_from_the_overridden_path(tmp_path: Path) -> None:
    # arrange
    other = tmp_path / "other.yaml"
    write_config(other, make_config(accounts={"fromOther": "111111111111"}))

    # act
    with override_config_path(other):
        accounts = list(Settings().accounts_under_test)

    # assert
    assert accounts == ["fromOther"]


def test_previous_override_restored_after_block(tmp_path: Path) -> None:
    # arrange
    previous = tmp_path / "previous.yaml"
    other = tmp_path / "other.yaml"
    write_config(previous, make_config(accounts={"fromPrevious": "111111111111"}))
    write_config(other, make_config(accounts={"fromOther": "222222222222"}))
    set_cats_config_filepath(previous)

    # act
    with override_config_path(other):
        pass

    # assert
    assert (get_cats_config_override(), list(Settings().accounts_under_test)) == (previous, ["fromPrevious"])


def test_override_restored_when_block_raises(tmp_path: Path) -> None:
    # arrange
    other = tmp_path / "other.yaml"
    write_config(other, make_config(accounts={"fromOther": "111111111111"}))

    # act
    with pytest.raises(RuntimeError), override_config_path(other):
        raise RuntimeError("boom")

    # assert
    assert get_cats_config_override() is None


def test_none_resets_settings_but_keeps_the_ambient_chain(cats_yaml: Path) -> None:
    # arrange
    write_config(cats_yaml, make_config(accounts={"ambient": "111111111111"}))

    # act
    with override_config_path(None) as effective:
        accounts = list(Settings().accounts_under_test)

    # assert
    assert (effective, accounts) == (cats_yaml, ["ambient"])


def test_nested_overrides_are_reentrant(tmp_path: Path) -> None:
    # arrange
    outer = tmp_path / "outer.yaml"
    inner = tmp_path / "inner.yaml"
    write_config(outer, make_config(accounts={"fromOuter": "111111111111"}))
    write_config(inner, make_config(accounts={"fromInner": "222222222222"}))

    # act
    with override_config_path(outer):
        with override_config_path(inner):
            inner_accounts = list(Settings().accounts_under_test)
        outer_accounts = list(Settings().accounts_under_test)

    # assert
    assert (inner_accounts, outer_accounts) == (["fromInner"], ["fromOuter"])
