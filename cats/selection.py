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

"""Config-driven test selection: which collected tests run against this deployment, and a record of each one that does not.

Two configured facts decide it, both in ``test_selection`` of cats.yaml (see
:class:`cats.config.CatsTestSelectionSettings`): what the deployment ``provides``, matched against
each test's ``@cats_requires`` marks, and the ``excluded`` entries, matched against a marker value.
This module is pure: :func:`select_items` decides from the items' marks; the pytest plugin applies
the decision in ``pytest_collection_modifyitems`` and records every held-back test as ``not_run``.

A test held back here is recorded, not dropped. It was in scope by the command line and out of
scope by configuration, and the report says so, test by test, with the reason; a test ``-k`` or ``-m``
removed leaves no entry at all.
"""

from collections.abc import Sequence, Set
from dataclasses import dataclass, field
from typing import Literal, TypedDict

import pytest

from cats.config import TestSelectionExclusion
from cats.reporting import get_custom_markers, get_standard_markers

# The two engine markers test_selection.excluded names as they are written on the test (see TEST_SELECTION_MARKER_ALIASES).
_STANDARD_NAME = "standard_name"
_REQUIRES = "cats_requires"


class ConfigExclusionRecord(TypedDict):
    """One test ``test_selection`` held back: an element of ``run.selection.config_excluded`` in report.json.

    Every key is always present.

    - ``nodeid``, ``standards``, ``requires``: the test, its ``@cats`` standards and its
      ``@cats_requires`` requirements (sorted).
    - ``kind``: ``excluded`` (it matched a ``test_selection.excluded`` entry) or ``not_provided``
      (it requires something ``provides`` does not list).
    - ``excluded_index``, ``marker``, ``value``: the matching entry, by its position in
      ``test_selection.excluded``, with the marker and the value that matched; all ``null`` for
      ``not_provided``.
    - ``not_provided``: the requirements the test declares that are not provided, sorted; ``[]``
      for ``excluded``.
    - ``reason``: the entry's own ``reason`` for ``excluded``; ``requires {a, b}; not provided {b}``
      for ``not_provided``. The same text is the test's ``not_run`` reason in every report format.
    """

    nodeid: str
    standards: list[str]
    requires: list[str]
    kind: Literal["excluded", "not_provided"]
    excluded_index: int | None
    marker: str | None
    value: str | bool | None
    not_provided: list[str]
    reason: str


@dataclass
class Selection:
    """What :func:`select_items` decided: the items that run, and each held-back item with its record."""

    kept: list[pytest.Item] = field(default_factory=list)
    deselected: list[tuple[pytest.Item, ConfigExclusionRecord]] = field(default_factory=list)


def requirements_of(item: pytest.Item) -> list[str]:
    """The requirements a test declares: its ``cats_requires`` marks at every scope, sorted, each once."""
    names: set[str] = {mark.kwargs["name"] for mark in item.iter_markers(name="cats_requires") if "name" in mark.kwargs}
    return sorted(names)


def not_provided_reason(requires: Sequence[str], missing: Sequence[str]) -> str:
    """``requires {a, b}; not provided {b}``: the gap, named."""
    return f"requires {{{', '.join(requires)}}}; not provided {{{', '.join(missing)}}}"


def _marker_values(marker: str, standards: list[str], requires: list[str], custom_markers: dict[str, list[object]]) -> list[object]:
    """The values a test carries for one marker name, as ``test_selection.excluded`` names markers."""
    if marker == _STANDARD_NAME:
        return list(standards)
    if marker == _REQUIRES:
        return list(requires)
    return list(custom_markers.get(marker, []))


def _exclusion_for(item: pytest.Function, provided: Set[str], exclusions: Sequence[TestSelectionExclusion]) -> ConfigExclusionRecord | None:
    """Why this test does not run, or None when it does.

    An ``excluded`` entry is checked first: a test that is both excluded and not provided is
    recorded as excluded, the user's explicit, reasoned decision being the more informative fact.
    Values match by equality, so a configured ``true`` matches ``@cats(mutating=True)`` and not
    ``@cats(mutating="true")``.
    """
    standards = get_standard_markers(item)
    requires = requirements_of(item)
    custom_markers: dict[str, list[object]] = dict(get_custom_markers(item))
    for index, exclusion in enumerate(exclusions):
        carried = _marker_values(exclusion.marker, standards, requires, custom_markers)
        for value in exclusion.values:
            if value in carried:
                return ConfigExclusionRecord(
                    nodeid=item.nodeid,
                    standards=standards,
                    requires=requires,
                    kind="excluded",
                    excluded_index=index,
                    marker=exclusion.marker,
                    value=value,
                    not_provided=[],
                    reason=exclusion.reason,
                )
    missing = sorted(set(requires) - provided)
    if len(missing) > 0:
        return ConfigExclusionRecord(
            nodeid=item.nodeid,
            standards=standards,
            requires=requires,
            kind="not_provided",
            excluded_index=None,
            marker=None,
            value=None,
            not_provided=missing,
            reason=not_provided_reason(requires, missing),
        )
    return None


def select_items(items: Sequence[pytest.Item], provided: Set[str], exclusions: Sequence[TestSelectionExclusion]) -> Selection:
    """Split the collected items into those that run and those ``test_selection`` holds back, in collection order.

    Only test functions are subject to selection; any other item kind is kept. A test with no
    ``cats_requires`` mark and no matching exclusion always runs.
    """
    selection = Selection()
    for item in items:
        record = _exclusion_for(item, provided, exclusions) if isinstance(item, pytest.Function) else None
        if record is None:
            selection.kept.append(item)
        else:
            selection.deselected.append((item, record))
    return selection
