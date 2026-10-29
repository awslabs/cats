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

"""Tests for inline IAM policy config validation and file-reference loading (_load_policy_document)."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cats.config import AccountSettings, CatsConfig
from tests.utils import write_config


def make_inline_policy_config(policies: dict[str, Any]) -> dict[str, Any]:
    """Build a minimal config dict with inline policies on global account settings."""
    return {
        "cats_deployment_settings": {"parent_account_id": "999999999999"},
        "global_account_settings": {"default_test_role_inline_policies": policies},
        "accounts_under_test": {"testaccount": {"account_id": "111111111111"}},
    }


def make_policy_document() -> dict[str, Any]:
    """Build a valid IAM policy document."""
    return {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": "*"}],
    }


def test_inline_document_parses(cats_yaml: Path) -> None:
    """A policy document written inline in yaml is accepted as-is."""
    # arrange
    document = make_policy_document()
    write_config(cats_yaml, make_inline_policy_config({"s3-read": document}))

    # act
    config = CatsConfig()

    # assert
    expected = {"s3-read": document}
    assert expected == config.global_account_settings.default_test_role_inline_policies


def test_file_reference_loads_relative_to_config(cats_yaml: Path) -> None:
    """A string value is loaded as a JSON file path resolved relative to cats.yaml."""
    # arrange
    document = make_policy_document()
    (cats_yaml.parent / "policy.json").write_text(json.dumps(document))
    write_config(cats_yaml, make_inline_policy_config({"from-file": "./policy.json"}))

    # act
    config = CatsConfig()

    # assert
    expected = {"from-file": document}
    assert expected == config.global_account_settings.default_test_role_inline_policies


def test_absolute_file_reference_loads(cats_yaml: Path) -> None:
    """An absolute path string value is loaded without resolving against cats.yaml."""
    # arrange
    document = make_policy_document()
    policy_path = cats_yaml.parent / "abs-policy.json"
    policy_path.write_text(json.dumps(document))
    write_config(cats_yaml, make_inline_policy_config({"from-abs-file": str(policy_path)}))

    # act
    config = CatsConfig()

    # assert
    expected = {"from-abs-file": document}
    assert expected == config.global_account_settings.default_test_role_inline_policies


def test_missing_policy_file_raises(cats_yaml: Path) -> None:
    """A file reference that does not exist fails validation naming both paths."""
    # act
    with pytest.raises(ValidationError) as excinfo:
        AccountSettings.model_validate({"default_test_role_inline_policies": {"nope": "./missing.json"}})

    # assert
    expected_msg = f"Value error, policy file './missing.json' not found at '{cats_yaml.parent}/missing.json'"
    assert expected_msg == excinfo.value.errors()[0]["msg"]


def test_invalid_json_policy_file_raises(cats_yaml: Path) -> None:
    """A file reference containing invalid JSON fails validation."""
    # arrange
    (cats_yaml.parent / "broken.json").write_text("not json {")

    # act
    with pytest.raises(ValidationError) as excinfo:
        AccountSettings.model_validate({"default_test_role_inline_policies": {"broken": "./broken.json"}})

    # assert
    expected_msg = f"Value error, policy file '{cats_yaml.parent}/broken.json' is not valid JSON: Expecting value: line 1 column 1 (char 0)"
    assert expected_msg == excinfo.value.errors()[0]["msg"]


def test_document_missing_statement_raises() -> None:
    """A policy document without a Statement key fails validation."""
    # act
    with pytest.raises(ValidationError) as excinfo:
        AccountSettings.model_validate({"default_test_role_inline_policies": {"bad": {"Version": "2012-10-17"}}})

    # assert
    expected_msg = "Value error, policy document must contain a 'Statement' key"
    assert expected_msg == excinfo.value.errors()[0]["msg"]


def test_non_object_document_raises() -> None:
    """A value that is neither an object nor a string fails validation."""
    # act
    with pytest.raises(ValidationError) as excinfo:
        AccountSettings.model_validate({"default_test_role_inline_policies": {"bad": 42}})

    # assert
    expected_msg = "Value error, policy document must be a JSON object, or a string path to a JSON file"
    assert expected_msg == excinfo.value.errors()[0]["msg"]


def test_invalid_policy_name_raises() -> None:
    """A policy name outside IAM's allowed character set fails validation."""
    # act
    with pytest.raises(ValidationError) as excinfo:
        AccountSettings.model_validate({"default_test_role_inline_policies": {"bad name!": make_policy_document()}})

    # assert
    expected_msg = "String should match pattern '^[A-Za-z0-9+=,.@_-]+$'"
    assert expected_msg == excinfo.value.errors()[0]["msg"]


def test_account_inherits_global_inline_policies(cats_yaml: Path) -> None:
    """Accounts without their own inline policies inherit the global setting."""
    # arrange
    document = make_policy_document()
    write_config(cats_yaml, make_inline_policy_config({"s3-read": document}))

    # act
    config = CatsConfig()

    # assert
    expected = {"s3-read": document}
    account = config.accounts_under_test["testaccount"]
    assert expected == account.account_settings.default_test_role_inline_policies
