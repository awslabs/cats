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

import os
import signal
import subprocess  # nosec B404 - runs this interpreter's own pytest, with arguments the user typed
import sys
from pathlib import Path
from types import FrameType
from typing import Annotated

import typer

from cats.cli.base import cats_cli
from cats.filepath import set_cats_config_filepath

# The signals a supervisor aims at this process alone and that must reach pytest; SIGHUP is POSIX-only.
_FORWARDED_SIGNALS = tuple(getattr(signal, name) for name in ("SIGTERM", "SIGHUP") if hasattr(signal, name))


@cats_cli.command("test", rich_help_panel="Test", context_settings={"ignore_unknown_options": True})
def run_tests(
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            help="Path to cats.yaml. Defaults to the CATS_CONFIG environment variable, then ./cats.yaml. "
            + "Long form only: -c belongs to pytest, which receives every other argument.",
        ),
    ] = None,
    args: Annotated[list[str] | None, typer.Argument(help="Arguments passed to pytest verbatim; given, they replace the default -vv -ra.")] = None,
) -> None:
    """Run all CATS tests with Pytest, defaulting to `pytest -vv -ra`.
    Pass arguments to `cats test` to override these defaults with your own arguments.

    The CATS plugin is loaded explicitly (`-p cats`), so PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 in a
    hardened CI environment still yields the account fixtures and the reports. The exit code
    is pytest's, unchanged: 0 all passed, 1 tests failed, 2 interrupted, 3 internal error,
    4 usage error, 5 no tests collected. pytest runs as a child process; a SIGTERM or SIGHUP
    sent to `cats test` is passed to it, so the run ends as `pytest` ends under the same signal:
    pytest handles neither, so it stops at once, finalizers do not run, no report is written,
    and `cats test` returns 128 + the signal number. To keep the reports when a run is cut short,
    interrupt the whole process group with SIGINT instead, as Ctrl+C does: `timeout --signal=INT
    30m cats test` (timeout itself then exits 124 unless given --preserve-status), or `kill -INT
    -- -<pgid>`, which needs SIGINT not ignored: a background job of a non-interactive shell
    starts with it ignored, so prefer timeout. Tests run under pytest's own logging, exactly as
    with `pytest`.
    """
    if config is not None:
        # Set env var so pytest-xdist workers inherit it
        os.environ["CATS_CONFIG"] = str(config.resolve())
        set_cats_config_filepath(config)

    if args is None:
        pytest_args = ["-vv", "-ra"]
    else:
        pytest_args = args
    raise typer.Exit(code=_run_pytest(pytest_args))


def _run_pytest(pytest_args: list[str]) -> int:
    """Run `python -m pytest -p cats <args>` as a child of this interpreter and return its exit code.

    A child process, not `pytest.main()` in this one: the CLI has already imported the `cats`
    package by the time this command runs, and pytest cannot assertion-rewrite a module that is
    imported before its import hook is installed, so an in-process run opened every `cats test`
    with two `PytestAssertRewriteWarning`s (and failed outright under `-W error`). The child
    starts with nothing imported, exactly as `pytest` on the command line does, and inherits
    the environment (CATS_CONFIG, AWS variables) and the working directory. Its stdout and
    stderr are the terminal's: the user watches the run live.

    Ctrl+C reaches both processes. pytest handles its own: it prints its interrupt summary,
    runs the finalizers, writes the reports and exits 2. This process waits for that instead
    of killing it mid-teardown, then reports pytest's code. A second Ctrl+C is left to propagate.

    A termination signal aimed at this process alone (SIGTERM; SIGHUP where it exists), from
    `kill <pid>` or a container runtime stopping PID 1, is passed to pytest unchanged, so
    `cats test` ends when pytest ends rather than leaving it running as an orphan holding
    whatever its fixtures hold. What the signal does to pytest is what it does to `pytest` run
    directly: pytest handles neither signal, so it stops at once with no finalizers and no
    reports. GNU `timeout`, a terminal hangup and a shell-executor CI runner signal the whole
    process group and reach pytest without this process's help; the recipe that keeps the
    reports is SIGINT to the group (`timeout --signal=INT`), which both processes handle as
    Ctrl+C. SIGINT is deliberately not passed on: Ctrl+C already reaches the child, and a SIGINT
    aimed at this process alone leaves pytest running, which the documentation says. A child
    ended by a signal has no pytest exit code and is reported as a shell reports it, 128 + the
    signal number. The handlers are installed once `Popen` returns, by which time pytest is
    already running: a signal aimed at this process in those microseconds ends it by its default
    action and leaves pytest running, as `kill` on a shell leaves the shell's child.

    `-P` (safe path, Python 3.11+) keeps the working directory off the child's `sys.path`.
    `python -m pytest` would otherwise prepend it, which the `pytest` console script and the
    in-process run never did: a `pytest.py` in the project would shadow the real package, and
    an import that fails under `pytest` could pass under `cats test`. PYTHONPATH still applies.
    """
    process = subprocess.Popen([sys.executable, "-P", "-m", "pytest", "-p", "cats", *pytest_args])  # noqa: S603  # nosec B603

    def forward(signum: int, frame: FrameType | None) -> None:
        process.send_signal(signum)

    previous = {signum: signal.signal(signum, forward) for signum in _FORWARDED_SIGNALS}
    try:
        try:
            exit_code = process.wait()
        except KeyboardInterrupt:
            exit_code = process.wait()
    finally:
        for signum, handler in previous.items():
            # None: the previous handler was not installed from Python, and signal.signal cannot restore it.
            if handler is not None:
                signal.signal(signum, handler)
    if exit_code < 0:
        # Killed by a signal before pytest could exit: report it as a shell would (128 + signal number).
        return 128 - exit_code
    return exit_code
