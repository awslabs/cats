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

"""Tests for cats.markers.cats_requires: one sorted ``cats_requires(name=...)`` mark per distinct requirement."""

import re
from collections.abc import Callable

import pytest

from cats.exceptions import CatsUsageError
from cats.markers import cats_requires

pytestmark = pytest.mark.filterwarnings("ignore::pytest.PytestUnknownMarkWarning")  # registration is the plugin's job in the user's session


def _marks(func: Callable[..., object]) -> list[tuple[str, str]]:
    """The (mark name, requirement name) of every pytest mark on a decorated function, sorted.

    Sorted, not in ``pytestmark`` order: pytest records marks in application order, which
    ``compose_decos`` reverses, and the selection reads them as a set.
    """
    raw: list[pytest.Mark] = getattr(func, "pytestmark", [])
    return sorted((mark.name, str(mark.kwargs["name"])) for mark in raw)


def test_one_requirement_applies_one_mark() -> None:
    # act
    @cats_requires("container-deployment")
    def decorated() -> None:
        pass

    # assert
    expected = [("cats_requires", "container-deployment")]
    assert expected == _marks(decorated)


def test_two_requirements_apply_one_mark_each() -> None:
    # act
    @cats_requires("uc-plus", "container-deployment")
    def decorated() -> None:
        pass

    # assert
    expected = [("cats_requires", "container-deployment"), ("cats_requires", "uc-plus")]
    assert expected == _marks(decorated)


def test_argument_order_does_not_change_the_marks() -> None:
    """The requirements are a set: ("a", "b") and ("b", "a") are the same declaration."""

    # act
    @cats_requires("a", "b")
    def forwards() -> None:
        pass

    @cats_requires("b", "a")
    def backwards() -> None:
        pass

    # assert
    assert _marks(forwards) == _marks(backwards)


def test_stacked_decorators_equal_one_combined_decorator() -> None:
    """Stacking unions the requirements, so two decorators carry what one would."""

    # act
    @cats_requires("a")
    @cats_requires("b")
    def stacked() -> None:
        pass

    @cats_requires("a", "b")
    def combined() -> None:
        pass

    # assert
    assert _marks(stacked) == _marks(combined)


def test_repeated_requirement_collapses_to_one_mark() -> None:
    # act
    @cats_requires("a", "a")
    def decorated() -> None:
        pass

    # assert
    expected = [("cats_requires", "a")]
    assert expected == _marks(decorated)


def test_surrounding_whitespace_is_stripped_like_the_config_side() -> None:
    """cats.yaml strips string values, so ``" a "`` on the test must name the same requirement as ``a`` in provides."""

    # act
    @cats_requires(" a ")
    def decorated() -> None:
        pass

    # assert
    expected = [("cats_requires", "a")]
    assert expected == _marks(decorated)


def test_no_requirement_raises_cats_usage_error() -> None:
    # arrange
    expected_message = "@cats_requires needs at least one requirement, e.g. @cats_requires('container-deployment')."

    # act / assert
    with pytest.raises(CatsUsageError, match=f"^{re.escape(expected_message)}$"):
        cats_requires()


def test_empty_string_raises_cats_usage_error() -> None:
    # arrange
    expected_message = "@cats_requires requirement '' must be a non-empty string."

    # act / assert
    with pytest.raises(CatsUsageError, match=f"^{re.escape(expected_message)}$"):
        cats_requires("")


def test_whitespace_only_string_raises_cats_usage_error() -> None:
    # arrange
    expected_message = "@cats_requires requirement '  ' must be a non-empty string."

    # act / assert
    with pytest.raises(CatsUsageError, match=f"^{re.escape(expected_message)}$"):
        cats_requires("  ")


def test_non_string_raises_cats_usage_error() -> None:
    # arrange
    expected_message = "@cats_requires requirement 7 must be a non-empty string."

    # act / assert
    with pytest.raises(CatsUsageError, match=f"^{re.escape(expected_message)}$"):
        cats_requires(7)  # type: ignore[arg-type]


def test_applies_to_a_class_like_any_mark() -> None:
    # act
    @cats_requires("container-deployment")
    class TestGroup:
        def test_inside(self) -> None:
            pass

    # assert
    expected = [("cats_requires", "container-deployment")]
    assert expected == _marks(TestGroup)
