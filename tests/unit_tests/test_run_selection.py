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

"""Tests for cats.report_run.RunSelection and selection_from_session."""

from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from cats.report_run import ConfiguredExclusionRecord, RunSelection, selection_from_session
from cats.selection import ConfigExclusionRecord


def make_selection(**overrides: Any) -> RunSelection:  # noqa: ANN401 - test helper forwarding dataclass fields
    fields: dict[str, Any] = {
        "keyword": "",
        "markexpr": "",
        "last_failed": False,
        "deselect": (),
        "explicit_args": (),
        "stopped_early": "",
    }
    fields.update(overrides)
    return RunSelection(**fields)


def make_session(
    mocker: MockerFixture,
    options: dict[str, object] | None = None,
    args_source: pytest.Config.ArgsSource = pytest.Config.ArgsSource.INVOCATION_DIR,
    args: list[str] | None = None,
    shouldfail: str | bool = False,
    shouldstop: str | bool = False,
) -> MagicMock:
    """A Session stand-in exposing only what selection_from_session reads."""
    defaults: dict[str, object] = {"keyword": "", "markexpr": "", "lf": False, "deselect": None}
    defaults.update(options or {})
    session: MagicMock = mocker.MagicMock()
    session.config.getoption.side_effect = lambda name, default=None: defaults.get(name, default)
    session.config.args_source = args_source
    session.config.args = args or ["/project"]
    session.shouldfail = shouldfail
    session.shouldstop = shouldstop
    return session


def test_session_without_selectors(mocker: MockerFixture) -> None:
    # act
    selection = selection_from_session(make_session(mocker), pytest.ExitCode.OK)

    # assert
    expected = make_selection()
    assert expected == selection


def test_every_field_round_trips_through_as_record() -> None:
    """``run.selection`` in report.json is the dataclass with tuples as lists; nothing is derived or judged."""
    # act
    record = make_selection(
        keyword="ssh",
        markexpr="not slow",
        last_failed=True,
        deselect=("tests/test_a.py::test_x",),
        explicit_args=("tests/test_a.py",),
        stopped_early="stopping after 1 failures",
    ).as_record()

    # assert
    expected = {
        "keyword": "ssh",
        "markexpr": "not slow",
        "last_failed": True,
        "deselect": ["tests/test_a.py::test_x"],
        "explicit_args": ["tests/test_a.py"],
        "stopped_early": "stopping after 1 failures",
        "provided": [],
        "config_excluded": [],
        "unmatched_exclusions": [],
    }
    assert expected == record


def test_keyword_and_marker_expressions_are_read_from_options(mocker: MockerFixture) -> None:
    # act
    selection = selection_from_session(make_session(mocker, options={"keyword": "ssh", "markexpr": "slow"}), pytest.ExitCode.OK)

    # assert
    expected = make_selection(keyword="ssh", markexpr="slow")
    assert expected == selection


def test_last_failed_and_deselect_are_read_from_options(mocker: MockerFixture) -> None:
    # act
    selection = selection_from_session(make_session(mocker, options={"lf": True, "deselect": ["a.py::test_x"]}), pytest.ExitCode.OK)

    # assert
    expected = make_selection(last_failed=True, deselect=("a.py::test_x",))
    assert expected == selection


def test_command_line_paths_count_as_explicit_args(mocker: MockerFixture) -> None:
    # arrange
    session = make_session(mocker, args_source=pytest.Config.ArgsSource.ARGS, args=["tests/test_a.py", "tests/test_b.py::test_y"])

    # act
    selection = selection_from_session(session, pytest.ExitCode.OK)

    # assert
    expected = make_selection(explicit_args=("tests/test_a.py", "tests/test_b.py::test_y"))
    assert expected == selection


def test_testpaths_from_ini_are_not_explicit_args(mocker: MockerFixture) -> None:
    """pytest fills config.args from the testpaths ini option when none are given; they were not typed, so they are not recorded as such."""
    # arrange
    session = make_session(mocker, args_source=pytest.Config.ArgsSource.TESTPATHS, args=["tests"])

    # act
    selection = selection_from_session(session, pytest.ExitCode.OK)

    # assert
    expected = make_selection()
    assert expected == selection


def test_options_owned_by_disabled_plugins_fall_back_to_their_defaults(mocker: MockerFixture) -> None:
    """-p no:cacheprovider removes --lf; reading it must not raise."""
    # arrange
    session = make_session(mocker, options={"lf": None})
    del_lf = {"keyword": "", "markexpr": "", "deselect": None}
    session.config.getoption.side_effect = lambda name, default=None: del_lf.get(name, default)

    # act
    selection = selection_from_session(session, pytest.ExitCode.OK)

    # assert
    expected = make_selection()
    assert expected == selection


def test_interrupted_session_stopped_early(mocker: MockerFixture) -> None:
    # act
    selection = selection_from_session(make_session(mocker), pytest.ExitCode.INTERRUPTED)

    # assert
    expected = make_selection(stopped_early="interrupted")
    assert expected == selection


def test_maxfail_stop_is_reported_from_shouldfail(mocker: MockerFixture) -> None:
    """-x / --maxfail set session.shouldfail with pytest's own reason text."""
    # act
    selection = selection_from_session(make_session(mocker, shouldfail="stopping after 1 failures"), pytest.ExitCode.TESTS_FAILED)

    # assert
    expected = make_selection(stopped_early="stopping after 1 failures")
    assert expected == selection


def test_shouldstop_is_reported_when_shouldfail_is_not_set(mocker: MockerFixture) -> None:
    # act
    selection = selection_from_session(make_session(mocker, shouldstop="plugin asked to stop"), pytest.ExitCode.OK)

    # assert
    expected = make_selection(stopped_early="plugin asked to stop")
    assert expected == selection


# ---------------------------------------------------------------------------
# test_selection: recorded in run.selection, zero-match entries derived
# ---------------------------------------------------------------------------


def make_record(nodeid: str, kind: str = "excluded", excluded_index: int | None = 0) -> ConfigExclusionRecord:
    return {
        "nodeid": nodeid,
        "standards": ["X"],
        "requires": [],
        "kind": kind,  # type: ignore[typeddict-item]
        "excluded_index": excluded_index,
        "marker": "standard_name" if kind == "excluded" else None,
        "value": "X" if kind == "excluded" else None,
        "not_provided": [] if kind == "excluded" else ["a"],
        "reason": "the reason",
    }


def test_record_carries_provided_exclusions_and_unmatched_entries() -> None:
    # arrange
    matched = make_record("t.py::x", excluded_index=0)
    selection = make_selection(
        provided=("a", "b"),
        config_excluded=(matched,),
        configured_exclusions=(
            {"marker": "standard_name", "values": ["X"], "reason": "first"},
            {"marker": "mutating", "values": [True], "reason": "second"},
        ),
    )

    # act
    record = selection.as_record()

    # assert
    expected = {
        "keyword": "",
        "markexpr": "",
        "last_failed": False,
        "deselect": [],
        "explicit_args": [],
        "stopped_early": "",
        "provided": ["a", "b"],
        "config_excluded": [matched],
        "unmatched_exclusions": [{"index": 1, "marker": "mutating", "values": [True], "reason": "second"}],
    }
    assert expected == record


def test_not_provided_records_match_no_exclusion_entry() -> None:
    """Only an excluded record claims an entry; a not_provided one leaves every entry unmatched."""
    # act
    selection = make_selection(
        config_excluded=(make_record("t.py::x", kind="not_provided", excluded_index=None),),
        configured_exclusions=({"marker": "standard_name", "values": ["X"], "reason": "r"},),
    )

    # assert
    expected = [{"index": 0, "marker": "standard_name", "values": ["X"], "reason": "r"}]
    assert expected == selection.unmatched_exclusions


def test_session_selection_sorts_provided_and_records_by_nodeid(mocker: MockerFixture) -> None:
    """Deterministic run.selection whatever order xdist workers reported in."""
    # arrange
    later, earlier = make_record("t.py::b"), make_record("t.py::a")
    configured: list[ConfiguredExclusionRecord] = [{"marker": "standard_name", "values": ["X"], "reason": "r"}]

    # act
    selection = selection_from_session(
        make_session(mocker), pytest.ExitCode.OK, provided={"b", "a"}, config_excluded=[later, earlier], configured_exclusions=configured
    )

    # assert
    expected = make_selection(provided=("a", "b"), config_excluded=(earlier, later), configured_exclusions=tuple(configured))
    assert expected == selection
