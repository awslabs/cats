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

"""Tests for dynamic fixture registration in pytest_plugin.py."""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import cats.pytest_plugin
from tests.utils import make_config, write_config


def test_single_account_creates_fixture(cats_yaml: Path) -> None:
    """Dynamic fixture is created for a single account in config."""
    # arrange
    write_config(cats_yaml, make_config(accounts={"testaccount": "234567890123"}))

    # act
    mock_pytest_config = MagicMock()
    cats.pytest_plugin.pytest_configure(mock_pytest_config)

    # assert
    assert "testaccount_account" in cats.pytest_plugin.__dict__


def test_multiple_accounts_create_fixtures(cats_yaml: Path) -> None:
    """Dynamic fixtures are created for all accounts in config."""
    # arrange
    accounts = {"alpha": "111111111111", "beta": "222222222222", "gamma": "333333333333"}
    write_config(cats_yaml, make_config(accounts=accounts))

    # act
    mock_pytest_config = MagicMock()
    cats.pytest_plugin.pytest_configure(mock_pytest_config)

    # assert
    expected_fixtures = ["alpha_account", "beta_account", "gamma_account"]
    for fixture_name in expected_fixtures:
        assert fixture_name in cats.pytest_plugin.__dict__


def test_fixture_has_correct_scope(cats_yaml: Path) -> None:
    """Fixture scope is read from global_account_settings.fixture_scope (inherited to account_settings)."""
    # arrange
    write_config(
        cats_yaml,
        make_config(accounts={"myaccount": "234567890123"}, fixture_scope="function"),
    )

    # act
    mock_pytest_config = MagicMock()
    cats.pytest_plugin.pytest_configure(mock_pytest_config)

    # assert
    fixture_func: Any = cats.pytest_plugin.__dict__["myaccount_account"]
    expected_scope = "function"
    assert fixture_func._fixture_function_marker.scope == expected_scope
