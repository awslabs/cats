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

"""Tests for cats.config.Settings: an invalid cats.yaml surfaces as CatsConfigError, never as pydantic's ValidationError.

The invalid file is scoped with override_config_path on a path CATS_CONFIG does not name, so the
CATS plugin recording this very test never reads it.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from cats.config import Settings, override_config_path
from cats.exceptions import CatsConfigError
from tests.utils import write_config


def test_invalid_yaml_raises_cats_config_error_naming_the_file(isolated_yaml: Path) -> None:
    # arrange
    write_config(isolated_yaml, {"unknown_section": {"a": 1}})
    expected_message = f"""{isolated_yaml} is not a valid CATS configuration:
1 validation error for CatsConfig
  unknown key 'unknown_section' [type=unknown_key, input_value={{'unknown_section': {{'a': 1}}}}, input_type=dict]"""

    # act
    with override_config_path(isolated_yaml), pytest.raises(CatsConfigError) as exc_info:
        Settings()

    # assert
    assert str(exc_info.value) == expected_message


def test_invalid_yaml_error_chains_pydantics_report(isolated_yaml: Path) -> None:
    # arrange
    write_config(isolated_yaml, {"unknown_section": {"a": 1}})

    # act
    with override_config_path(isolated_yaml), pytest.raises(CatsConfigError) as exc_info:
        Settings()

    # assert
    assert isinstance(exc_info.value.__cause__, ValidationError) is True


def test_valid_yaml_loads(isolated_yaml: Path) -> None:
    # arrange
    write_config(isolated_yaml, {"cats_deployment_settings": {"aws_region": "eu-west-1"}})

    # act
    with override_config_path(isolated_yaml):
        region = Settings().cats_deployment_settings.aws_region

    # assert
    assert region == "eu-west-1"


def test_empty_file_raises_cats_config_error(isolated_yaml: Path) -> None:
    """An existing but empty cats.yaml is a mistake, not the no-config case; `cats validate` and the runtime say so alike."""
    # arrange
    isolated_yaml.write_text("")
    expected_message = (
        f"{isolated_yaml} is empty: a CATS configuration is a YAML mapping of top-level sections "
        + "(cats_deployment_settings, accounts_under_test, ...). Delete the file to run without one."
    )

    # act
    with override_config_path(isolated_yaml), pytest.raises(CatsConfigError) as exc_info:
        Settings()

    # assert
    assert str(exc_info.value) == expected_message


def test_list_document_raises_cats_config_error(isolated_yaml: Path) -> None:
    """A list root is a one-line CatsConfigError, not pydantic-settings' raw ValueError."""
    # arrange
    isolated_yaml.write_text("- accounts_under_test\n")
    expected_message = (
        f"{isolated_yaml} must be a YAML mapping of top-level sections (cats_deployment_settings, accounts_under_test, ...), got a list"
    )

    # act
    with override_config_path(isolated_yaml), pytest.raises(CatsConfigError) as exc_info:
        Settings()

    # assert
    assert str(exc_info.value) == expected_message
