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

"""Tests for MultiAccountProgress."""

from unittest.mock import MagicMock, call

from pytest_mock import MockerFixture

from cats.cfn.deploy import DeployResult
from cats.cli.infra_ui.progress import MultiAccountProgress
from tests.utils import make_stack_event


def test_multi_account_progress_initializes_all_accounts_as_waiting(mock_console: MagicMock) -> None:
    # arrange
    account_names = ["account1", "account2", "account3"]
    account_ids = {"account1": "111111111111", "account2": "222222222222", "account3": "333333333333"}

    # act
    progress = MultiAccountProgress(account_names, account_ids, "Deploy", mock_console)

    # assert
    assert progress.statuses == {
        "account1": "Waiting",
        "account2": "Waiting",
        "account3": "Waiting",
    }
    assert progress.resource_counts == {
        "account1": 0,
        "account2": 0,
        "account3": 0,
    }
    assert len(progress.completed) == 0


def test_multi_account_progress_on_event_updates_status(mock_console: MagicMock) -> None:
    # arrange
    progress = MultiAccountProgress(["account1"], {"account1": "111111111111"}, "Deploy", mock_console)

    # act
    progress.on_event("account1", make_stack_event("stack", "CREATE_IN_PROGRESS", "AWS::CloudFormation::Stack"))

    # assert
    assert progress.statuses["account1"] == "CREATE_IN_PROGRESS"


def test_multi_account_progress_mark_recreating_sets_recreating_status(mock_console: MagicMock) -> None:
    # arrange - deleting a dead stack takes minutes with no events, so the table has to say so
    progress = MultiAccountProgress(["account1", "account2"], {"account1": "111111111111", "account2": "222222222222"}, "Deploy", mock_console)
    expected = {"account1": "Recreating", "account2": "Waiting"}

    # act
    progress.mark_recreating("account1")

    # assert
    assert expected == progress.statuses


def test_multi_account_progress_recreating_account_moves_to_in_progress_on_first_resource(mock_console: MagicMock) -> None:
    # arrange - the replacement stack starts reporting resources, so Recreating is done
    progress = MultiAccountProgress(["account1"], {"account1": "111111111111"}, "Deploy", mock_console)
    progress.mark_recreating("account1")
    expected = {"account1": "IN_PROGRESS"}

    # act
    progress.on_event("account1", make_stack_event("Bucket1", "CREATE_IN_PROGRESS"))

    # assert
    assert expected == progress.statuses


def test_multi_account_progress_on_event_increments_resource_count(mock_console: MagicMock) -> None:
    # arrange
    progress = MultiAccountProgress(["account1"], {"account1": "111111111111"}, "Deploy", mock_console)

    # act
    progress.on_event("account1", make_stack_event("Bucket1", "CREATE_COMPLETE"))
    progress.on_event("account1", make_stack_event("Bucket2", "CREATE_COMPLETE"))

    # assert
    assert progress.resource_counts["account1"] == 2


def test_multi_account_progress_on_event_ignores_rollback_in_count(mock_console: MagicMock) -> None:
    # arrange
    progress = MultiAccountProgress(["account1"], {"account1": "111111111111"}, "Deploy", mock_console)

    # act
    progress.on_event("account1", make_stack_event("Bucket1", "CREATE_COMPLETE"))
    progress.on_event("account1", make_stack_event("Bucket2", "ROLLBACK_COMPLETE"))

    # assert
    assert progress.resource_counts["account1"] == 1


def test_multi_account_progress_mark_complete_success(mock_console: MagicMock) -> None:
    # arrange
    progress = MultiAccountProgress(["account1"], {"account1": "111111111111"}, "Deploy", mock_console)
    result = DeployResult(account_name="account1", success=True)

    # act
    progress.mark_complete(result)

    # assert
    assert "account1" in progress.completed
    assert progress.statuses["account1"] == "Complete"


def test_multi_account_progress_mark_complete_failure(mock_console: MagicMock) -> None:
    # arrange
    progress = MultiAccountProgress(["account1"], {"account1": "111111111111"}, "Deploy", mock_console)
    result = DeployResult(account_name="account1", success=False, error="CREATE_FAILED")

    # act
    progress.mark_complete(result)

    # assert
    assert "account1" in progress.completed
    assert progress.statuses["account1"] == "Failed"
    assert progress.errors["account1"] == "CREATE_FAILED"


def test_multi_account_progress_mark_complete_already_absent(mock_console: MagicMock) -> None:
    # arrange
    progress = MultiAccountProgress(["account1"], {"account1": "111111111111"}, "Destroy", mock_console)
    result = DeployResult(account_name="account1", success=True, already_absent=True)

    # act
    progress.mark_complete(result)

    # assert
    assert progress.statuses["account1"] == "Absent"


def test_multi_account_progress_context_manager_no_live_when_not_terminal(mock_console: MagicMock) -> None:
    # act
    with MultiAccountProgress(["account1"], {"account1": "111111111111"}, "Deploy", mock_console) as progress:
        pass

    # assert
    assert progress.live is None


# =============================================================================
# MultiAccountProgress plain mode (non-TTY, e.g. CI)
# =============================================================================


def make_multi_progress(mock_console: MagicMock) -> MultiAccountProgress:
    """Create a MultiAccountProgress against the non-terminal mock console."""
    return MultiAccountProgress(["account1"], {"account1": "111111111111"}, "Deploy", mock_console)


def test_plain_mark_started_prints_starting(mock_console: MagicMock) -> None:
    # arrange
    progress = make_multi_progress(mock_console)
    expected = [call("[Deploy] account1 (111111111111): starting", markup=False, highlight=False, soft_wrap=True)]

    # act
    progress.mark_started("account1")

    # assert
    assert expected == mock_console.print.call_args_list


def test_plain_mark_started_after_recreating_prints_nothing(mock_console: MagicMock) -> None:
    # arrange - a recreated account already announced itself via mark_recreating
    progress = make_multi_progress(mock_console)
    progress.mark_recreating("account1")
    mock_console.print.reset_mock()

    # act
    progress.mark_started("account1")

    # assert
    assert mock_console.print.call_args_list == []


def test_plain_mark_recreating_prints_message(mock_console: MagicMock) -> None:
    # arrange
    progress = make_multi_progress(mock_console)
    expected = [
        call(
            "[Deploy] account1 (111111111111): deleting unrecoverable stack before recreating",
            markup=False,
            highlight=False,
            soft_wrap=True,
        )
    ]

    # act
    progress.mark_recreating("account1")

    # assert
    assert expected == mock_console.print.call_args_list


def test_plain_stack_in_progress_prints_status(mock_console: MagicMock) -> None:
    # arrange
    progress = make_multi_progress(mock_console)
    expected = [call("[Deploy] account1 (111111111111): CREATE_IN_PROGRESS", markup=False, highlight=False, soft_wrap=True)]

    # act
    progress.on_event("account1", make_stack_event("stack", "CREATE_IN_PROGRESS", "AWS::CloudFormation::Stack"))

    # assert
    assert expected == mock_console.print.call_args_list


def test_plain_repeated_stack_status_prints_once(mock_console: MagicMock) -> None:
    # arrange
    progress = make_multi_progress(mock_console)
    expected = [call("[Deploy] account1 (111111111111): CREATE_IN_PROGRESS", markup=False, highlight=False, soft_wrap=True)]

    # act
    progress.on_event("account1", make_stack_event("stack", "CREATE_IN_PROGRESS", "AWS::CloudFormation::Stack"))
    progress.on_event("account1", make_stack_event("stack", "CREATE_IN_PROGRESS", "AWS::CloudFormation::Stack"))

    # assert
    assert expected == mock_console.print.call_args_list


def test_plain_resource_event_prints_nothing(mock_console: MagicMock) -> None:
    # arrange - the multi view stays at stack-level granularity
    progress = make_multi_progress(mock_console)

    # act
    progress.on_event("account1", make_stack_event("Bucket1", "CREATE_COMPLETE"))

    # assert
    assert mock_console.print.call_args_list == []


def test_plain_create_complete_prints_status_with_elapsed_and_count_then_waits_for_listeners(mock_console: MagicMock, mocker: MockerFixture) -> None:
    # arrange - CREATE_COMPLETE is the stack, not the deploy: the EC2 listeners still have to come up
    fake_monotonic = mocker.patch("cats.cli.infra_ui.progress.time.monotonic")
    fake_monotonic.return_value = 100.0
    progress = make_multi_progress(mock_console)
    progress.mark_started("account1")
    progress.on_event("account1", make_stack_event("Bucket1", "CREATE_COMPLETE"))
    progress.on_event("account1", make_stack_event("Bucket2", "CREATE_COMPLETE"))
    mock_console.print.reset_mock()
    fake_monotonic.return_value = 352.0
    expected = [
        call(
            "[Deploy] account1 (111111111111): CREATE_COMPLETE in 4m 12s (2 resources); waiting for the test role and EC2 listeners",
            markup=False,
            highlight=False,
            soft_wrap=True,
        )
    ]

    # act
    progress.on_event("account1", make_stack_event("stack", "CREATE_COMPLETE", "AWS::CloudFormation::Stack"))

    # assert
    assert (expected, progress.statuses["account1"], progress.completed) == (mock_console.print.call_args_list, "Readying", set())


def test_plain_on_notice_prints_listener_step(mock_console: MagicMock) -> None:
    # arrange
    progress = make_multi_progress(mock_console)
    progress.on_event("account1", make_stack_event("stack", "CREATE_COMPLETE", "AWS::CloudFormation::Stack"))
    mock_console.print.reset_mock()
    expected = [call("[Deploy] account1 (111111111111): EC2 listener i-0abc (private) ready", markup=False, highlight=False, soft_wrap=True)]

    # act
    progress.on_notice("account1", "EC2 listener i-0abc (private) ready")

    # assert
    assert expected == mock_console.print.call_args_list


def test_plain_mark_complete_after_readying_prints_created_with_elapsed(mock_console: MagicMock, mocker: MockerFixture) -> None:
    # arrange - the polled result closes an account whose stack completed earlier
    fake_monotonic = mocker.patch("cats.cli.infra_ui.progress.time.monotonic")
    fake_monotonic.return_value = 100.0
    progress = make_multi_progress(mock_console)
    progress.mark_started("account1")
    progress.on_event("account1", make_stack_event("stack", "CREATE_COMPLETE", "AWS::CloudFormation::Stack"))
    mock_console.print.reset_mock()
    fake_monotonic.return_value = 130.0
    expected = [call("[Deploy] account1 (111111111111): OK Created in 30s, ready", markup=False, highlight=False, soft_wrap=True)]

    # act
    progress.mark_complete(DeployResult(account_name="account1", success=True))

    # assert
    assert (expected, progress.statuses["account1"], progress.completed) == (mock_console.print.call_args_list, "Created", {"account1"})


def test_mark_complete_for_an_account_a_stack_event_closed_changes_nothing(mock_console: MagicMock) -> None:
    # arrange - a destroy is closed by its DELETE_COMPLETE event; the polled result must not reopen or reprint it
    progress = MultiAccountProgress(["account1"], {"account1": "111111111111"}, "Destroy", mock_console)
    progress.on_event("account1", make_stack_event("stack", "DELETE_COMPLETE", "AWS::CloudFormation::Stack"))
    mock_console.print.reset_mock()

    # act
    progress.mark_complete(DeployResult(account_name="account1", success=True))

    # assert
    assert (mock_console.print.call_args_list, progress.statuses["account1"]) == ([], "Destroyed")


def test_plain_delete_complete_prints_destroyed(mock_console: MagicMock, mocker: MockerFixture) -> None:
    # arrange
    fake_monotonic = mocker.patch("cats.cli.infra_ui.progress.time.monotonic")
    fake_monotonic.return_value = 100.0
    progress = MultiAccountProgress(["account1"], {"account1": "111111111111"}, "Destroy", mock_console)
    progress.mark_started("account1")
    mock_console.print.reset_mock()
    fake_monotonic.return_value = 145.0
    expected = [call("[Destroy] account1 (111111111111): OK Destroyed in 45s (0 resources)", markup=False, highlight=False, soft_wrap=True)]

    # act
    progress.on_event("account1", make_stack_event("stack", "DELETE_COMPLETE", "AWS::CloudFormation::Stack"))

    # assert
    assert expected == mock_console.print.call_args_list


def test_plain_terminal_failure_prints_status_with_elapsed(mock_console: MagicMock, mocker: MockerFixture) -> None:
    # arrange
    fake_monotonic = mocker.patch("cats.cli.infra_ui.progress.time.monotonic")
    fake_monotonic.return_value = 100.0
    progress = make_multi_progress(mock_console)
    progress.mark_started("account1")
    mock_console.print.reset_mock()
    fake_monotonic.return_value = 190.0
    expected = [call("[Deploy] account1 (111111111111): FAIL ROLLBACK_COMPLETE in 1m 30s", markup=False, highlight=False, soft_wrap=True)]

    # act
    progress.on_event("account1", make_stack_event("stack", "ROLLBACK_COMPLETE", "AWS::CloudFormation::Stack"))

    # assert
    assert expected == mock_console.print.call_args_list


def test_plain_mark_complete_success_prints_complete(mock_console: MagicMock) -> None:
    # arrange
    progress = make_multi_progress(mock_console)
    expected = [call("[Deploy] account1 (111111111111): OK Complete", markup=False, highlight=False, soft_wrap=True)]

    # act
    progress.mark_complete(DeployResult(account_name="account1", success=True))

    # assert
    assert expected == mock_console.print.call_args_list


def test_plain_mark_complete_destroy_success_prints_destroyed(mock_console: MagicMock) -> None:
    # arrange - a stack that vanished before its DELETE_COMPLETE event was read is closed by its polled result, in a Destroy table
    progress = MultiAccountProgress(["account1"], {"account1": "111111111111"}, "Destroy", mock_console)
    expected = [call("[Destroy] account1 (111111111111): OK Destroyed", markup=False, highlight=False, soft_wrap=True)]

    # act
    progress.mark_complete(DeployResult(account_name="account1", success=True))

    # assert
    assert (expected, progress.statuses["account1"]) == (mock_console.print.call_args_list, "Destroyed")


def test_plain_mark_complete_absent_prints_absent(mock_console: MagicMock) -> None:
    # arrange
    progress = MultiAccountProgress(["account1"], {"account1": "111111111111"}, "Destroy", mock_console)
    expected = [call("[Destroy] account1 (111111111111): Absent (not deployed)", markup=False, highlight=False, soft_wrap=True)]

    # act
    progress.mark_complete(DeployResult(account_name="account1", success=True, already_absent=True))

    # assert
    assert expected == mock_console.print.call_args_list


def test_plain_mark_complete_failure_prints_error(mock_console: MagicMock) -> None:
    # arrange
    progress = make_multi_progress(mock_console)
    expected = [call("[Deploy] account1 (111111111111): FAIL CREATE_FAILED", markup=False, highlight=False, soft_wrap=True)]

    # act
    progress.mark_complete(DeployResult(account_name="account1", success=False, error="CREATE_FAILED"))

    # assert
    assert expected == mock_console.print.call_args_list


def test_plain_mark_complete_failure_without_error_prints_failed(mock_console: MagicMock) -> None:
    # arrange
    progress = make_multi_progress(mock_console)
    expected = [call("[Deploy] account1 (111111111111): FAIL", markup=False, highlight=False, soft_wrap=True)]

    # act
    progress.mark_complete(DeployResult(account_name="account1", success=False))

    # assert
    assert expected == mock_console.print.call_args_list


def test_terminal_console_suppresses_plain_lines_multi() -> None:
    # arrange - a real terminal gets the Live panel, not plain lines
    terminal_console = MagicMock()
    terminal_console.is_terminal = True
    progress = MultiAccountProgress(["account1"], {"account1": "111111111111"}, "Deploy", terminal_console)

    # act - not entered as a context manager, so no Live exists either
    progress.mark_started("account1")

    # assert
    assert terminal_console.print.call_args_list == []


def test_plain_terminal_event_without_mark_started_uses_global_start_for_elapsed(mock_console: MagicMock, mocker: MockerFixture) -> None:
    # arrange - no mark_started call, so elapsed falls back to the overall start time
    fake_monotonic = mocker.patch("cats.cli.infra_ui.progress.time.monotonic")
    fake_monotonic.return_value = 100.0
    progress = make_multi_progress(mock_console)
    fake_monotonic.return_value = 160.0
    expected = [
        call(
            "[Deploy] account1 (111111111111): CREATE_COMPLETE in 1m 0s (0 resources); waiting for the test role and EC2 listeners",
            markup=False,
            highlight=False,
            soft_wrap=True,
        )
    ]

    # act
    progress.on_event("account1", make_stack_event("stack", "CREATE_COMPLETE", "AWS::CloudFormation::Stack"))

    # assert
    assert expected == mock_console.print.call_args_list
