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

"""Tests for CatsReportingSettings._validate_custom_markers: a custom marker must work as a @cats keyword and a pytest marker."""

import pytest
from pydantic import ValidationError

from cats.config import CatsReportingSettings


def _error_message(exc_info: pytest.ExceptionInfo[ValidationError]) -> str:
    return str(exc_info.value.errors()[0]["msg"])


def test_valid_names_are_kept_in_order() -> None:
    # act
    settings = CatsReportingSettings(custom_markers=["team", "priority", "owner"])

    # assert
    assert settings.custom_markers == ["team", "priority", "owner"]


def test_name_with_a_hyphen_is_rejected() -> None:
    # arrange
    expected_message = (
        "Value error, reporting.custom_markers: 'team-name' is not a valid Python identifier; a custom marker is passed to @cats as a keyword."
    )

    # act
    with pytest.raises(ValidationError) as exc_info:
        CatsReportingSettings(custom_markers=["team-name"])

    # assert
    assert _error_message(exc_info) == expected_message


def test_python_keyword_is_rejected() -> None:
    # arrange
    expected_message = (
        "Value error, reporting.custom_markers: 'class' is not a valid Python identifier; a custom marker is passed to @cats as a keyword."
    )

    # act
    with pytest.raises(ValidationError) as exc_info:
        CatsReportingSettings(custom_markers=["class"])

    # assert
    assert _error_message(exc_info) == expected_message


def test_cats_prefix_is_rejected() -> None:
    # arrange
    expected_message = "Value error, reporting.custom_markers: 'cats_severity' starts with 'cats_', the prefix reserved for CATS's own markers."

    # act
    with pytest.raises(ValidationError) as exc_info:
        CatsReportingSettings(custom_markers=["cats_severity"])

    # assert
    assert _error_message(exc_info) == expected_message


@pytest.mark.parametrize("name", ["skip", "skipif", "xfail", "parametrize", "usefixtures", "filterwarnings"])
def test_pytest_builtin_marker_is_rejected(name: str) -> None:
    # arrange
    expected_message = f"Value error, reporting.custom_markers: '{name}' is a pytest built-in marker; apply @pytest.mark.{name} directly."

    # act
    with pytest.raises(ValidationError) as exc_info:
        CatsReportingSettings(custom_markers=[name])

    # assert
    assert _error_message(exc_info) == expected_message


@pytest.mark.parametrize("name", ["standard_name", "control_ids", "control_group"])
def test_cats_keyword_is_rejected(name: str) -> None:
    # arrange
    expected_message = f"Value error, reporting.custom_markers: '{name}' is a @cats keyword, not a custom marker."

    # act
    with pytest.raises(ValidationError) as exc_info:
        CatsReportingSettings(custom_markers=[name])

    # assert
    assert _error_message(exc_info) == expected_message


def test_duplicate_name_is_rejected() -> None:
    # arrange
    expected_message = "Value error, reporting.custom_markers: 'team' is listed more than once."

    # act
    with pytest.raises(ValidationError) as exc_info:
        CatsReportingSettings(custom_markers=["team", "owner", "team"])

    # assert
    assert _error_message(exc_info) == expected_message
