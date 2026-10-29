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

"""Tests for CatsConfig.require_aws_region: the deploy Region is required unless AWS_REGION fills it."""

import re
from pathlib import Path

import pytest

from cats.config import AWS_REGION_UNSET_MESSAGE, CatsConfig
from cats.exceptions import CatsConfigError
from tests.utils import write_config


def test_unset_region_without_env_loads_as_none(cats_yaml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # arrange
    monkeypatch.delenv("AWS_REGION", raising=False)
    write_config(cats_yaml, {})

    # act
    config = CatsConfig()

    # assert
    assert config.cats_deployment_settings.aws_region is None


def test_require_aws_region_raises_when_unset(cats_yaml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # arrange
    monkeypatch.delenv("AWS_REGION", raising=False)
    write_config(cats_yaml, {})
    config = CatsConfig()
    expected_message = (
        "cats_deployment_settings.aws_region is not set. CATS deploys its stacks into and connects its test sessions "
        + "to that Region: set the key in cats.yaml, or export AWS_REGION."
    )

    # act & assert
    with pytest.raises(CatsConfigError, match=f"^{re.escape(expected_message)}$"):
        config.require_aws_region()


def test_require_aws_region_message_is_the_shared_constant() -> None:
    # arrange
    expected_message = (
        "cats_deployment_settings.aws_region is not set. CATS deploys its stacks into and connects its test sessions "
        + "to that Region: set the key in cats.yaml, or export AWS_REGION."
    )

    # assert
    assert expected_message == AWS_REGION_UNSET_MESSAGE


def test_require_aws_region_returns_the_configured_region(cats_yaml: Path) -> None:
    # arrange
    write_config(cats_yaml, {"cats_deployment_settings": {"aws_region": "eu-west-1"}})
    config = CatsConfig()

    # act
    region = config.require_aws_region()

    # assert
    assert region == "eu-west-1"


def test_require_aws_region_returns_the_environment_fallback(cats_yaml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # arrange
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
    write_config(cats_yaml, {})
    config = CatsConfig()

    # act
    region = config.require_aws_region()

    # assert
    assert region == "ap-southeast-2"
