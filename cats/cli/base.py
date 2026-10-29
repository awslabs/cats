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

"""Base CLI app - imported by command modules to avoid circular imports."""

import logging
import os
from typing import TYPE_CHECKING

from botocore.exceptions import BotoCoreError, ClientError
from rich.logging import RichHandler
from rich.text import Text
from typer import Exit, Option, Typer
from typer.core import TyperGroup

from cats.cli.console import fail_marker, stderr_console, stdout_console
from cats.exceptions import CatsConfigError, CatsError, CatsUsageError
from cats.version import __version__

FORMAT = "%(message)s"
# Logs go to stderr: stdout belongs to command output (`cats run` and MCP emit JSON there
# for machine consumers, and a DEBUG record would corrupt the document).
logging.basicConfig(
    level="NOTSET",
    format=FORMAT,
    datefmt="[%X]",
    handlers=[RichHandler(level="NOTSET", console=stderr_console)],
)

# AWS SDK loggers silenced for every command. `cats test` runs pytest in a child process
# (cats.cli.testing), which never imports this module, so tests run under pytest's own logging.
QUIET_LOGGERS = ("boto", "urllib3", "s3transfer", "boto3", "botocore", "nose")

for name in logging.Logger.manager.loggerDict:
    if name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.CRITICAL)


if TYPE_CHECKING:
    from typer._click.core import Context


class OrderCommands(TyperGroup):
    def list_commands(self, ctx: "Context") -> list[str]:
        """Return list of commands in the order they appear.

        See: https://github.com/tiangolo/typer/issues/428#issuecomment-1238866548
        """
        order = [
            "init",
            "validate",
            "generate-types",
            "test",
            "infra",
            "run",
            "prompts",
            "mcp",
        ]
        ordered_commands = [command for command in order if command in self.commands]
        additional_commands = [command for command in self.commands if command not in ordered_commands]
        return ordered_commands + additional_commands

    def invoke(self, ctx: "Context") -> object:
        """One exit-code rule for every command: 2 for the caller's arguments or configuration, 1 for a failed operation.

        A CatsError that reaches here is printed to stderr as a diagnostic, not a traceback. So is
        an AWS SDK error (expired or missing credentials, throttling, an unreachable endpoint): it
        is the environment or the service failing, which is an operation failure, and the SDK's
        message says what happened better than two hundred lines of frames do. The same catch
        would also flatten an SDK error CATS itself caused (a ParamValidationError from a bad
        request, bare or already wrapped in a CatsError by a session boundary), so CATS_DEBUG=1
        lets that class of error propagate with its traceback. `cats run` handles its own errors
        first so its stderr stays JSON.
        """
        try:
            return super().invoke(ctx)
        except (CatsUsageError, CatsConfigError) as e:
            _print_failure(e)
            raise Exit(code=2) from e
        except CatsError as e:
            # `raise ... from None` suppresses the context; the traceback would then hide the SDK error the flag exists to show.
            chained = e.__cause__ if e.__suppress_context__ is True else (e.__cause__ or e.__context__)
            if _debugging_an_sdk_error(chained) is True:
                raise
            _print_failure(e)
            raise Exit(code=1) from e
        except (ClientError, BotoCoreError) as e:
            if _debugging_an_sdk_error(e) is True:
                raise
            _print_failure(e)
            raise Exit(code=1) from e


def _debugging_an_sdk_error(error: BaseException | None) -> bool:
    """Whether CATS_DEBUG=1 is set and `error` is an AWS SDK error, which then propagates with its traceback."""
    return os.environ.get("CATS_DEBUG") == "1" and isinstance(error, (ClientError, BotoCoreError))


def _print_failure(error: Exception) -> None:
    """The message as text, never as markup: pydantic reports contain [type=...] brackets Rich would eat."""
    stderr_console.print(Text.assemble((fail_marker(stderr_console), "red"), " ", str(error)), soft_wrap=True)


cats_cli = Typer(no_args_is_help=True, add_completion=False, cls=OrderCommands, pretty_exceptions_show_locals=False)


def version_callback(value: bool) -> None:
    if value is True:
        stdout_console.print(__version__)
        raise Exit()


@cats_cli.callback()
def root_app_callback(
    version: bool = Option(
        None,
        "--version",
        "-v",
        callback=version_callback,
        is_eager=True,
        help=f"Installed version ({__version__})",
    ),
) -> None:
    return None
