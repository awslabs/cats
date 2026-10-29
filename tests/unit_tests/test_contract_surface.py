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

"""Pins the covered surface declared on docs/concepts/versioning.md.

This file mirrors that page: the export list and the reserved-name sets it states are
asserted here as literals. A change to this file is a contract change, and needs the page
and a version decision to go with it.
"""

import inspect

import pytest

import cats
from cats.config import CATS_DECORATOR_KEYWORDS, PYTEST_BUILTIN_MARKERS, RESERVED_NETWORK_TARGET_NAMES, RESERVED_ROLE_ATTRIBUTE_NAMES
from cats.markers import cats as cats_decorator
from cats.report_run import REPORT_SCHEMA_VERSION
from cats.resources.session_interact import SessionHandler


def test_public_exports_are_exactly_the_covered_list() -> None:
    # arrange
    expected = [
        "AccountUnderTest",
        "CatsConfig",
        "CatsConfigError",
        "CatsError",
        "CatsInfrastructureError",
        "CatsLambdaInvocationError",
        "CatsNetworkResponse",
        "CatsNetworkResult",
        "CatsReporter",
        "CatsSSMError",
        "CatsTestPack",
        "CatsUsageError",
        "CatsVersionMismatchError",
        "GetAccountFixture",
        "NetworkHandler",
        "ShellResult",
        "StandardMetadata",
        "__version__",
        "cats",
        "cats_requires",
        "get_account_fixture_names",
        "get_accounts_by_tag",
    ]

    # act
    exports = sorted(cats.__all__)

    # assert
    assert expected == exports


def test_reserved_network_target_names_are_frozen() -> None:
    # arrange
    expected = frozenset({"name", "partition", "session", "role", "tags"})

    # assert
    assert expected == RESERVED_NETWORK_TARGET_NAMES


def test_reserved_role_names_are_exactly_default() -> None:
    """SessionHandler has one public name; every other attribute is underscore-private."""
    # act
    public_names = {name for name, _ in inspect.getmembers(SessionHandler) if name.startswith("_") is False}

    # assert
    assert (RESERVED_ROLE_ATTRIBUTE_NAMES, public_names) == (frozenset({"default"}), {"default"})


def test_cats_decorator_keywords_are_frozen() -> None:
    """@cats never gains a keyword; every other keyword is a declared custom marker."""
    # act
    parameters = inspect.signature(cats_decorator).parameters
    keyword_only = {name for name, parameter in parameters.items() if parameter.kind is inspect.Parameter.KEYWORD_ONLY}

    # assert
    assert (CATS_DECORATOR_KEYWORDS, keyword_only) == (
        frozenset({"standard_name", "control_ids", "control_group"}),
        {"standard_name", "control_ids", "control_group"},
    )


def test_pytest_builtin_markers_reserved_from_custom_markers() -> None:
    # arrange
    expected = frozenset({"skip", "skipif", "xfail", "parametrize", "usefixtures", "filterwarnings"})

    # assert
    assert expected == PYTEST_BUILTIN_MARKERS


def test_plugin_fixtures_are_cats_report_and_get_account_fixture() -> None:
    """CATS's own fixtures; every account fixture is <keyname>_account and no future CATS fixture ends in _account."""
    # arrange
    import cats.pytest_plugin as plugin

    # act - <keyname>_account fixtures are registered into the module once a config has loaded; they are the scheme, not CATS's own
    fixtures = sorted(name for name, value in vars(plugin).items() if hasattr(value, "_pytestfixturefunction") or hasattr(value, "_fixture_function"))
    own_fixtures = [name for name in fixtures if name.endswith("_account") is False]

    # assert
    assert own_fixtures == ["cats_report", "get_account_fixture"]


def test_the_marker_cats_emits_is_cats_standard() -> None:
    # act
    @cats_decorator(standard_name="SOC2-CC6.6")
    def decorated() -> None:
        pass

    marks: list[pytest.Mark] = decorated.pytestmark  # type: ignore[attr-defined]

    # assert
    assert [mark.name for mark in marks] == ["cats_standard"]


def test_report_schema_version_is_one() -> None:
    # assert
    assert REPORT_SCHEMA_VERSION == 1
