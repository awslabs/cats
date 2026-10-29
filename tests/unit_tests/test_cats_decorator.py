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

"""Tests for the @cats decorator widening — control_ids and control_group kwargs."""

import re
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import pytest

from cats.exceptions import CatsUsageError
from cats.markers import cats
from tests.utils import make_config, write_config

T = TypeVar("T", bound=Callable[..., object])


def _marks(func: T) -> list[pytest.Mark]:
    """Return the pytest markers attached to a decorated function."""
    raw: list[pytest.Mark] = getattr(func, "pytestmark", [])
    return raw


def _standard_marks(func: T) -> list[pytest.Mark]:
    """Return only the 'cats_standard' markers from a decorated function."""
    return [m for m in _marks(func) if m.name == "cats_standard"]


def _first_standard_marker(func: T) -> pytest.Mark:
    """Return the single 'cats_standard' marker attached to a decorated function."""
    marks = _standard_marks(func)
    assert len(marks) == 1, f"expected one standard marker, got {len(marks)}"
    return marks[0]


def test_no_control_kwargs_marker_has_only_name() -> None:
    """@cats without control_ids / control_group attaches only name on the standard marker."""

    # arrange / act
    @cats(standard_name="SC-7")
    def decorated() -> None:
        pass

    mark = _first_standard_marker(decorated)

    # assert
    expected_kwargs = {"name": "SC-7"}
    assert mark.kwargs == expected_kwargs


def test_control_ids_kwarg_attaches_to_standard_marker() -> None:
    """control_ids is attached as a kwarg on the standard marker, not a separate marker."""

    # arrange / act
    @cats(standard_name="ACME-G-DCP-1", control_ids=["SC-7", "AC-3"])
    def decorated() -> None:
        pass

    mark = _first_standard_marker(decorated)

    # assert
    expected_kwargs = {"name": "ACME-G-DCP-1", "control_ids": ["SC-7", "AC-3"]}
    assert mark.kwargs == expected_kwargs


def test_control_group_kwarg_attaches_to_standard_marker() -> None:
    """control_group is attached as a kwarg on the standard marker."""

    # arrange / act
    @cats(standard_name="ACME-G-DCP-1", control_group="Governance")
    def decorated() -> None:
        pass

    mark = _first_standard_marker(decorated)

    # assert
    expected_kwargs = {"name": "ACME-G-DCP-1", "control_group": "Governance"}
    assert mark.kwargs == expected_kwargs


def test_both_control_kwargs_attach_together() -> None:
    """control_ids and control_group can both be set on the same standard marker."""

    # arrange / act
    @cats(standard_name="ACME-G-DCP-1", control_ids=["SC-7"], control_group="Governance")
    def decorated() -> None:
        pass

    mark = _first_standard_marker(decorated)

    # assert
    expected_kwargs = {
        "name": "ACME-G-DCP-1",
        "control_ids": ["SC-7"],
        "control_group": "Governance",
    }
    assert mark.kwargs == expected_kwargs


def test_list_standard_name_applies_control_ids_to_each() -> None:
    """A list of standard_name values attaches the same control_ids to each marker."""

    # arrange / act
    @cats(standard_name=["A", "B"], control_ids=["SC-7"])
    def decorated() -> None:
        pass

    standard_marks = _standard_marks(decorated)
    names = sorted(m.kwargs["name"] for m in standard_marks)
    control_ids_lists = [m.kwargs.get("control_ids") for m in standard_marks]

    # assert
    expected_names = ["A", "B"]
    expected_control_ids_lists = [["SC-7"], ["SC-7"]]
    assert names == expected_names
    assert control_ids_lists == expected_control_ids_lists


def test_control_kwargs_without_standard_name_raises_cats_usage_error() -> None:
    """control_ids / control_group describe a standard; passing them without one is a caller mistake, not a warning."""
    # arrange
    expected_message = "@cats was passed control_ids or control_group without standard_name; they describe a standard, so name one."

    # act / assert
    with pytest.raises(CatsUsageError, match=f"^{re.escape(expected_message)}$"):
        cats(control_ids=["SC-7"])


def test_undeclared_keyword_raises_cats_usage_error(cats_yaml: Path) -> None:
    """A keyword that is neither a CATS keyword nor a declared custom marker fails at import instead of being dropped."""
    # arrange
    write_config(cats_yaml, make_config(reporting={"custom_markers": ["team"]}))
    expected_message = (
        "@cats keyword 'standard_nam' is not a CATS keyword (control_group, control_ids, standard_name) "
        + f"and is not declared in reporting.custom_markers of {cats_yaml} (declared: team)."
    )

    # act / assert
    with pytest.raises(CatsUsageError, match=f"^{re.escape(expected_message)}$"):
        cats(standard_nam="SOC2-CC6.6")


def test_undeclared_keyword_with_no_custom_markers_says_none_declared(cats_yaml: Path) -> None:
    # arrange
    write_config(cats_yaml, make_config())
    expected_message = (
        "@cats keyword 'team' is not a CATS keyword (control_group, control_ids, standard_name) "
        + f"and is not declared in reporting.custom_markers of {cats_yaml} (declared: (none))."
    )

    # act / assert
    with pytest.raises(CatsUsageError, match=f"^{re.escape(expected_message)}$"):
        cats(team="platform")


def test_pytest_marker_names_are_undeclared_custom_markers(cats_yaml: Path) -> None:
    """slow, skip and skipreason are pytest markers, not @cats keywords: undeclared, they are rejected like any other keyword."""
    # arrange
    write_config(cats_yaml, make_config())
    expected_message = (
        "@cats keyword 'slow' is not a CATS keyword (control_group, control_ids, standard_name) "
        + f"and is not declared in reporting.custom_markers of {cats_yaml} (declared: (none))."
    )

    # act / assert
    with pytest.raises(CatsUsageError, match=f"^{re.escape(expected_message)}$"):
        cats(slow=True)


def test_custom_marker_value_of_another_type_raises_cats_usage_error(cats_yaml: Path) -> None:
    # arrange
    write_config(cats_yaml, make_config(reporting={"custom_markers": ["priority"]}))
    expected_message = "@cats custom marker 'priority' must be a str, a bool or a list of str, got int."

    # act / assert
    with pytest.raises(CatsUsageError, match=f"^{re.escape(expected_message)}$"):
        cats(priority=3)  # type: ignore[arg-type]


@pytest.mark.filterwarnings("ignore::pytest.PytestUnknownMarkWarning")  # registration is the plugin's job in the user's session
def test_custom_marker_without_a_standard_attaches_only_the_custom_marker(cats_yaml: Path) -> None:
    """@cats(team=...) on a test that attests to nothing is a first-class case."""
    # arrange
    write_config(cats_yaml, make_config(reporting={"custom_markers": ["team"]}))

    # act
    @cats(team="platform")
    def decorated() -> None:
        pass

    # assert
    expected_marks = [("team", {"kwarg": "platform"})]
    assert [(m.name, m.kwargs) for m in _marks(decorated)] == expected_marks


def test_stacked_decorators_produce_separate_standard_markers() -> None:
    """Stacking @cats applies one standard marker per decorator, each with its own kwargs."""

    # arrange / act
    @cats(standard_name="A", control_ids=["SC-7"], control_group="Governance")
    @cats(standard_name="B", control_ids=["AC-3"], control_group="Identity")
    def decorated() -> None:
        pass

    standard_marks = _standard_marks(decorated)
    by_name = {m.kwargs["name"]: m.kwargs for m in standard_marks}

    # assert
    expected_a = {"name": "A", "control_ids": ["SC-7"], "control_group": "Governance"}
    expected_b = {"name": "B", "control_ids": ["AC-3"], "control_group": "Identity"}
    assert by_name["A"] == expected_a
    assert by_name["B"] == expected_b


# ---------------------------------------------------------------------------
# control_ids validation: a non-empty string everywhere; an OSCAL token when oscal_ar is configured
# ---------------------------------------------------------------------------

OSCAL_TOKEN_RULE = (
    "is not an OSCAL token, which reporting.report_formats oscal_ar needs for a finding target-id: it must start with "
    + "a letter or '_' and contain only letters, digits, '.', '-', '_'. Spaces and parentheses are not allowed - "
    + "use the catalog convention (e.g. 'ac-2.1' for AC-2(1))"
)


def _configure_oscal(cats_yaml: Path) -> None:
    write_config(cats_yaml, make_config(reporting={"report_formats": ["cats_json", "oscal_ar"]}))


def test_control_id_with_parentheses_rejected_with_oscal_output(cats_yaml: Path) -> None:
    # arrange
    _configure_oscal(cats_yaml)

    # act / assert
    with pytest.raises(CatsUsageError, match=re.escape(f"@cats control_ids entry 'AC-2(1)' {OSCAL_TOKEN_RULE}")):
        cats(standard_name="SOC2-CC6.6", control_ids=["AC-2(1)"])


def test_control_id_with_space_rejected_with_oscal_output(cats_yaml: Path) -> None:
    # arrange
    _configure_oscal(cats_yaml)

    # act / assert
    with pytest.raises(CatsUsageError, match=re.escape(f"@cats control_ids entry 'SC-7 (5)' {OSCAL_TOKEN_RULE}")):
        cats(standard_name="SOC2-CC6.6", control_ids=["SC-7 (5)"])


def test_control_id_starting_with_digit_rejected_with_oscal_output(cats_yaml: Path) -> None:
    # arrange
    _configure_oscal(cats_yaml)

    # act / assert
    with pytest.raises(CatsUsageError, match=re.escape(f"@cats control_ids entry '800-53' {OSCAL_TOKEN_RULE}")):
        cats(standard_name="SOC2-CC6.6", control_ids=["800-53"])


def test_control_ids_in_catalog_notation_accepted_without_oscal_output(cats_yaml: Path) -> None:
    """Only the OSCAL writer has a grammar; report.json, JUnit and Security Hub carry the ids as written."""
    # arrange
    write_config(cats_yaml, make_config(reporting={"report_formats": ["cats_json", "junit"]}))

    # act
    @cats(standard_name="CIS-1.1.1", control_ids=["AC-2(1)", "1.1.1", "SC-7 (5)"])
    def decorated() -> None:
        pass

    # assert
    assert _first_standard_marker(decorated).kwargs["control_ids"] == ["AC-2(1)", "1.1.1", "SC-7 (5)"]


def test_control_id_empty_string_rejected_in_every_format() -> None:
    # act / assert
    with pytest.raises(CatsUsageError, match=re.escape("@cats control_ids entry '' must be a non-empty string")):
        cats(standard_name="SOC2-CC6.6", control_ids=[""])


def test_control_id_that_is_not_a_string_rejected_in_every_format() -> None:
    # act / assert
    with pytest.raises(CatsUsageError, match=re.escape("@cats control_ids entry 7 must be a non-empty string")):
        cats(standard_name="SOC2-CC6.6", control_ids=[7])  # type: ignore[list-item]


def test_duplicate_control_ids_raise_value_error() -> None:
    # act / assert
    with pytest.raises(CatsUsageError, match="@cats control_ids contains duplicate entry 'AC-3'"):
        cats(standard_name="SOC2-CC6.6", control_ids=["AC-3", "AC-3"])


def test_catalog_convention_control_ids_accepted() -> None:
    # act
    decorator = cats(standard_name="SOC2-CC6.6", control_ids=["AC-3", "sc-7.5", "_internal-1"])

    # assert
    def dummy() -> None: ...

    marked = decorator(dummy)
    marker = marked.pytestmark[0]  # type: ignore[attr-defined]
    assert marker.kwargs["control_ids"] == ["AC-3", "sc-7.5", "_internal-1"]


def test_empty_standard_name_list_raises_cats_usage_error() -> None:
    """@cats(standard_name=[]) attached no mark and silently dropped the test from the compliance evidence."""
    # act / assert
    with pytest.raises(CatsUsageError, match=re.escape("@cats standard_name is an empty list; name at least one standard or omit standard_name.")):
        cats(standard_name=[])


@pytest.mark.parametrize("standard_name", ["", "   "])
def test_empty_standard_name_raises_cats_usage_error(standard_name: str) -> None:
    """A mark named "" would be attached but no report could key on it; control ids are already required to be non-empty."""
    # act / assert
    with pytest.raises(CatsUsageError, match=re.escape("@cats standard_name is empty; name a standard or omit standard_name.")):
        cats(standard_name=standard_name)


def test_standard_name_list_with_an_empty_name_raises_cats_usage_error() -> None:
    # act / assert
    with pytest.raises(CatsUsageError, match=re.escape("@cats standard_name contains an empty name; every element must name a standard.")):
        cats(standard_name=["NIST-800-53", ""])


def test_standard_name_list_with_a_non_string_raises_cats_usage_error() -> None:
    # act / assert
    with pytest.raises(CatsUsageError, match=re.escape("@cats standard_name must be a str or a list of str; list element 7 is int.")):
        cats(standard_name=["SOC2-CC6.6", 7])  # type: ignore[list-item]


def test_standard_name_of_another_type_raises_cats_usage_error() -> None:
    # act / assert
    with pytest.raises(CatsUsageError, match=re.escape("@cats standard_name must be a str or a list of str, got tuple.")):
        cats(standard_name=("SOC2-CC6.6",))  # type: ignore[arg-type]
