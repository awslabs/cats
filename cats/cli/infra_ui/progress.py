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

"""Rich progress display for CloudFormation operations."""

import logging
import time
from typing import TYPE_CHECKING

from rich.console import Console, Group
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn
from rich.table import Table

from cats.cfn.events import StackEvent, is_success, is_terminal
from cats.filepath import get_cats_config_filepath

if TYPE_CHECKING:
    from types import TracebackType

    from rich.console import RenderableType

    from cats.cfn.deploy import DeployResult

MAX_DISPLAYED_EVENTS = 20


def format_elapsed(seconds: float) -> str:
    """Format elapsed time as '1m 23s' or '45s'."""
    minutes, secs = divmod(int(seconds), 60)
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


class SingleAccountProgress:
    """Progress display for single account deploy/destroy.

    On a terminal, renders a Rich Live panel. When stdout is not a TTY
    (e.g. CI), prints one plain line per state transition instead, so
    long operations are never silent.
    """

    def __init__(
        self,
        account_name: str,
        account_id: str,
        stack_name: str,
        operation: str,
        console: Console,
        known_resource_count: int | None = None,
    ) -> None:
        self.account_name = account_name
        self.account_id = account_id
        self.stack_name = stack_name
        self.operation = operation
        self.console = console
        self.plain = not console.is_terminal  # no TTY: print plain lines instead of a Live panel
        self.current_status = "STARTING"
        self.notice: str | None = None  # slow out-of-band work, e.g. replacing a dead stack
        self.start_time = time.monotonic()
        self.live: Live | None = None
        self.resources_complete: set[str] = set()
        self._known_resource_count = known_resource_count
        self.resource_events: dict[str, StackEvent] = {}  # latest event per resource
        self.failed_resources: list[StackEvent] = []  # events with FAILED status and reason
        self._original_log_level: int = logging.DEBUG

    @property
    def resources_total(self) -> int | None:
        """Total resources: known count if provided, else count of unique resources seen."""
        if self._known_resource_count is not None:
            return self._known_resource_count
        count = sum(1 for e in self.resource_events.values() if e.resource_type != "AWS::CloudFormation::Stack")
        return count if count > 0 else None

    def on_event(self, event: StackEvent) -> None:
        """Callback for deploy_stack/destroy_stack."""
        # Update latest event for this resource
        self.resource_events[event.logical_id] = event

        # Track failures with reasons
        if "FAILED" in event.status and event.status_reason is not None:
            self.failed_resources.append(event)
            self._plain_print(f"{event.logical_id} {event.status}: {event.status_reason}")

        # Update status if this is a stack-level event
        if event.resource_type == "AWS::CloudFormation::Stack":
            status_changed = event.status != self.current_status
            self.current_status = event.status
            self.notice = None  # the stack is producing events now, so any notice has been overtaken
            # If terminal, print final and stop refreshing
            if "COMPLETE" in event.status or "FAILED" in event.status:
                elapsed = format_elapsed(time.monotonic() - self.start_time)
                self._plain_print(f"{event.status} in {elapsed}")
                if self.live is not None:
                    self.live.stop()
                    self.console.print(self._render())
                    self.live = None  # Mark as done
                return
            if status_changed:
                self._plain_print(event.status)

        # Track unique resources completing
        if "COMPLETE" in event.status and "ROLLBACK" not in event.status:
            if event.resource_type != "AWS::CloudFormation::Stack" and event.logical_id not in self.resources_complete:
                self.resources_complete.add(event.logical_id)
                self._plain_print(f"{event.logical_id} {event.status} ({self._plain_resource_progress()})")

        self._refresh()

    def _plain_resource_progress(self) -> str:
        """Resource progress as '3/14 resources', or '3 resources' if the total is unknown."""
        total = self.resources_total
        if total is None:
            return f"{len(self.resources_complete)} resources"
        return f"{len(self.resources_complete)}/{total} resources"

    def _plain_print(self, message: str) -> None:
        """Print one plain progress line when there is no terminal for the Live panel (e.g. CI)."""
        if not self.plain:
            return
        self.console.print(f"[{self.operation}] {self.account_name} ({self.account_id}): {message}", markup=False, highlight=False, soft_wrap=True)

    def set_notice(self, message: str) -> None:
        """Show a message for work that produces no stack events yet, so the panel isn't silent.

        Cleared by the next stack-level event, so it never outlives the work it describes.
        """
        self.notice = message
        self._plain_print(message)
        self._refresh()

    def on_notice(self, message: str) -> None:
        """Print one line for a step that happens after the stack is complete: the test role and EC2 listeners becoming ready.

        The panel has already printed its final render by then (the terminal stack event stops it),
        so the line goes under it, in the same form the plain mode uses; on a terminal it is what
        the operator watches between CREATE_COMPLETE and 'Deployed successfully'.
        """
        if self.plain:
            self._plain_print(message)
            return
        self.console.print(f"  {message}", markup=False, highlight=False, soft_wrap=True)

    def print_failures(self, console: Console | None = None) -> None:
        """Print details of failed resources, if any; diagnostics, so callers pass their stderr console."""
        if not self.failed_resources:
            return
        target = console if console is not None else self.console
        target.print("\n[bold red]Resource failures:[/bold red]")
        for event in self.failed_resources:
            target.print(f"  {escape(event.logical_id)}: {escape(str(event.status_reason))}")

    def _build_status_line(self) -> "RenderableType":
        """Build the status line with optional progress bar."""
        status_text = self._colorize_status(self.current_status)

        if self.resources_total is not None and self.resources_total > 0:
            progress = Progress(
                BarColumn(bar_width=20, complete_style="green", finished_style="green"),
                TextColumn("{task.completed}/{task.total}"),
                expand=False,
            )
            progress.add_task("", total=self.resources_total, completed=len(self.resources_complete))
            status_table = Table.grid(expand=True)
            status_table.add_column(ratio=1)
            status_table.add_column(justify="right")
            status_table.add_row(status_text, progress)
            return status_table

        return f"{status_text}  Resources: {len(self.resources_complete)}"

    def _colorize_status(self, status: str) -> str:
        """Return status string with Rich color markup."""
        if is_success(status):
            return f"[green]Stack Status: {status}[/green]"
        elif is_terminal(status):
            return f"[red]Stack Status: {status}[/red]"
        return f"[yellow]Stack Status: {status}[/yellow]"

    def _build_events_table(self) -> Table:
        """Build the events table showing latest state per resource.

        Failed events are always shown, even if they would be truncated.
        """
        table = Table(show_header=True, box=None, padding=(0, 1), expand=True)
        table.add_column("Time", style="dim", width=8)
        table.add_column("Resource", ratio=1)
        table.add_column("Status", width=20)

        all_events = sorted(self.resource_events.values(), key=lambda e: e.timestamp)
        all_events = [e for e in all_events if e.resource_type != "AWS::CloudFormation::Stack"]

        # Partition into failed and non-failed
        failed = [e for e in all_events if "FAILED" in e.status]
        non_failed = [e for e in all_events if "FAILED" not in e.status]

        # Show recent non-failed events, reserving space for all failed events
        slots_for_non_failed = MAX_DISPLAYED_EVENTS - len(failed)
        display_events = non_failed[-slots_for_non_failed:] if slots_for_non_failed > 0 else []
        display_events.extend(failed)
        display_events.sort(key=lambda e: e.timestamp)

        for event in display_events[-MAX_DISPLAYED_EVENTS:]:
            status_style = ""
            if "COMPLETE" in event.status and "ROLLBACK" not in event.status:
                status_style = "green"
            elif "FAILED" in event.status or "ROLLBACK" in event.status:
                status_style = "red"
            elif "IN_PROGRESS" in event.status:
                status_style = "yellow"

            table.add_row(
                event.timestamp.strftime("%H:%M:%S"),
                event.logical_id,
                f"[{status_style}]{event.status}[/{status_style}]" if status_style else event.status,
            )

        return table

    def _render(self) -> Panel:
        """Render current state as Rich Panel."""
        elapsed = format_elapsed(time.monotonic() - self.start_time)
        header = f"Stack Name: {self.stack_name}  Elapsed: {elapsed}"

        rows: list[RenderableType] = [self._build_status_line()]
        if self.notice is not None:
            rows.append(f"[yellow]{escape(self.notice)}[/yellow]")
        rows.append(self._build_events_table())

        return Panel(
            Group(*rows),
            title=f"{self.operation}ing CATS stack: {self.account_name} ({self.account_id})",
            subtitle=header,
        )

    def _refresh(self) -> None:
        """Update the live display."""
        if self.live is not None:
            self.live.update(self._render())

    def __enter__(self) -> "SingleAccountProgress":
        if self.console.is_terminal:
            # Suppress logging while Live display is active
            self._original_log_level = logging.getLogger("cats").level
            logging.getLogger("cats").setLevel(logging.WARNING)
            # Pass self._render as get_renderable so elapsed time updates automatically
            self.live = Live(self._render(), console=self.console, refresh_per_second=1, transient=True, get_renderable=self._render)
            self.live.start()
        else:
            self._plain_print(f"starting stack {self.stack_name}")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: "TracebackType | None",
    ) -> None:
        if self.live is not None:
            self.live.stop()
            self.console.print(self._render())
        logging.getLogger("cats").setLevel(self._original_log_level)


class MultiAccountProgress:
    """Progress display for parallel deploy/destroy of multiple accounts.

    On a terminal, renders a Rich Live panel. When stdout is not a TTY
    (e.g. CI), prints one plain line per account status transition instead,
    so long operations are never silent.
    """

    def __init__(
        self,
        account_names: list[str],
        account_ids: dict[str, str],
        operation: str,
        console: Console,
    ) -> None:
        self.account_names = account_names
        self.account_ids = account_ids
        self.operation = operation
        self.console = console
        self.plain = not console.is_terminal  # no TTY: print plain lines instead of a Live panel
        self.start_time = time.monotonic()
        self.live: Live | None = None
        self._original_log_level: int = logging.DEBUG
        # Per-account state
        self.statuses: dict[str, str] = {name: "Waiting" for name in account_names}
        self.resource_counts: dict[str, int] = {name: 0 for name in account_names}
        self.started_at: dict[str, float] = {}  # per-account start, for plain-mode elapsed
        self.completed: set[str] = set()
        self.errors: dict[str, str] = {}

    def on_event(self, account_name: str, event: StackEvent) -> None:
        """Callback for poll_pending_stacks."""
        # Update status from any stack-level event
        if event.resource_type == "AWS::CloudFormation::Stack":
            status_changed = event.status != self.statuses[account_name]
            self.statuses[account_name] = event.status
            if is_terminal(event.status):
                elapsed = format_elapsed(time.monotonic() - self.started_at.get(account_name, self.start_time))
                if is_success(event.status) and "DELETE" in event.status:
                    self.completed.add(account_name)
                    self.statuses[account_name] = "Destroyed"
                    self._plain_print(account_name, f"OK Destroyed in {elapsed} ({self.resource_counts[account_name]} resources)")
                elif is_success(event.status):
                    # The stack is complete; its test role and EC2 listeners are still being proven
                    # ready. poll_pending_stacks calls mark_complete in the cycle they become so, closing the account.
                    self.statuses[account_name] = "Readying"
                    self._plain_print(
                        account_name,
                        f"{event.status} in {elapsed} ({self.resource_counts[account_name]} resources); waiting for the test role and EC2 listeners",
                    )
                else:
                    self.completed.add(account_name)
                    self.errors[account_name] = event.status
                    self._plain_print(account_name, f"FAIL {event.status} in {elapsed}")
            elif status_changed:
                self._plain_print(account_name, event.status)
        elif self.statuses[account_name] in ("Waiting", "Starting", "Recreating"):
            # First resource event means operation is in progress
            self.statuses[account_name] = "IN_PROGRESS"
        if "COMPLETE" in event.status and "ROLLBACK" not in event.status:
            self.resource_counts[account_name] += 1
        self._refresh()

    def on_notice(self, account_name: str, message: str) -> None:
        """Callback for poll_pending_stacks: a test role was proven usable, or an EC2 listener wrote its ready verdict."""
        self._plain_print(account_name, message)
        self._refresh()

    def mark_started(self, account_name: str) -> None:
        """Mark an account as started (API call made, waiting for events)."""
        self.started_at.setdefault(account_name, time.monotonic())
        if self.statuses.get(account_name) == "Waiting":
            self.statuses[account_name] = "Starting"
            self._plain_print(account_name, "starting")
            self._refresh()

    def mark_recreating(self, account_name: str) -> None:
        """Mark an account whose unrecoverable stack is being deleted before a fresh create."""
        self.started_at.setdefault(account_name, time.monotonic())
        self.statuses[account_name] = "Recreating"
        self._plain_print(account_name, "deleting unrecoverable stack before recreating")
        self._refresh()

    def mark_complete(self, result: "DeployResult") -> None:
        """Mark an account as finished: an immediate result, or a polled one the cycle its test role and listeners are ready.

        poll_pending_stacks reports each result through this as it is decided, so a `--all` deploy closes
        each account on its own clock. A destroy or a failed stack is closed by its terminal stack event
        in on_event; calling this again for it changes nothing, so every polled result can pass through.
        """
        if result.account_name in self.completed:
            return
        self.completed.add(result.account_name)
        was_readying = self.statuses.get(result.account_name) == "Readying"
        if result.success:
            if result.already_absent:
                self.statuses[result.account_name] = "Absent"
                self._plain_print(result.account_name, "Absent (not deployed)")
            elif was_readying:
                elapsed = format_elapsed(time.monotonic() - self.started_at.get(result.account_name, self.start_time))
                self.statuses[result.account_name] = "Created"
                self._plain_print(result.account_name, f"OK Created in {elapsed}, ready")
            elif self.operation == "Destroy":
                # The stack vanished before its DELETE_COMPLETE event was read: destroyed all the same.
                self.statuses[result.account_name] = "Destroyed"
                self._plain_print(result.account_name, "OK Destroyed")
            else:
                self.statuses[result.account_name] = "Complete"
                self._plain_print(result.account_name, "OK Complete")
        else:
            self.statuses[result.account_name] = "Failed"
            if result.error is not None:
                self.errors[result.account_name] = result.error
            self._plain_print(result.account_name, f"FAIL {result.error}" if result.error is not None else "FAIL")
        self._refresh()

    def _plain_print(self, account_name: str, message: str) -> None:
        """Print one plain progress line when there is no terminal for the Live panel (e.g. CI).

        Messages must be ASCII-only: non-TTY environments can have a non-UTF-8
        stdout (e.g. LANG=C), where unicode glyphs raise UnicodeEncodeError.
        """
        if not self.plain:
            return
        account_id = self.account_ids.get(account_name, "")
        self.console.print(f"[{self.operation}] {account_name} ({account_id}): {message}", markup=False, highlight=False, soft_wrap=True)

    def _render(self) -> Panel:
        """Render current state as Rich Panel."""
        elapsed = format_elapsed(time.monotonic() - self.start_time)
        completed_count = len(self.completed)
        total = len(self.account_names)
        failed_count = len(self.errors)

        # Overall status line (like single account)
        if failed_count > 0:
            overall_status = f"[red]Status: {failed_count} FAILED[/red]"
        elif completed_count == total:
            overall_status = "[green]Status: COMPLETE[/green]"
        else:
            overall_status = f"[yellow]Status: {self.operation.upper()}ING[/yellow]"

        # Progress bar with success/fail counts
        if failed_count > 0:
            status_text = f"{completed_count}/{total} ({failed_count} failed)"
        else:
            status_text = f"{completed_count}/{total}"

        # Status + progress bar + elapsed row
        status_row = Table.grid(expand=True)
        status_row.add_column(width=25)
        status_row.add_column(ratio=1)
        status_row.add_column(justify="right", width=15)
        progress = Progress(
            BarColumn(bar_width=30, complete_style="green", finished_style="green"),
            TextColumn(status_text),
            expand=False,
        )
        progress.add_task("", total=total, completed=completed_count)
        status_row.add_row(overall_status, progress, f"Elapsed: {elapsed}")

        # Account table - limit to 20 rows
        table = Table(show_header=True, box=None, padding=(0, 1), expand=True)
        table.add_column("Account", width=15)
        table.add_column("Account ID", width=14)
        table.add_column("Status", width=22)

        # Show failed first, then in-progress/starting, then completed, then waiting
        def sort_key(name: str) -> tuple[int, str]:
            status = self.statuses[name]
            if status == "Failed" or "FAILED" in status or "ROLLBACK" in status:
                return (0, name)
            elif status in ("Starting", "Recreating", "Readying") or "IN_PROGRESS" in status:
                return (1, name)
            elif status in ("Complete", "Absent", "Created", "Destroyed") or "COMPLETE" in status:
                return (2, name)
            else:  # Waiting
                return (3, name)

        sorted_names = sorted(self.account_names, key=sort_key)

        for name in sorted_names:
            status = self.statuses[name]
            account_id = self.account_ids.get(name, "")

            # Color and icon based on status
            STATUS_DISPLAY = {
                "Created": "[green]✓ Created[/green]",
                "Destroyed": "[green]✓ Destroyed[/green]",
                "Complete": "[green]✓ Complete[/green]",
                "Absent": "[dim]- Absent[/dim]",
                "Failed": "[red]✗ Failed[/red]",
                "Waiting": "[dim]○ Waiting[/dim]",
                "Starting": "[yellow]◐ Starting[/yellow]",
                "Recreating": "[yellow]◐ Recreating[/yellow]",
                "Readying": "[yellow]◐ Readying[/yellow]",
            }
            if status in STATUS_DISPLAY:
                status_str = STATUS_DISPLAY[status]
            elif "FAILED" in status or "ROLLBACK" in status:
                status_str = f"[red]✗ {status}[/red]"
            elif "IN_PROGRESS" in status:
                status_str = f"[yellow]◐ {status}[/yellow]"
            else:
                status_str = status

            table.add_row(name, account_id, status_str)

        # Bottom line with config path
        config_path = get_cats_config_filepath()
        subtitle = f"Config: {config_path}"

        return Panel(
            Group(status_row, table),
            title=f"{self.operation}ing CATS in {total} targets",
            subtitle=subtitle,
        )

    def print_failures(self, console: Console | None = None) -> None:
        """Print details of failed accounts, if any; diagnostics, so callers pass their stderr console."""
        if not self.errors:
            return
        target = console if console is not None else self.console
        target.print("\n[bold red]Failures:[/bold red]")
        for name, error in self.errors.items():
            target.print(f"  [bold]{escape(name)}[/bold]: {escape(str(error))}")

    def _refresh(self) -> None:
        """Update the live display."""
        if self.live is not None:
            self.live.update(self._render())

    def __enter__(self) -> "MultiAccountProgress":
        if self.console.is_terminal:
            self._original_log_level = logging.getLogger("cats").level
            logging.getLogger("cats").setLevel(logging.WARNING)
            self.live = Live(
                console=self.console,
                refresh_per_second=1,
                transient=False,
                get_renderable=self._render,
            )
            self.live.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: "TracebackType | None",
    ) -> None:
        if self.live is not None:
            self.live.stop()
        logging.getLogger("cats").setLevel(self._original_log_level)
