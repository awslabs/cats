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

"""Tests for cats.cli.progress module."""

from unittest.mock import MagicMock, call

from pytest_mock import MockerFixture
from rich.console import Console, Group

from cats.cli.infra_ui.progress import (
    SingleAccountProgress,
    format_elapsed,
)
from tests.utils import make_stack_event

# =============================================================================
# format_elapsed
# =============================================================================


def test_format_elapsed_seconds_only() -> None:
    # act
    result = format_elapsed(45.7)

    # assert
    assert result == "45s"


def test_format_elapsed_minutes_and_seconds() -> None:
    # act
    result = format_elapsed(83.2)

    # assert
    assert result == "1m 23s"


def test_format_elapsed_zero() -> None:
    # act
    result = format_elapsed(0)

    # assert
    assert result == "0s"


def test_format_elapsed_exact_minute() -> None:
    # act
    result = format_elapsed(120)

    # assert
    assert result == "2m 0s"


# =============================================================================
# SingleAccountProgress
# =============================================================================


def test_single_account_progress_collects_events(mock_console: MagicMock) -> None:
    # arrange
    progress = SingleAccountProgress(
        account_name="test",
        account_id="123456789012",
        stack_name="test-stack",
        operation="Deploy",
        console=mock_console,
    )

    # act
    progress.on_event(make_stack_event("Resource1", "CREATE_IN_PROGRESS"))
    progress.on_event(make_stack_event("Resource2", "CREATE_IN_PROGRESS"))

    # assert
    assert len(progress.resource_events) == 2
    assert "Resource1" in progress.resource_events
    assert "Resource2" in progress.resource_events


def test_single_account_progress_updates_in_place(mock_console: MagicMock) -> None:
    # arrange
    progress = SingleAccountProgress(
        account_name="test",
        account_id="123456789012",
        stack_name="test-stack",
        operation="Deploy",
        console=mock_console,
    )

    # act
    progress.on_event(make_stack_event("Resource1", "CREATE_IN_PROGRESS"))
    progress.on_event(make_stack_event("Resource1", "CREATE_COMPLETE"))

    # assert - only one entry, updated in place
    assert len(progress.resource_events) == 1
    assert progress.resource_events["Resource1"].status == "CREATE_COMPLETE"


def test_single_account_progress_updates_status_on_stack_event(mock_console: MagicMock) -> None:
    # arrange
    progress = SingleAccountProgress(
        account_name="test",
        account_id="123456789012",
        stack_name="test-stack",
        operation="Deploy",
        console=mock_console,
    )

    # act
    progress.on_event(make_stack_event("test-stack", "CREATE_IN_PROGRESS", "AWS::CloudFormation::Stack"))

    # assert
    assert progress.current_status == "CREATE_IN_PROGRESS"


def test_single_account_progress_tracks_resource_completion(mock_console: MagicMock) -> None:
    # arrange
    progress = SingleAccountProgress(
        account_name="test",
        account_id="123456789012",
        stack_name="test-stack",
        operation="Deploy",
        console=mock_console,
        known_resource_count=5,
    )

    # act
    progress.on_event(make_stack_event("Resource1", "CREATE_COMPLETE"))
    progress.on_event(make_stack_event("Resource2", "CREATE_COMPLETE"))
    progress.on_event(make_stack_event("Resource3", "CREATE_IN_PROGRESS"))

    # assert
    assert len(progress.resources_complete) == 2
    assert progress.resources_total == 5  # Known count unchanged


def test_single_account_progress_ignores_stack_in_resource_count(mock_console: MagicMock) -> None:
    # arrange
    progress = SingleAccountProgress(
        account_name="test",
        account_id="123456789012",
        stack_name="test-stack",
        operation="Deploy",
        console=mock_console,
    )

    # act
    progress.on_event(make_stack_event("Resource1", "CREATE_COMPLETE"))
    progress.on_event(make_stack_event("test-stack", "CREATE_COMPLETE", "AWS::CloudFormation::Stack"))

    # assert
    assert len(progress.resources_complete) == 1


def test_single_account_progress_ignores_rollback_in_resource_count(mock_console: MagicMock) -> None:
    # arrange
    progress = SingleAccountProgress(
        account_name="test",
        account_id="123456789012",
        stack_name="test-stack",
        operation="Deploy",
        console=mock_console,
    )

    # act
    progress.on_event(make_stack_event("Resource1", "CREATE_COMPLETE"))
    progress.on_event(make_stack_event("Resource2", "ROLLBACK_COMPLETE"))

    # assert
    assert len(progress.resources_complete) == 1


def test_single_account_progress_dynamic_total(mock_console: MagicMock) -> None:
    # arrange
    progress = SingleAccountProgress(
        account_name="test",
        account_id="123456789012",
        stack_name="test-stack",
        operation="Deploy",
        console=mock_console,
    )

    # act
    progress.on_event(make_stack_event("Resource1", "CREATE_IN_PROGRESS"))
    progress.on_event(make_stack_event("Resource2", "CREATE_IN_PROGRESS"))
    progress.on_event(make_stack_event("Resource3", "CREATE_IN_PROGRESS"))

    # assert - total updates dynamically
    assert progress.resources_total == 3


def test_single_account_progress_context_manager_no_live_when_not_terminal(mock_console: MagicMock) -> None:
    # act
    with SingleAccountProgress(
        account_name="test",
        account_id="123456789012",
        stack_name="test-stack",
        operation="Deploy",
        console=mock_console,
    ) as progress:
        pass

    # assert
    assert progress.live is None


def test_single_account_progress_tracks_failed_resources_with_reason(mock_console: MagicMock) -> None:
    # arrange
    progress = SingleAccountProgress(
        account_name="test",
        account_id="123456789012",
        stack_name="test-stack",
        operation="Deploy",
        console=mock_console,
    )
    event_with_reason = make_stack_event(
        logical_id="MyLambda",
        resource_type="AWS::Lambda::Function",
        status="CREATE_FAILED",
        status_reason="Function not found",
    )
    event_without_reason = make_stack_event(
        logical_id="MyBucket",
        resource_type="AWS::S3::Bucket",
        status="CREATE_FAILED",
        status_reason=None,
    )

    # act
    progress.on_event(event_with_reason)
    progress.on_event(event_without_reason)

    # assert - only tracks failures WITH a reason
    assert len(progress.failed_resources) == 1
    assert progress.failed_resources[0].logical_id == "MyLambda"
    assert progress.failed_resources[0].status_reason == "Function not found"


def text_rows(progress: SingleAccountProgress) -> list[str]:
    """The plain-string rows of the rendered panel: the status line, then any notice."""
    group = progress._render().renderable
    assert isinstance(group, Group)
    return [row for row in group.renderables if isinstance(row, str)]


def test_single_account_progress_set_notice_shows_message_in_panel(mock_console: MagicMock) -> None:
    # arrange - deleting a dead stack produces no stack events, so the panel would sit silent
    progress = SingleAccountProgress(
        account_name="test",
        account_id="123456789012",
        stack_name="test-stack",
        operation="Deploy",
        console=mock_console,
    )
    message = "Deleting unrecoverable stack test-stack (ROLLBACK_COMPLETE) before recreating"
    expected = [
        "[yellow]Stack Status: STARTING[/yellow]  Resources: 0",
        f"[yellow]{message}[/yellow]",
    ]

    # act
    progress.set_notice(message)

    # assert
    assert expected == text_rows(progress)


def test_single_account_progress_notice_containing_rich_markup_is_shown_verbatim(mock_console: MagicMock) -> None:
    """A notice carrying AWS error text is data, not markup: '[sts:AssumeRole]' must survive rendering, not vanish as a style tag."""
    # arrange
    progress = SingleAccountProgress(
        account_name="test",
        account_id="123456789012",
        stack_name="test-stack",
        operation="Deploy",
        console=mock_console,
    )
    message = "Waiting on AccessDenied: not authorized to perform [sts:AssumeRole] on the test role"

    # act
    progress.set_notice(message)
    rendered = Console(width=200, color_system=None, force_terminal=False).render_str(text_rows(progress)[1]).plain

    # assert
    assert rendered == message


def test_single_account_progress_stack_event_clears_notice(mock_console: MagicMock) -> None:
    # arrange - the recreated stack is reporting events, so "Deleting ... " is no longer true
    progress = SingleAccountProgress(
        account_name="test",
        account_id="123456789012",
        stack_name="test-stack",
        operation="Deploy",
        console=mock_console,
    )
    progress.set_notice("Deleting unrecoverable stack test-stack (ROLLBACK_COMPLETE) before recreating")
    expected = ["[yellow]Stack Status: CREATE_IN_PROGRESS[/yellow]  Resources: 0"]

    # act
    progress.on_event(make_stack_event("test-stack", "CREATE_IN_PROGRESS", "AWS::CloudFormation::Stack"))

    # assert
    assert expected == text_rows(progress)


# =============================================================================
# SingleAccountProgress plain mode (non-TTY, e.g. CI)
# =============================================================================


def make_single_progress(mock_console: MagicMock) -> SingleAccountProgress:
    """Create a SingleAccountProgress against the non-terminal mock console."""
    return SingleAccountProgress(
        account_name="test",
        account_id="123456789012",
        stack_name="test-stack",
        operation="Deploy",
        console=mock_console,
    )


def test_plain_enter_prints_starting_line(mock_console: MagicMock) -> None:
    # arrange
    expected = [call("[Deploy] test (123456789012): starting stack test-stack", markup=False, highlight=False, soft_wrap=True)]

    # act
    with make_single_progress(mock_console):
        pass

    # assert
    assert expected == mock_console.print.call_args_list


def test_plain_stack_in_progress_prints_status_line(mock_console: MagicMock) -> None:
    # arrange
    progress = make_single_progress(mock_console)
    expected = [call("[Deploy] test (123456789012): CREATE_IN_PROGRESS", markup=False, highlight=False, soft_wrap=True)]

    # act
    progress.on_event(make_stack_event("test-stack", "CREATE_IN_PROGRESS", "AWS::CloudFormation::Stack"))

    # assert
    assert expected == mock_console.print.call_args_list


def test_plain_repeated_stack_status_prints_once(mock_console: MagicMock) -> None:
    # arrange
    progress = make_single_progress(mock_console)
    expected = [call("[Deploy] test (123456789012): CREATE_IN_PROGRESS", markup=False, highlight=False, soft_wrap=True)]

    # act
    progress.on_event(make_stack_event("test-stack", "CREATE_IN_PROGRESS", "AWS::CloudFormation::Stack"))
    progress.on_event(make_stack_event("test-stack", "CREATE_IN_PROGRESS", "AWS::CloudFormation::Stack"))

    # assert
    assert expected == mock_console.print.call_args_list


def test_plain_resource_complete_prints_progress_count(mock_console: MagicMock) -> None:
    # arrange - two resources seen, so the dynamic total is 2
    progress = make_single_progress(mock_console)
    progress.on_event(make_stack_event("Bucket1", "CREATE_IN_PROGRESS"))
    progress.on_event(make_stack_event("Bucket2", "CREATE_IN_PROGRESS"))
    expected = [call("[Deploy] test (123456789012): Bucket1 CREATE_COMPLETE (1/2 resources)", markup=False, highlight=False, soft_wrap=True)]

    # act
    progress.on_event(make_stack_event("Bucket1", "CREATE_COMPLETE"))

    # assert
    assert expected == mock_console.print.call_args_list


def test_plain_duplicate_resource_complete_prints_once(mock_console: MagicMock) -> None:
    # arrange
    progress = make_single_progress(mock_console)
    expected = [call("[Deploy] test (123456789012): Bucket1 CREATE_COMPLETE (1/1 resources)", markup=False, highlight=False, soft_wrap=True)]

    # act
    progress.on_event(make_stack_event("Bucket1", "CREATE_COMPLETE"))
    progress.on_event(make_stack_event("Bucket1", "CREATE_COMPLETE"))

    # assert
    assert expected == mock_console.print.call_args_list


def test_plain_resource_failure_prints_reason(mock_console: MagicMock) -> None:
    # arrange
    progress = make_single_progress(mock_console)
    expected = [call("[Deploy] test (123456789012): MyLambda CREATE_FAILED: Function not found", markup=False, highlight=False, soft_wrap=True)]

    # act
    progress.on_event(
        make_stack_event(
            "MyLambda",
            "CREATE_FAILED",
            resource_type="AWS::Lambda::Function",
            status_reason="Function not found",
        )
    )

    # assert
    assert expected == mock_console.print.call_args_list


def test_plain_terminal_stack_status_prints_elapsed(mock_console: MagicMock, mocker: MockerFixture) -> None:
    # arrange
    fake_monotonic = mocker.patch("cats.cli.infra_ui.progress.time.monotonic")
    fake_monotonic.return_value = 100.0
    progress = make_single_progress(mock_console)
    fake_monotonic.return_value = 352.0
    expected = [call("[Deploy] test (123456789012): CREATE_COMPLETE in 4m 12s", markup=False, highlight=False, soft_wrap=True)]

    # act
    progress.on_event(make_stack_event("test-stack", "CREATE_COMPLETE", "AWS::CloudFormation::Stack"))

    # assert
    assert expected == mock_console.print.call_args_list


def test_plain_set_notice_prints_message(mock_console: MagicMock) -> None:
    # arrange
    progress = make_single_progress(mock_console)
    message = "Deleting unrecoverable stack test-stack (ROLLBACK_COMPLETE) before recreating"
    expected = [call(f"[Deploy] test (123456789012): {message}", markup=False, highlight=False, soft_wrap=True)]

    # act
    progress.set_notice(message)

    # assert
    assert expected == mock_console.print.call_args_list


def test_plain_on_notice_prints_listener_step(mock_console: MagicMock) -> None:
    # arrange
    progress = make_single_progress(mock_console)
    message = "EC2 listener i-0abc (private) ready"
    expected = [call(f"[Deploy] test (123456789012): {message}", markup=False, highlight=False, soft_wrap=True)]

    # act
    progress.on_notice(message)

    # assert
    assert expected == mock_console.print.call_args_list


def test_terminal_on_notice_prints_indented_line_under_the_panel() -> None:
    # arrange - the panel has printed its final render by the time listeners come up; the step goes under it
    terminal_console = MagicMock()
    terminal_console.is_terminal = True
    progress = SingleAccountProgress(
        account_name="test", account_id="123456789012", stack_name="test-stack", operation="Deploy", console=terminal_console
    )
    expected = [call("  EC2 listener i-0abc (private) ready", markup=False, highlight=False, soft_wrap=True)]

    # act
    progress.on_notice("EC2 listener i-0abc (private) ready")

    # assert
    assert expected == terminal_console.print.call_args_list


def test_terminal_console_suppresses_plain_lines() -> None:
    # arrange - a real terminal gets the Live panel, not plain lines
    terminal_console = MagicMock()
    terminal_console.is_terminal = True
    progress = SingleAccountProgress(
        account_name="test",
        account_id="123456789012",
        stack_name="test-stack",
        operation="Deploy",
        console=terminal_console,
    )

    # act - not entered as a context manager, so no Live exists either
    progress.on_event(make_stack_event("test-stack", "CREATE_IN_PROGRESS", "AWS::CloudFormation::Stack"))

    # assert
    assert terminal_console.print.call_args_list == []


def test_plain_resource_progress_with_known_total() -> None:
    # arrange
    console = MagicMock()
    console.is_terminal = False
    progress = SingleAccountProgress(
        account_name="test",
        account_id="123456789012",
        stack_name="test-stack",
        operation="Deploy",
        console=console,
        known_resource_count=14,
    )
    progress.resources_complete.add("Bucket1")

    # act
    result = progress._plain_resource_progress()

    # assert
    assert result == "1/14 resources"


def test_plain_resource_progress_without_total(mock_console: MagicMock) -> None:
    # arrange - no events seen yet, so the dynamic total is unknown
    progress = make_single_progress(mock_console)

    # act
    result = progress._plain_resource_progress()

    # assert
    assert result == "0 resources"


def test_plain_resource_complete_with_known_total_prints_known_denominator(mock_console: MagicMock) -> None:
    # arrange - a known resource count is used as the denominator, not the events seen
    console = MagicMock()
    console.is_terminal = False
    progress = SingleAccountProgress(
        account_name="test",
        account_id="123456789012",
        stack_name="test-stack",
        operation="Deploy",
        console=console,
        known_resource_count=14,
    )
    expected = [call("[Deploy] test (123456789012): Bucket1 CREATE_COMPLETE (1/14 resources)", markup=False, highlight=False, soft_wrap=True)]

    # act
    progress.on_event(make_stack_event("Bucket1", "CREATE_COMPLETE"))

    # assert
    assert expected == console.print.call_args_list


def test_plain_stack_failure_with_reason_prints_reason_then_terminal_line(mock_console: MagicMock, mocker: MockerFixture) -> None:
    # arrange - a stack-level failure carrying a reason produces both lines
    fake_monotonic = mocker.patch("cats.cli.infra_ui.progress.time.monotonic")
    fake_monotonic.return_value = 100.0
    progress = make_single_progress(mock_console)
    fake_monotonic.return_value = 130.0
    expected = [
        call("[Deploy] test (123456789012): test-stack CREATE_FAILED: Stack creation failed", markup=False, highlight=False, soft_wrap=True),
        call("[Deploy] test (123456789012): CREATE_FAILED in 30s", markup=False, highlight=False, soft_wrap=True),
    ]

    # act
    progress.on_event(
        make_stack_event(
            "test-stack",
            "CREATE_FAILED",
            resource_type="AWS::CloudFormation::Stack",
            status_reason="Stack creation failed",
        )
    )

    # assert
    assert expected == mock_console.print.call_args_list
