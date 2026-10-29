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

"""Tests for account tags in config."""

from pathlib import Path

from cats.config import Settings
from tests.utils import write_config


def test_account_tags_from_config(cats_yaml: Path) -> None:
    """Account config tags are accessible via Settings."""
    # arrange
    write_config(
        cats_yaml,
        {
            "accounts_under_test": {
                "prod": {
                    "account_id": "111111111111",
                    "tags": {"env": "prod", "north": None},
                }
            }
        },
    )
    expected = {"env": "prod", "north": None}

    # act
    result = Settings().accounts_under_test["prod"].tags

    # assert
    assert result == expected


def test_account_tags_empty_by_default(cats_yaml: Path) -> None:
    """Account with no tags returns empty dict."""
    # arrange
    write_config(
        cats_yaml,
        {"accounts_under_test": {"prod": {"account_id": "111111111111"}}},
    )
    expected: dict[str, str | None] = {}

    # act
    result = Settings().accounts_under_test["prod"].tags

    # assert
    assert result == expected


def test_network_tags_from_config(cats_yaml: Path) -> None:
    """Network config tags are accessible via Settings."""
    # arrange
    write_config(
        cats_yaml,
        {
            "accounts_under_test": {
                "prod": {
                    "account_id": "111111111111",
                    "account_network_under_test": {
                        "private": {
                            "vpc_id": "vpc-123",
                            "subnet_ids": ["subnet-456"],
                            "tags": {"egress": "restricted"},
                        }
                    },
                }
            }
        },
    )
    expected = {"egress": "restricted"}

    # act
    result = Settings().accounts_under_test["prod"].account_network_under_test["private"].tags

    # assert
    assert result == expected


def test_network_tags_empty_by_default(cats_yaml: Path) -> None:
    """Network with no tags returns empty dict."""
    # arrange
    write_config(
        cats_yaml,
        {
            "accounts_under_test": {
                "prod": {
                    "account_id": "111111111111",
                    "account_network_under_test": {
                        "private": {
                            "vpc_id": "vpc-123",
                            "subnet_ids": ["subnet-456"],
                        }
                    },
                }
            }
        },
    )
    expected: dict[str, str | None] = {}

    # act
    result = Settings().accounts_under_test["prod"].account_network_under_test["private"].tags

    # assert
    assert result == expected
