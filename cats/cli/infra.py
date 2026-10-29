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

"""CLI commands for infrastructure deployment."""

import signal
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import Annotated

import typer
from botocore.exceptions import BotoCoreError, ClientError
from jinja2 import Environment, FileSystemLoader
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from cats.cfn.deploy import (
    DeployResult,
    PendingStack,
    get_stack_name,
    get_stack_state,
    poll_pending_stacks,
    start_deploy,
    start_destroy,
)
from cats.cfn.render import estimate_template_size, reject_oversized_template, render_template, template_size
from cats.cfn.resolve import resolve_account, resolve_account_settings
from cats.cfn.session import get_deploy_session
from cats.cli.console import fail_marker, ok_marker, stderr_console
from cats.cli.infra_ui.progress import MultiAccountProgress, SingleAccountProgress
from cats.config import Settings
from cats.exceptions import CatsConfigError, CatsError, CatsUsageError
from cats.filepath import get_cats_config_filepath, require_config_file, set_cats_config_filepath
from cats.resources.session_interact import get_role_arn
from cats.version import __version__

console = Console()


ConfigOption = Annotated[
    Path | None,
    typer.Option(
        "--config",
        "-c",
        help="Provide a path to cats.yaml config file. Defaults to CATS_CONFIG env var then ./cats.yaml",
    ),
]


def _handle_interrupt(operation: str, account_names: list[str]) -> None:
    """Ctrl+C during a stack operation: say what may still be running and how to check it, for these accounts only.

    Diagnostics, so stderr. The remediation names the interrupted accounts, never `--all`: advice
    to destroy stacks the user did not touch is worse than none.
    """
    names = " ".join(account_names)
    stderr_console.print(f"[yellow]Interrupted. The {operation} of {', '.join(account_names)} may still be in progress in CloudFormation.[/yellow]")
    stderr_console.print(f"Run 'cats infra status {names}' to check the state.")
    if operation == "deploy":
        stderr_console.print(f"Run 'cats infra destroy {names}' to clean up.")
    else:
        stderr_console.print(f"The deletion continues in CloudFormation; re-run 'cats infra destroy {names}' to wait for it.")
    raise typer.Exit(130)


@contextmanager
def _interrupt_handler(operation: str, account_names: list[str]) -> Iterator[None]:
    """Install a Ctrl+C handler that prints recovery instructions for this operation and these accounts."""

    def handle(signum: int, frame: FrameType | None) -> None:
        _handle_interrupt(operation, account_names)

    prev = signal.signal(signal.SIGINT, handle)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, prev)


infra_app = typer.Typer(
    name="infra",
    help="Deploy and manage CATS infrastructure. "
    + "The read-only commands (status, targets, show-template) take account keynames and default to every account; "
    + "deploy and destroy require keynames or --all.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)


def _apply_config(config_file: Path | None) -> None:
    """Point the command at its cats.yaml, and refuse to run without one.

    An explicit --config is checked by `set_cats_config_filepath`; the resolved default
    (CATS_CONFIG, then ./cats.yaml) is checked here. Without the check an absent file loads
    as an empty configuration, and `targets` prints nothing, `show-template` renders nothing
    and `status` blames `aws_region` in a file that does not exist, all with exit 0 or a wrong
    diagnosis. `cats validate` and `cats run` already fail the same way: exit 2, naming the file.
    """
    if config_file is not None:
        set_cats_config_filepath(config_file)
    require_config_file(get_cats_config_filepath())


def _start_deploy_account(
    account_name: str,
    disable_rollback: bool = False,
    on_notice: Callable[[str], None] | None = None,
) -> PendingStack | DeployResult:
    """Start deploying CATS infrastructure to a single account."""
    region = Settings().require_aws_region()
    account = Settings().accounts_under_test[account_name]
    # Offline first: an oversized config is refused before the deploy role is assumed or anything
    # is resolved. The check on the real render below is the authoritative one.
    reject_oversized_template(account_name, estimate_template_size(Settings(), account_name))
    session = get_deploy_session(account_name)
    resolved = resolve_account(
        account_name,
        account,
        session,
    )

    account_settings = resolve_account_settings(account)
    template = render_template(
        resolved=resolved,
        deployment_settings=Settings().cats_deployment_settings,
        account_settings=account_settings,
        securityhub_region=Settings().effective_securityhub_region(),
    )
    reject_oversized_template(account_name, template_size(template))

    deployment_settings = Settings().cats_deployment_settings
    stack_name = get_stack_name(deployment_settings.cfn_stack_prefix, account_name)
    # The names the template gives the additional roles, as cats/resources/session_interact.py
    # rebuilds them at test time: the readiness gate proves assumable exactly what the tests assume.
    resource_prefix = f"{deployment_settings.cfn_stack_prefix}{account_name}"
    additional_role_arns = [
        get_role_arn(f"{resource_prefix}-{role_name}", account.account_id, deployment_settings.partition)
        for role_name in account_settings.additional_roles
    ]
    return start_deploy(
        session=session,
        stack_name=stack_name,
        template_body=template,
        region=region,
        account_name=account_name,
        disable_rollback=disable_rollback,
        on_notice=on_notice,
        additional_role_arns=additional_role_arns,
    )


def _start_destroy_account(account_name: str) -> PendingStack | DeployResult:
    """Start destroying CATS infrastructure in a single account."""
    region = Settings().require_aws_region()
    session = get_deploy_session(account_name)
    stack_name = get_stack_name(Settings().cats_deployment_settings.cfn_stack_prefix, account_name)

    return start_destroy(
        session=session,
        stack_name=stack_name,
        region=region,
        account_name=account_name,
    )


def _resolve_account_names(account_names: list[str] | None, all_accounts: bool) -> list[str]:
    """Resolve the accounts an infra command operates on, validating against the config."""
    configured = list(Settings().accounts_under_test.keys())
    if all_accounts is True:
        if account_names:
            raise typer.BadParameter("Cannot specify both account names and --all")
        return configured
    if not account_names:
        raise typer.BadParameter("Specify account name(s) or --all")
    unknown = [name for name in account_names if name not in configured]
    if unknown:
        raise typer.BadParameter(f"Account(s) not found in config: {', '.join(unknown)}. Configured accounts: {', '.join(configured) or '(none)'}")
    return list(dict.fromkeys(account_names))


def _deploy_single(account_name: str, no_rollback: bool) -> None:
    """Deploy to a single account with detailed progress view."""
    with _interrupt_handler("deploy", [account_name]):
        account = Settings().accounts_under_test[account_name]
        stack_name = get_stack_name(Settings().cats_deployment_settings.cfn_stack_prefix, account_name)

        with SingleAccountProgress(
            account_name=account_name,
            account_id=account.account_id,
            stack_name=stack_name,
            operation="Deploy",
            console=console,
        ) as progress:
            started = _start_deploy_account(account_name, no_rollback, on_notice=progress.set_notice)
            if isinstance(started, DeployResult):
                # start_deploy returns a result only for a failure: a stack with nothing to update is
                # still polled, so its readiness gate runs and "Deployed successfully" means ready.
                stderr_console.print(f"[red]{fail_marker(stderr_console)} Deployment failed: {escape(str(started.error))}[/red]")
                raise typer.Exit(1)

            results = poll_pending_stacks(
                {account_name: started},
                on_event=lambda _name, event: progress.on_event(event),
                on_notice=lambda _name, message: progress.on_notice(message),
            )
            result = results[account_name]

        if result.success:
            console.print(f"[green]{ok_marker(console)} Deployed successfully[/green]")
        else:
            stderr_console.print(f"[red]{fail_marker(stderr_console)} Deployment failed: {escape(str(result.error))}[/red]")
            progress.print_failures(stderr_console)
            raise typer.Exit(1)


def _deploy_all(account_names: list[str], no_rollback: bool) -> None:
    """Deploy to multiple accounts with summary progress view."""
    with _interrupt_handler("deploy", account_names):
        pending: dict[str, PendingStack] = {}
        results: dict[str, DeployResult] = {}
        config_failures: set[str] = set()
        Settings().require_aws_region()
        account_ids = {name: Settings().accounts_under_test[name].account_id for name in account_names}

        progress = MultiAccountProgress(account_names, account_ids, "Deploy", console)
        with progress:

            def start_one(name: str) -> tuple[str, PendingStack | DeployResult]:
                # The compact table has no room for the message - the status cell carries it
                try:
                    result = _start_deploy_account(name, no_rollback, on_notice=lambda _message: progress.mark_recreating(name))
                except (CatsConfigError, CatsUsageError) as e:
                    # A pre-flight failure is the caller's configuration: the command exits 2.
                    config_failures.add(name)
                    return name, DeployResult(account_name=name, success=False, error=str(e))
                except Exception as e:
                    # One account's failure (role assume, resolve) must not abandon the other
                    # in-flight stacks unmonitored.
                    return name, DeployResult(account_name=name, success=False, error=str(e))
                if isinstance(result, PendingStack):
                    progress.mark_started(name)
                return name, result

            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = {executor.submit(start_one, name): name for name in account_names}
                for future in as_completed(futures):
                    name, result = future.result()
                    if isinstance(result, PendingStack):
                        pending[name] = result
                    else:
                        results[name] = result
                        progress.mark_complete(result)

            # A create or update is open until its test role and EC2 listeners are ready; the stack
            # event alone did not close it. on_result closes each account the cycle its own gate
            # finishes, so one slow EC2 boot does not hold the others at 'Readying'.
            results.update(poll_pending_stacks(pending, on_event=progress.on_event, on_notice=progress.on_notice, on_result=progress.mark_complete))

        failed = [r for r in results.values() if not r.success]
        if failed:
            progress.print_failures(stderr_console)
            raise typer.Exit(2 if len(config_failures) > 0 else 1)


@infra_app.command("deploy")
def deploy(
    account_names: Annotated[list[str] | None, typer.Argument(help="Account keyname(s) to deploy")] = None,
    all_accounts: Annotated[bool, typer.Option("--all", help="Deploy to all accounts in parallel")] = False,
    no_rollback: Annotated[bool, typer.Option("--no-rollback", help="Do not roll back on failure (for debugging)")] = False,
    config_file: ConfigOption = None,
) -> None:
    """Deploy CATS infrastructure via CloudFormation.

    Accepts one or more account keynames, or --all. Multiple accounts deploy in
    parallel. Exits 1 if any account fails, 2 if any failure is a configuration error.
    """
    _apply_config(config_file)
    names = _resolve_account_names(account_names, all_accounts)

    if len(names) == 1:
        _deploy_single(names[0], no_rollback)
    else:
        _deploy_all(names, no_rollback)


def _destroy_single(account_name: str) -> None:
    """Destroy in a single account with detailed progress view."""
    with _interrupt_handler("destroy", [account_name]):
        account = Settings().accounts_under_test[account_name]
        stack_name = get_stack_name(Settings().cats_deployment_settings.cfn_stack_prefix, account_name)

        with SingleAccountProgress(
            account_name=account_name,
            account_id=account.account_id,
            stack_name=stack_name,
            operation="Destroy",
            console=console,
        ) as progress:
            started = _start_destroy_account(account_name)
            if isinstance(started, DeployResult):
                if started.success:
                    console.print("[dim]Nothing to destroy - stack does not exist[/dim]")
                else:
                    stderr_console.print(f"[red]{fail_marker(stderr_console)} Destruction failed: {escape(str(started.error))}[/red]")
                    raise typer.Exit(1)
                return

            results = poll_pending_stacks(
                {account_name: started},
                on_event=lambda _name, event: progress.on_event(event),
            )
            result = results[account_name]

        if result.success:
            console.print(f"[green]{ok_marker(console)} Destroyed successfully[/green]")
        else:
            stderr_console.print(f"[red]{fail_marker(stderr_console)} Destruction failed: {escape(str(result.error))}[/red]")
            progress.print_failures(stderr_console)
            raise typer.Exit(1)


def _destroy_all(account_names: list[str]) -> None:
    """Destroy in multiple accounts with summary progress view."""
    with _interrupt_handler("destroy", account_names):
        pending: dict[str, PendingStack] = {}
        results: dict[str, DeployResult] = {}
        config_failures: set[str] = set()
        Settings().require_aws_region()
        account_ids = {name: Settings().accounts_under_test[name].account_id for name in account_names}

        progress = MultiAccountProgress(account_names, account_ids, "Destroy", console)
        with progress:

            def start_one(name: str) -> tuple[str, PendingStack | DeployResult]:
                try:
                    result = _start_destroy_account(name)
                except (CatsConfigError, CatsUsageError) as e:
                    # A pre-flight failure is the caller's configuration: the command exits 2, as deploy does.
                    config_failures.add(name)
                    return name, DeployResult(account_name=name, success=False, error=str(e))
                except Exception as e:
                    return name, DeployResult(account_name=name, success=False, error=str(e))
                if isinstance(result, PendingStack):
                    progress.mark_started(name)
                return name, result

            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = {executor.submit(start_one, name): name for name in account_names}
                for future in as_completed(futures):
                    name, result = future.result()
                    if isinstance(result, PendingStack):
                        pending[name] = result
                    else:
                        results[name] = result
                        progress.mark_complete(result)

            # A destroy is normally closed by its DELETE_COMPLETE event; on_result covers the results that
            # arrive without one (the stack vanished before the event was read, or its events could not be
            # read), so the row closes and a failure reaches print_failures.
            results.update(poll_pending_stacks(pending, on_event=progress.on_event, on_result=progress.mark_complete))

        failed = [r for r in results.values() if not r.success]
        if failed:
            progress.print_failures(stderr_console)
            raise typer.Exit(2 if len(config_failures) > 0 else 1)


@infra_app.command("destroy")
def destroy(
    account_names: Annotated[list[str] | None, typer.Argument(help="Account keyname(s) to destroy")] = None,
    all_accounts: Annotated[bool, typer.Option("--all", help="Destroy in all accounts in parallel")] = False,
    config_file: ConfigOption = None,
) -> None:
    """Destroy CATS infrastructure via CloudFormation.

    Accepts one or more account keynames, or --all. Multiple accounts destroy in
    parallel. Exits 1 if any account fails, 2 if any failure is a configuration error.
    There is no confirmation prompt: the command deletes as soon as it runs, which is
    what a pipeline step needs.
    """
    _apply_config(config_file)
    names = _resolve_account_names(account_names, all_accounts)

    if len(names) == 1:
        _destroy_single(names[0])
    else:
        _destroy_all(names)


@infra_app.command("targets")
def targets(
    account_names: Annotated[list[str] | None, typer.Argument(help="Account keynames to list (default: all)")] = None,
    config_file: ConfigOption = None,
) -> None:
    """Show what accounts and network targets would be affected by deploy/destroy.

    Lists each account's stack name and network targets (VPCs/subnets) that CATS
    will deploy infrastructure into. Use this to verify your config before deploying.
    Read-only: takes account keynames, and with none lists every account.
    """
    _apply_config(config_file)
    account_names = _resolve_account_names(account_names, all_accounts=not account_names)
    prefix = Settings().cats_deployment_settings.cfn_stack_prefix

    accounts = []
    for name in account_names:
        account = Settings().accounts_under_test[name]
        accounts.append(
            {
                "name": name,
                "account_id": account.account_id,
                "stack_name": get_stack_name(prefix, name),
                "network_targets": [
                    {"name": t_name, "vpc_id": t.vpc_id, "subnet_count": len(t.subnet_ids)}
                    for t_name, t in account.account_network_under_test.items()
                ],
            }
        )

    templates_dir = Path(__file__).parent.parent / "cfn" / "templates"
    # Security rationale: this renders terminal text, not HTML; HTML autoescaping would corrupt the displayed values.
    # nosemgrep: python_escaping_rule-jinja2-autoescape-false  # noqa: ERA001
    env = Environment(loader=FileSystemLoader(templates_dir), autoescape=False)  # noqa: S701  # nosec B701
    template = env.get_template("targets.txt.j2")
    console.print(template.render(accounts=accounts))


def _format_version_cell(deployed_version: str | None) -> str:
    """Version cell for the status table."""
    if deployed_version is None:
        return "[dim]unknown[/dim]"
    if deployed_version != __version__:
        return f"[yellow]{escape(deployed_version)} (installed {__version__})[/yellow]"
    return deployed_version


@infra_app.command("status")
def status(
    account_names: Annotated[list[str] | None, typer.Argument(help="Account keynames to check (default: all)")] = None,
    config_file: ConfigOption = None,
) -> None:
    """Show CloudFormation stack status and deploying CATS version per account.

    A Version cell reading 'X (installed Y)' means the stack was deployed by another CATS
    release: tests and deploy will refuse it until it is recreated with
    'cats infra destroy <account> && cats infra deploy <account>'.

    Read-only: takes account keynames, and with none checks every account. Accounts are
    read in parallel. Exits 1 when any account's status could not be read (e.g.
    role-assume failure); 'not deployed' is a successful answer, not an error.
    """
    _apply_config(config_file)
    names = _resolve_account_names(account_names, all_accounts=not account_names)
    prefix = Settings().cats_deployment_settings.cfn_stack_prefix
    region = Settings().require_aws_region()

    table = Table(show_header=True)
    table.add_column("Account")
    table.add_column("Status")
    table.add_column("Stack")
    table.add_column("Version")

    with ThreadPoolExecutor(max_workers=10) as executor:
        rows = list(executor.map(lambda name: _status_row(name, get_stack_name(prefix, name), region), names))

    errored = False
    for row, is_error in rows:
        table.add_row(*row)
        errored = errored or is_error

    console.print(table)
    if errored is True:
        raise typer.Exit(1)


def _status_row(name: str, stack_name: str, region: str) -> tuple[tuple[str, str, str, str], bool]:
    """One account's status row and whether reading it failed; runs on a pool thread, so no console output here."""
    try:
        session = get_deploy_session(name)
        state = get_stack_state(session, stack_name, region)
    except (CatsError, ClientError, BotoCoreError) as e:
        # A CATS or AWS failure is this account's status; anything else is a bug and propagates.
        return (name, f"[red]error: {escape(str(e))}[/red]", "-", "-"), True
    if state is None:
        return (name, "[dim]not deployed[/dim]", "-", "-"), False
    style = _status_style(state.status)
    return (name, f"[{style}]{state.status}[/{style}]", stack_name, _format_version_cell(state.version)), False


def _status_style(status: str) -> str:
    """Red for any failed or rolled-back state (ROLLBACK_COMPLETE ends in COMPLETE but is a failed deploy), green for complete, yellow in progress."""
    if "FAILED" in status or "ROLLBACK" in status:
        return "red"
    if "COMPLETE" in status:
        return "green"
    return "yellow"


@infra_app.command("show-template")
def show_template(
    account_names: Annotated[list[str] | None, typer.Argument(help="Account keynames to render (default: all)")] = None,
    dump_dir: Annotated[Path | None, typer.Option("--dump-dir", help="Write templates to directory")] = None,
    config_file: ConfigOption = None,
) -> None:
    """Show or export the CloudFormation template that would be deployed.

    Read-only: takes account keynames, and with none renders every account. With several
    accounts, each template is preceded by a '# <account>' heading unless --dump-dir is given.
    """
    _apply_config(config_file)
    names = _resolve_account_names(account_names, all_accounts=not account_names)

    for name in names:
        account = Settings().accounts_under_test[name]
        session = get_deploy_session(name)
        resolved = resolve_account(
            name,
            account,
            session,
        )

        template = render_template(
            resolved=resolved,
            deployment_settings=Settings().cats_deployment_settings,
            account_settings=resolve_account_settings(account),
            securityhub_region=Settings().effective_securityhub_region(),
        )

        if dump_dir is not None:
            dump_dir.mkdir(parents=True, exist_ok=True)
            filepath = dump_dir / f"{name}.yaml"
            filepath.write_text(template)
            console.print(f"Wrote {filepath}")
        else:
            if len(names) > 1:
                console.print(f"[bold]# {name}[/bold]")
            # markup=False: templates contain [...] sequences (Python type hints,
            # list literals) that Rich would otherwise parse as markup and strip.
            # soft_wrap=True: never hard-wrap long template lines to console width.
            console.print(template, markup=False, soft_wrap=True)
