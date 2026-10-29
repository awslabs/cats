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

"""Tests for cats.reporting.get_custom_markers: the values of the markers cats.yaml declares, in source order."""

from types import SimpleNamespace

import pytest
from pytest_mock import MockerFixture

from cats.reporting import get_custom_markers


def _item_with_marks(*marks: pytest.Mark) -> SimpleNamespace:
    """A stand-in for a collected test whose iter_markers yields ``marks`` closest-first, as pytest's does."""
    return SimpleNamespace(iter_markers=lambda name=None: [mark for mark in marks if name is None or mark.name == name])


def test_key_order_follows_the_decorators_not_set_iteration(mocker: MockerFixture) -> None:
    """report.json is diffable: custom_markers keys follow the decorators' order, not a set's iteration order."""
    # arrange
    mocker.patch("cats.reporting.get_custom_marker_names", return_value={"priority", "team", "ticket"})
    item = _item_with_marks(
        pytest.mark.ticket(kwarg="SEC-1").mark,  # closest to the function: applied last, so it is the last decorator in source order
        pytest.mark.priority(kwarg="high").mark,
        pytest.mark.team(kwarg="payments").mark,
    )

    # act
    result = get_custom_markers(item)  # type: ignore[arg-type]

    # assert
    assert (list(result), dict(result)) == (["team", "priority", "ticket"], {"team": ["payments"], "priority": ["high"], "ticket": ["SEC-1"]})


def test_undeclared_markers_are_not_recorded(mocker: MockerFixture) -> None:
    # arrange
    mocker.patch("cats.reporting.get_custom_marker_names", return_value={"team"})
    item = _item_with_marks(pytest.mark.xfail.mark, pytest.mark.team(kwarg="payments").mark)

    # act
    result = get_custom_markers(item)  # type: ignore[arg-type]

    # assert
    assert dict(result) == {"team": ["payments"]}
