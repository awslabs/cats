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

"""Tests for cats.cfn.events module."""

from unittest.mock import MagicMock

import pytest

from cats.cfn.events import (
    TERMINAL_FAILURE,
    TERMINAL_SUCCESS,
    StackPoller,
    is_success,
    is_terminal,
)
from tests.utils import make_cfn_event

# =============================================================================
# is_terminal
# =============================================================================


@pytest.mark.parametrize(
    "status",
    sorted(TERMINAL_SUCCESS) + sorted(TERMINAL_FAILURE),
)
def test_is_terminal_returns_true_for_terminal_states(status: str) -> None:
    # act
    result = is_terminal(status)

    # assert
    assert result is True


@pytest.mark.parametrize(
    "status",
    ["CREATE_IN_PROGRESS", "UPDATE_IN_PROGRESS", "DELETE_IN_PROGRESS", "ROLLBACK_IN_PROGRESS"],
)
def test_is_terminal_returns_false_for_in_progress_states(status: str) -> None:
    # act
    result = is_terminal(status)

    # assert
    assert result is False


# =============================================================================
# is_success
# =============================================================================


@pytest.mark.parametrize("status", sorted(TERMINAL_SUCCESS))
def test_is_success_returns_true_for_success_states(status: str) -> None:
    # act
    result = is_success(status)

    # assert
    assert result is True


@pytest.mark.parametrize("status", sorted(TERMINAL_FAILURE))
def test_is_success_returns_false_for_failure_states(status: str) -> None:
    # act
    result = is_success(status)

    # assert
    assert result is False


# =============================================================================
# StackPoller
# =============================================================================
# StackPoller
# =============================================================================


def test_fetch_new_events_returns_new_events_only() -> None:
    # arrange
    mock_cfn = MagicMock()
    stack_name = "test-stack"
    poller = StackPoller(mock_cfn, stack_name)

    event1 = make_cfn_event("e1", "Resource1", "CREATE_IN_PROGRESS", "AWS::S3::Bucket")
    event2 = make_cfn_event("e2", "Resource2", "CREATE_IN_PROGRESS", "AWS::IAM::Role")
    event3 = make_cfn_event("e3", "Resource1", "CREATE_COMPLETE", "AWS::S3::Bucket")

    mock_cfn.describe_stack_events.side_effect = [
        {"StackEvents": [event2, event1]},  # newest first
        {"StackEvents": [event3, event2, event1]},  # includes old events
    ]

    # act
    first_batch = poller.fetch_new_events()
    second_batch = poller.fetch_new_events()

    # assert
    assert len(first_batch) == 2
    assert len(second_batch) == 1
    assert first_batch[0].logical_id == "Resource1"
    assert first_batch[1].logical_id == "Resource2"
    assert second_batch[0].logical_id == "Resource1"


def test_fetch_new_events_sets_final_status_on_terminal() -> None:
    # arrange
    mock_cfn = MagicMock()
    stack_name = "test-stack"
    poller = StackPoller(mock_cfn, stack_name)

    event_terminal = make_cfn_event("e1", stack_name, "CREATE_COMPLETE")
    mock_cfn.describe_stack_events.return_value = {"StackEvents": [event_terminal]}

    # act
    events = poller.fetch_new_events()

    # assert
    assert len(events) == 1
    assert poller.final_status == "CREATE_COMPLETE"
    assert poller.is_complete is True


def test_fetch_new_events_sets_final_status_on_failure() -> None:
    # arrange
    mock_cfn = MagicMock()
    stack_name = "test-stack"
    poller = StackPoller(mock_cfn, stack_name)

    event_terminal = make_cfn_event("e1", stack_name, "ROLLBACK_COMPLETE")
    mock_cfn.describe_stack_events.return_value = {"StackEvents": [event_terminal]}

    # act
    events = poller.fetch_new_events()

    # assert
    assert len(events) == 1
    assert poller.final_status == "ROLLBACK_COMPLETE"
    assert poller.is_complete is True


def test_fetch_new_events_returns_empty_when_complete() -> None:
    # arrange
    mock_cfn = MagicMock()
    stack_name = "test-stack"
    poller = StackPoller(mock_cfn, stack_name)

    event_terminal = make_cfn_event("e1", stack_name, "CREATE_COMPLETE")
    mock_cfn.describe_stack_events.return_value = {"StackEvents": [event_terminal]}

    # act
    poller.fetch_new_events()  # completes the stack
    second_call = poller.fetch_new_events()

    # assert
    assert second_call == []
    assert mock_cfn.describe_stack_events.call_count == 1  # no second API call


def test_is_complete_false_until_terminal_event() -> None:
    # arrange
    mock_cfn = MagicMock()
    stack_name = "test-stack"
    poller = StackPoller(mock_cfn, stack_name)

    # Resource completes but stack hasn't
    event1 = make_cfn_event("e1", "SomeResource", "CREATE_COMPLETE", "AWS::S3::Bucket")
    mock_cfn.describe_stack_events.return_value = {"StackEvents": [event1]}

    # act
    poller.fetch_new_events()

    # assert
    assert poller.is_complete is False
    assert poller.final_status is None
