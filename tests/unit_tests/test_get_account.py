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

"""Tests for cats.resources.get_account factory."""

import os
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest
from pytest_mock import MockerFixture

from cats.config import Settings
from cats.exceptions import CatsConfigError
from cats.resources import get_account
from tests.utils import make_config, write_config


@pytest.fixture
def mock_aut(mocker: MockerFixture) -> MagicMock:
    """Patch AccountUnderTest; each call to it returns a fresh sentinel."""
    mock: MagicMock = mocker.patch("cats.resources.AccountUnderTest")
    mock.side_effect = lambda name: SimpleNamespace(name=name, account_id=Settings().accounts_under_test[name].account_id)
    return mock


def test_calls_account_under_test_with_the_name(cats_yaml: Path, mock_aut: MagicMock) -> None:
    # arrange
    write_config(cats_yaml, make_config(account_name="a", account_id="111111111111"))

    # act
    get_account(None, "a")

    # assert
    assert mock_aut.call_args == call("a")


def test_none_config_path_uses_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mock_aut: MagicMock) -> None:
    # arrange
    config_file = tmp_path / "cats.yaml"
    monkeypatch.setenv("CATS_CONFIG", str(config_file))
    write_config(config_file, make_config(account_name="a", account_id="111111111111"))

    # act
    account = get_account(None, "a")

    # assert
    assert account.account_id == "111111111111"


def test_explicit_config_path_overrides_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mock_aut: MagicMock) -> None:
    # arrange
    env_file = tmp_path / "env.yaml"
    explicit_file = tmp_path / "explicit.yaml"
    write_config(env_file, make_config(account_name="a", account_id="111111111111"))
    write_config(explicit_file, make_config(account_name="a", account_id="222222222222"))
    monkeypatch.setenv("CATS_CONFIG", str(env_file))

    # act
    account = get_account(str(explicit_file), "a")

    # assert
    assert account.account_id == "222222222222"


def test_caches_on_identical_call(cats_yaml: Path, mock_aut: MagicMock) -> None:
    # arrange
    write_config(cats_yaml, make_config(account_name="a", account_id="111111111111"))

    # act
    first = get_account(None, "a")
    second = get_account(None, "a")

    # assert
    assert first is second
    assert mock_aut.call_count == 1


def test_cache_deduplicates_equivalent_paths(cats_yaml: Path, monkeypatch: pytest.MonkeyPatch, mock_aut: MagicMock) -> None:
    # arrange
    write_config(cats_yaml, make_config(account_name="a", account_id="111111111111"))
    monkeypatch.chdir(cats_yaml.parent)
    monkeypatch.delenv("CATS_CONFIG", raising=False)

    # act
    via_none = get_account(None, "a")
    via_relative = get_account("./cats.yaml", "a")

    # assert
    assert via_none is via_relative
    assert mock_aut.call_count == 1


def test_cache_invalidates_on_mtime_change(cats_yaml: Path, mock_aut: MagicMock) -> None:
    # arrange
    write_config(cats_yaml, make_config(account_name="a", account_id="111111111111"))

    # act
    first = get_account(None, "a")
    stat = cats_yaml.stat()
    os.utime(cats_yaml, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    second = get_account(None, "a")

    # assert
    assert first is not second
    assert mock_aut.call_count == 2


def test_different_name_constructs_new_instance(cats_yaml: Path, mock_aut: MagicMock) -> None:
    # arrange
    write_config(
        cats_yaml,
        make_config(accounts={"a": "111111111111", "b": "222222222222"}),
    )

    # act
    first = get_account(None, "a")
    second = get_account(None, "b")

    # assert
    assert first is not second
    assert mock_aut.call_count == 2


def test_missing_config_raises_cats_config_error(tmp_path: Path) -> None:
    # arrange
    missing = tmp_path / "nope.yaml"

    # act / assert
    with pytest.raises(CatsConfigError, match=f"Config file not found: {missing}"):
        get_account(str(missing), "a")


def test_unknown_account_raises_cats_config_error_with_available_names(cats_yaml: Path) -> None:
    # arrange
    write_config(cats_yaml, make_config(accounts={"a": "111111111111", "b": "222222222222"}))
    expected_message = f"Account 'nope' not found in {cats_yaml.resolve()}. Available accounts: a, b"

    # act / assert
    with pytest.raises(CatsConfigError, match=f"^{re.escape(expected_message)}$"):
        get_account(None, "nope")
