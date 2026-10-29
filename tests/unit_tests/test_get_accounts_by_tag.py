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

"""Tests for get_accounts_by_tag function."""

from pathlib import Path

from cats.constants import get_accounts_by_tag
from tests.utils import write_config


def test_key_only_match(cats_yaml: Path) -> None:
    """get_accounts_by_tag returns accounts with key-only tag."""
    # arrange
    write_config(
        cats_yaml,
        {
            "accounts_under_test": {
                "prod": {"account_id": "111111111111", "tags": {"north": None}},
                "dev": {"account_id": "222222222222", "tags": {"south": None}},
            }
        },
    )
    expected = ["prod_account"]

    # act
    result = get_accounts_by_tag("north")

    # assert
    assert result == expected


def test_key_value_match(cats_yaml: Path) -> None:
    """get_accounts_by_tag returns accounts with matching key-value tag."""
    # arrange
    write_config(
        cats_yaml,
        {
            "accounts_under_test": {
                "prod": {"account_id": "111111111111", "tags": {"env": "prod"}},
                "dev": {"account_id": "222222222222", "tags": {"env": "dev"}},
            }
        },
    )
    expected = ["prod_account"]

    # act
    result = get_accounts_by_tag("env", "prod")

    # assert
    assert result == expected


def test_key_value_mismatch(cats_yaml: Path) -> None:
    """get_accounts_by_tag excludes accounts with different tag value."""
    # arrange
    write_config(
        cats_yaml,
        {
            "accounts_under_test": {
                "prod": {"account_id": "111111111111", "tags": {"env": "prod"}},
                "dev": {"account_id": "222222222222", "tags": {"env": "dev"}},
            }
        },
    )
    expected: list[str] = []

    # act
    result = get_accounts_by_tag("env", "staging")

    # assert
    assert result == expected


def test_no_matches(cats_yaml: Path) -> None:
    """get_accounts_by_tag returns empty list when no accounts have tag."""
    # arrange
    write_config(
        cats_yaml,
        {
            "accounts_under_test": {
                "prod": {"account_id": "111111111111", "tags": {"env": "prod"}},
                "dev": {"account_id": "222222222222", "tags": {"env": "dev"}},
            }
        },
    )
    expected: list[str] = []

    # act
    result = get_accounts_by_tag("nonexistent")

    # assert
    assert result == expected


def test_key_exists_any_value(cats_yaml: Path) -> None:
    """get_accounts_by_tag with key only matches any value for that key."""
    # arrange
    write_config(
        cats_yaml,
        {
            "accounts_under_test": {
                "prod": {"account_id": "111111111111", "tags": {"env": "prod"}},
                "dev": {"account_id": "222222222222", "tags": {"env": "dev"}},
                "staging": {"account_id": "333333333333", "tags": {}},
            }
        },
    )
    expected = ["prod_account", "dev_account"]

    # act
    result = get_accounts_by_tag("env")

    # assert
    assert sorted(result) == sorted(expected)


def test_multiple_tags_on_account(cats_yaml: Path) -> None:
    """get_accounts_by_tag matches accounts with multiple tags."""
    # arrange
    write_config(
        cats_yaml,
        {
            "accounts_under_test": {
                "prod": {"account_id": "111111111111", "tags": {"env": "prod", "north": None, "critical": "true"}},
            }
        },
    )

    # act & assert
    assert get_accounts_by_tag("env", "prod") == ["prod_account"]
    assert get_accounts_by_tag("north") == ["prod_account"]
    assert get_accounts_by_tag("critical", "true") == ["prod_account"]
