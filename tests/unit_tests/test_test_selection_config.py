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

"""Tests for the ``test_selection`` section of cats.yaml: CatsTestSelectionSettings, TestSelectionExclusion and
CatsConfig._validate_excluded_markers."""

from typing import Any

import pytest
from pydantic import ValidationError

from cats.config import CatsConfig, CatsReportingSettings


def _errors(exc_info: pytest.ExceptionInfo[ValidationError]) -> list[tuple[str, str]]:
    """Every (location, message) pair of a ValidationError, so a test states exactly what was rejected and why."""
    return [(".".join(str(part) for part in error["loc"]), str(error["msg"])) for error in exc_info.value.errors()]


def _config(
    excluded: list[dict[str, Any]] | None = None, provides: list[str] | None = None, custom_markers: list[str] | None = None
) -> dict[str, Any]:
    config: dict[str, Any] = {"test_selection": {}}
    if provides is not None:
        config["test_selection"]["provides"] = provides
    if excluded is not None:
        config["test_selection"]["excluded"] = excluded
    if custom_markers is not None:
        config["reporting"] = {"custom_markers": custom_markers}
    return config


def test_default_selects_nothing() -> None:
    # act
    config = CatsConfig.model_validate({})

    # assert
    expected: dict[str, list[Any]] = {"provides": [], "excluded": []}
    assert expected == config.test_selection.model_dump()


def test_full_block_loads_with_string_and_boolean_values() -> None:
    # arrange
    data = _config(
        provides=["container-deployment"],
        excluded=[
            {"marker": "standard_name", "values": ["ACME-G-SCP-34"], "reason": "RDS not in use; accepted by CISO 2026-09-01"},
            {"marker": "mutating", "values": [True], "reason": "Change freeze; read-only run until 2026-10-20"},
            {"marker": "cats_requires", "values": ["uc-plus"], "reason": "UC plus is not licensed here"},
        ],
        custom_markers=["mutating"],
    )

    # act
    config = CatsConfig.model_validate(data)

    # assert
    expected = {
        "provides": ["container-deployment"],
        "excluded": [
            {"marker": "standard_name", "values": ["ACME-G-SCP-34"], "reason": "RDS not in use; accepted by CISO 2026-09-01"},
            {"marker": "mutating", "values": [True], "reason": "Change freeze; read-only run until 2026-10-20"},
            {"marker": "cats_requires", "values": ["uc-plus"], "reason": "UC plus is not licensed here"},
        ],
    }
    assert expected == config.test_selection.model_dump()


def test_excluded_entry_without_reason_is_rejected() -> None:
    # act
    with pytest.raises(ValidationError) as exc_info:
        CatsConfig.model_validate(_config(excluded=[{"marker": "standard_name", "values": ["X"]}]))

    # assert
    expected = [("test_selection.excluded.0.reason", "Field required")]
    assert expected == _errors(exc_info)


def test_excluded_entry_with_blank_reason_is_rejected() -> None:
    """Whitespace is stripped first, so a reason of spaces is an empty reason."""
    # act
    with pytest.raises(ValidationError) as exc_info:
        CatsConfig.model_validate(_config(excluded=[{"marker": "standard_name", "values": ["X"], "reason": "   "}]))

    # assert
    expected = [("test_selection.excluded.0.reason", "String should have at least 1 character")]
    assert expected == _errors(exc_info)


def test_excluded_entry_with_empty_values_is_rejected() -> None:
    # act
    with pytest.raises(ValidationError) as exc_info:
        CatsConfig.model_validate(_config(excluded=[{"marker": "standard_name", "values": [], "reason": "x"}]))

    # assert
    expected = [("test_selection.excluded.0.values", "List should have at least 1 item after validation, not 0")]
    assert expected == _errors(exc_info)


def test_excluded_value_that_is_neither_string_nor_boolean_is_rejected() -> None:
    """``1`` is not ``true``: a marker value is a str or a bool, and YAML's integer must not be coerced to either."""
    # act
    with pytest.raises(ValidationError) as exc_info:
        CatsConfig.model_validate(_config(excluded=[{"marker": "standard_name", "values": [1], "reason": "x"}]))

    # assert
    expected = [
        ("test_selection.excluded.0.values.0.constrained-str", "Input should be a valid string"),
        ("test_selection.excluded.0.values.0.bool", "Input should be a valid boolean"),
    ]
    assert expected == _errors(exc_info)


def test_repeated_excluded_value_is_rejected() -> None:
    # act
    with pytest.raises(ValidationError) as exc_info:
        CatsConfig.model_validate(_config(excluded=[{"marker": "standard_name", "values": ["X", "X"], "reason": "x"}]))

    # assert
    expected = [("test_selection.excluded.0.values", "List must be unique")]
    assert expected == _errors(exc_info)


def test_marker_that_is_not_declared_is_rejected_with_the_nearest_name() -> None:
    """The read-only-run lever: a misspelled marker must be an error, never an exclusion that matches nothing."""
    # arrange
    expected_message = (
        "Value error, test_selection.excluded[0].marker 'mutatng' is not a marker a test can carry: "
        + "use standard_name, cats_requires, or a name declared in reporting.custom_markers (mutating, team); did you mean 'mutating'?"
    )

    # act
    with pytest.raises(ValidationError) as exc_info:
        CatsConfig.model_validate(_config(excluded=[{"marker": "mutatng", "values": [True], "reason": "x"}], custom_markers=["mutating", "team"]))

    # assert
    expected = [("", expected_message)]
    assert expected == _errors(exc_info)


def test_marker_with_no_custom_markers_declared_says_so() -> None:
    # arrange
    expected_message = (
        "Value error, test_selection.excluded[0].marker 'priority' is not a marker a test can carry: "
        + "use standard_name, cats_requires, or a name declared in reporting.custom_markers (none declared)"
    )

    # act
    with pytest.raises(ValidationError) as exc_info:
        CatsConfig.model_validate(_config(excluded=[{"marker": "priority", "values": ["high"], "reason": "x"}]))

    # assert
    expected = [("", expected_message)]
    assert expected == _errors(exc_info)


def test_engine_marker_name_is_rejected_in_favor_of_its_alias() -> None:
    """``cats_standard`` is the pytest mark; the config names it ``standard_name``, as the test author does."""
    # arrange
    expected_message = (
        "Value error, test_selection.excluded[0].marker 'cats_standard' is not a marker a test can carry: "
        + "use standard_name, cats_requires, or a name declared in reporting.custom_markers (none declared); did you mean 'standard_name'?"
    )

    # act
    with pytest.raises(ValidationError) as exc_info:
        CatsConfig.model_validate(_config(excluded=[{"marker": "cats_standard", "values": ["X"], "reason": "x"}]))

    # assert
    expected = [("", expected_message)]
    assert expected == _errors(exc_info)


def test_second_entry_is_named_by_its_position() -> None:
    # arrange
    data = _config(
        excluded=[
            {"marker": "standard_name", "values": ["X"], "reason": "x"},
            {"marker": "team", "values": ["platform"], "reason": "x"},
        ]
    )
    expected_message = (
        "Value error, test_selection.excluded[1].marker 'team' is not a marker a test can carry: "
        + "use standard_name, cats_requires, or a name declared in reporting.custom_markers (none declared)"
    )

    # act
    with pytest.raises(ValidationError) as exc_info:
        CatsConfig.model_validate(data)

    # assert
    expected = [("", expected_message)]
    assert expected == _errors(exc_info)


def test_requires_is_an_ordinary_custom_marker_name() -> None:
    """The engine mark is addressed as ``cats_requires``, so no name is taken from the custom-marker namespace for it."""
    # act
    settings = CatsReportingSettings(custom_markers=["requires"])

    # assert
    assert settings.custom_markers == ["requires"]


def test_requires_as_a_marker_is_hinted_to_cats_requires() -> None:
    """The decorator's name without its prefix is the likely slip; the hint names the mark."""
    # arrange
    expected_message = (
        "Value error, test_selection.excluded[0].marker 'requires' is not a marker a test can carry: "
        + "use standard_name, cats_requires, or a name declared in reporting.custom_markers (none declared); did you mean 'cats_requires'?"
    )

    # act
    with pytest.raises(ValidationError) as exc_info:
        CatsConfig.model_validate(_config(excluded=[{"marker": "requires", "values": ["uc-plus"], "reason": "x"}]))

    # assert
    expected = [("", expected_message)]
    assert expected == _errors(exc_info)


def test_empty_provides_entry_is_rejected() -> None:
    # act
    with pytest.raises(ValidationError) as exc_info:
        CatsConfig.model_validate(_config(provides=[""]))

    # assert
    expected = [("test_selection.provides.0", "String should have at least 1 character")]
    assert expected == _errors(exc_info)


def test_repeated_provides_entry_is_rejected() -> None:
    # act
    with pytest.raises(ValidationError) as exc_info:
        CatsConfig.model_validate(_config(provides=["a", "a"]))

    # assert
    expected = [("test_selection.provides", "List must be unique")]
    assert expected == _errors(exc_info)


def test_unknown_key_is_hinted() -> None:
    # act
    with pytest.raises(ValidationError) as exc_info:
        CatsConfig.model_validate({"test_selection": {"provide": ["a"]}})

    # assert
    expected = [("test_selection", "unknown key 'provide', did you mean 'provides'?")]
    assert expected == _errors(exc_info)
