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

"""Tests for cats.utils.yaml_utils.load_yaml: a repeated key is an error, not a silent override."""

from pathlib import Path

import pytest
from yaml.constructor import ConstructorError

from cats.cli.validate import validate_config
from cats.utils.yaml_utils import load_yaml

DUPLICATE_ACCOUNTS = """\
accounts_under_test:
  prod:
    account_id: "111111111111"
accounts_under_test:
  dev:
    account_id: "222222222222"
"""

DUPLICATE_ACCOUNTS_MESSAGE = """while constructing a mapping
  in "<unicode string>", line 1, column 1:
    accounts_under_test:
    ^
found duplicate key 'accounts_under_test'; a repeated key would silently replace the first
  in "<unicode string>", line 4, column 1:
    accounts_under_test:
    ^"""


def test_unique_keys_load_as_a_mapping() -> None:
    # act
    loaded = load_yaml("a: 1\nb:\n  c: 2\n")

    # assert
    assert loaded == {"a": 1, "b": {"c": 2}}


def test_duplicate_top_level_key_is_rejected() -> None:
    # act
    with pytest.raises(ConstructorError) as exc_info:
        load_yaml(DUPLICATE_ACCOUNTS)

    # assert
    assert str(exc_info.value) == DUPLICATE_ACCOUNTS_MESSAGE


def test_duplicate_nested_key_is_rejected() -> None:
    # arrange
    expected_message = """while constructing a mapping
  in "<unicode string>", line 3, column 5:
        account_id: "1"
        ^
found duplicate key 'account_id'; a repeated key would silently replace the first
  in "<unicode string>", line 4, column 5:
        account_id: "2"
        ^"""

    # act
    with pytest.raises(ConstructorError) as exc_info:
        load_yaml('accounts_under_test:\n  prod:\n    account_id: "1"\n    account_id: "2"\n')

    # assert
    assert str(exc_info.value) == expected_message


def test_validate_reports_the_duplicate_key(isolated_yaml: Path) -> None:
    """A duplicated account key is a validation failure, not a silent drop of one account from the evidence."""
    # arrange
    isolated_yaml.write_text(DUPLICATE_ACCOUNTS)
    expected_message = f"""YAML syntax error: while constructing a mapping
  in "{isolated_yaml}", line 1, column 1
found duplicate key 'accounts_under_test'; a repeated key would silently replace the first
  in "{isolated_yaml}", line 4, column 1"""

    # act
    valid, message, _ = validate_config(isolated_yaml)

    # assert
    assert (valid, message) == (False, expected_message)
