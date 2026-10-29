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

"""Tests for cats.selection.select_items: which collected tests run, and the record of each one held back.

The items are real ``pytest.Function`` objects collected in-process by ``pytester.getitems``, which
generates items without running ``pytest_collection_modifyitems``, so the selection under test is
the only one applied.
"""

from pathlib import Path

import pytest

# Aliased: pytest would otherwise try to collect a module-level name starting with Test.
from cats.config import TestSelectionExclusion as Exclusion
from cats.selection import ConfigExclusionRecord, requirements_of, select_items
from tests.utils import make_config, write_config

TEST_SOURCE = """
from cats import cats, cats_requires


def test_base():
    pass


@cats(standard_name="ACME-G-SCP-34")
def test_standard():
    pass


@cats(standard_name="ACME-X-1", mutating=True)
def test_mutating_bool():
    pass


@cats(standard_name="ACME-X-2", mutating="true")
def test_mutating_string():
    pass


@cats(team=["platform", "security"])
def test_two_teams():
    pass


@cats_requires("container-deployment")
def test_container():
    pass


@cats_requires("uc-plus", "container-deployment")
def test_uc_plus_container():
    pass


@cats_requires("uc-plus")
@cats(standard_name="ACME-G-SCP-34")
def test_excluded_and_not_provided():
    pass


@cats_requires("uc-plus")
class TestGroup:
    def test_method(self):
        pass
"""


def _items(pytester: pytest.Pytester, cats_yaml: Path) -> dict[str, pytest.Item]:
    """The collected items of TEST_SOURCE by name, with the custom markers the source uses declared and reporting off.

    The inner session runs without syrupy and pytest-randomly: neither is wanted for collecting, and
    syrupy's session-finish hook fails on a session that only collected.
    """
    write_config(cats_yaml, make_config(reporting={"enabled": False, "custom_markers": ["mutating", "team"]}))
    module = pytester.getmodulecol(TEST_SOURCE, configargs=("-p", "no:syrupy", "-p", "no:randomly", "-p", "no:cacheprovider"))
    return {item.name: item for item in pytester.genitems([module])}


def _record(
    item: pytest.Item,
    kind: str,
    standards: list[str],
    requires: list[str],
    reason: str,
    excluded_index: int | None = None,
    marker: str | None = None,
    value: str | bool | None = None,
    not_provided: list[str] | None = None,
) -> ConfigExclusionRecord:
    return {
        "nodeid": item.nodeid,
        "standards": standards,
        "requires": requires,
        "kind": kind,  # type: ignore[typeddict-item]
        "excluded_index": excluded_index,
        "marker": marker,
        "value": value,
        "not_provided": not_provided or [],
        "reason": reason,
    }


def _exclusion(marker: str, *values: str | bool, reason: str = "the reason") -> Exclusion:
    return Exclusion(marker=marker, values=list(values), reason=reason)


def test_nothing_configured_keeps_every_item_in_order(pytester: pytest.Pytester, cats_yaml: Path) -> None:
    # arrange
    items = _items(pytester, cats_yaml)

    # act
    selection = select_items(list(items.values()), provided=set(), exclusions=[])

    # assert - the two cats_requires tests and the class are held back for want of a provider; everything else runs
    expected_kept = ["test_base", "test_standard", "test_mutating_bool", "test_mutating_string", "test_two_teams"]
    assert expected_kept == [item.name for item in selection.kept]


def test_base_test_always_runs(pytester: pytest.Pytester, cats_yaml: Path) -> None:
    # arrange
    items = _items(pytester, cats_yaml)

    # act
    selection = select_items([items["test_base"]], provided=set(), exclusions=[_exclusion("standard_name", "ACME-G-SCP-34")])

    # assert
    assert ([items["test_base"]], []) == (selection.kept, selection.deselected)


def test_requirement_provided_runs(pytester: pytest.Pytester, cats_yaml: Path) -> None:
    # arrange
    items = _items(pytester, cats_yaml)

    # act
    selection = select_items([items["test_container"]], provided={"container-deployment"}, exclusions=[])

    # assert
    assert ([items["test_container"]], []) == (selection.kept, selection.deselected)


def test_requirement_not_provided_is_recorded_with_the_gap_named(pytester: pytest.Pytester, cats_yaml: Path) -> None:
    """The acceptance case: requiring {a, b} with only a provided reads ``requires {a, b}; not provided {b}``."""
    # arrange
    items = _items(pytester, cats_yaml)
    item = items["test_uc_plus_container"]

    # act
    selection = select_items([item], provided={"container-deployment"}, exclusions=[])

    # assert
    expected = [
        (
            item,
            _record(
                item,
                kind="not_provided",
                standards=[],
                requires=["container-deployment", "uc-plus"],
                not_provided=["uc-plus"],
                reason="requires {container-deployment, uc-plus}; not provided {uc-plus}",
            ),
        )
    ]
    assert ([], expected) == (selection.kept, selection.deselected)


def test_class_level_requirement_applies_to_its_methods(pytester: pytest.Pytester, cats_yaml: Path) -> None:
    # arrange
    items = _items(pytester, cats_yaml)
    item = items["test_method"]

    # act
    selection = select_items([item], provided=set(), exclusions=[])

    # assert
    expected = [
        (
            item,
            _record(
                item,
                kind="not_provided",
                standards=[],
                requires=["uc-plus"],
                not_provided=["uc-plus"],
                reason="requires {uc-plus}; not provided {uc-plus}",
            ),
        )
    ]
    assert expected == selection.deselected


def test_excluded_by_standard_name(pytester: pytest.Pytester, cats_yaml: Path) -> None:
    # arrange
    items = _items(pytester, cats_yaml)
    item = items["test_standard"]
    exclusion = _exclusion("standard_name", "ACME-G-SCP-34", reason="RDS not in use; accepted by CISO 2026-09-01")

    # act
    selection = select_items([item], provided=set(), exclusions=[exclusion])

    # assert
    expected = [
        (
            item,
            _record(
                item,
                kind="excluded",
                standards=["ACME-G-SCP-34"],
                requires=[],
                excluded_index=0,
                marker="standard_name",
                value="ACME-G-SCP-34",
                reason="RDS not in use; accepted by CISO 2026-09-01",
            ),
        )
    ]
    assert ([], expected) == (selection.kept, selection.deselected)


def test_boolean_value_excludes_the_boolean_marker_and_not_the_string(pytester: pytest.Pytester, cats_yaml: Path) -> None:
    """``values: [true]`` deselects ``mutating=True`` and no other: ``mutating="true"`` is a different value."""
    # arrange
    items = _items(pytester, cats_yaml)
    bool_item, string_item = items["test_mutating_bool"], items["test_mutating_string"]
    exclusion = _exclusion("mutating", True, reason="Change freeze")

    # act
    selection = select_items([bool_item, string_item], provided=set(), exclusions=[exclusion])

    # assert
    expected_deselected = [
        (
            bool_item,
            _record(
                bool_item,
                kind="excluded",
                standards=["ACME-X-1"],
                requires=[],
                excluded_index=0,
                marker="mutating",
                value=True,
                reason="Change freeze",
            ),
        )
    ]
    assert ([string_item], expected_deselected) == (selection.kept, selection.deselected)


def test_excluded_by_one_of_a_list_valued_marker(pytester: pytest.Pytester, cats_yaml: Path) -> None:
    # arrange
    items = _items(pytester, cats_yaml)
    item = items["test_two_teams"]

    # act
    selection = select_items([item], provided=set(), exclusions=[_exclusion("team", "security")])

    # assert
    expected = [
        (item, _record(item, kind="excluded", standards=[], requires=[], excluded_index=0, marker="team", value="security", reason="the reason"))
    ]
    assert expected == selection.deselected


def test_excluded_by_cats_requires(pytester: pytest.Pytester, cats_yaml: Path) -> None:
    """``marker: cats_requires`` holds back every test declaring that requirement, even when it is provided."""
    # arrange
    items = _items(pytester, cats_yaml)
    item = items["test_container"]

    # act
    selection = select_items([item], provided={"container-deployment"}, exclusions=[_exclusion("cats_requires", "container-deployment")])

    # assert
    expected = [
        (
            item,
            _record(
                item,
                kind="excluded",
                standards=[],
                requires=["container-deployment"],
                excluded_index=0,
                marker="cats_requires",
                value="container-deployment",
                reason="the reason",
            ),
        )
    ]
    assert expected == selection.deselected


def test_excluded_wins_over_not_provided(pytester: pytest.Pytester, cats_yaml: Path) -> None:
    """A test that is both is recorded as excluded: the user's explicit, reasoned decision is the more informative fact."""
    # arrange
    items = _items(pytester, cats_yaml)
    item = items["test_excluded_and_not_provided"]

    # act
    selection = select_items([item], provided=set(), exclusions=[_exclusion("standard_name", "ACME-G-SCP-34")])

    # assert
    expected = [
        (
            item,
            _record(
                item,
                kind="excluded",
                standards=["ACME-G-SCP-34"],
                requires=["uc-plus"],
                excluded_index=0,
                marker="standard_name",
                value="ACME-G-SCP-34",
                reason="the reason",
            ),
        )
    ]
    assert expected == selection.deselected


def test_first_matching_entry_is_the_one_recorded(pytester: pytest.Pytester, cats_yaml: Path) -> None:
    # arrange
    items = _items(pytester, cats_yaml)
    item = items["test_mutating_bool"]
    exclusions = [_exclusion("standard_name", "ACME-X-1", reason="first"), _exclusion("mutating", True, reason="second")]

    # act
    selection = select_items([item], provided=set(), exclusions=exclusions)

    # assert
    expected = [
        (
            item,
            _record(
                item, kind="excluded", standards=["ACME-X-1"], requires=[], excluded_index=0, marker="standard_name", value="ACME-X-1", reason="first"
            ),
        )
    ]
    assert expected == selection.deselected


def test_entry_matching_nothing_holds_nothing_back(pytester: pytest.Pytester, cats_yaml: Path) -> None:
    # arrange
    items = _items(pytester, cats_yaml)
    item = items["test_standard"]

    # act
    selection = select_items([item], provided=set(), exclusions=[_exclusion("standard_name", "ACME-G-SCP-99")])

    # assert
    assert ([item], []) == (selection.kept, selection.deselected)


def test_requirements_of_reads_every_scope_sorted_and_once(pytester: pytest.Pytester, cats_yaml: Path) -> None:
    # arrange
    items = _items(pytester, cats_yaml)

    # act
    requires = requirements_of(items["test_uc_plus_container"])

    # assert
    assert requires == ["container-deployment", "uc-plus"]
