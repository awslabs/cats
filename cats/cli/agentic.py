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

from pathlib import Path
from typing import Annotated

import typer

from cats.cli.base import cats_cli
from cats.cli.console import stderr_console, stdout_console
from cats.prompts import PromptName, get_all_prompts, get_prompt

prompts_app = typer.Typer(
    name="prompts",
    help="Context prompts for AI agents working with CATS. Start with `cats prompts overview`.",
    no_args_is_help=True,
    invoke_without_command=True,
    pretty_exceptions_show_locals=False,
)


@prompts_app.callback(invoke_without_command=True)
def prompts_callback(
    ctx: typer.Context,
    all_prompts: Annotated[bool, typer.Option("--all", help="Output all prompts combined")] = False,
    dump_dir: Annotated[Path | None, typer.Option("--dump-dir", help="Write all prompts as markdown files to directory")] = None,
) -> None:
    """Context prompts for AI agents working with CATS."""
    if (all_prompts is True or dump_dir is not None) and ctx.invoked_subcommand is not None:
        # The group options act on every prompt; a subcommand named alongside them is a usage error.
        raise typer.BadParameter(f"--all and --dump-dir take no prompt name; drop '{ctx.invoked_subcommand}' or the option")
    if all_prompts is True and dump_dir is not None:
        raise typer.BadParameter("--all prints to stdout and --dump-dir writes files; give one or the other")

    if all_prompts is True:
        _print_prompt(get_all_prompts())
        raise typer.Exit()

    if dump_dir is not None:
        dump_dir.mkdir(parents=True, exist_ok=True)
        for name in PromptName:
            filepath = dump_dir / f"{name.value}.md"
            filepath.write_text(get_prompt(name), encoding="utf-8")
            stdout_console.print(f"Wrote {filepath}")
        raise typer.Exit()

    # If no options and no subcommand, show help
    if ctx.invoked_subcommand is None:
        stdout_console.print(ctx.get_help())


def _print_prompt(text: str) -> None:
    """Prompts go out verbatim: no markup, and no hard wrap at the console width, which an agent's pipe reports as 80 columns."""
    stdout_console.print(text, markup=False, soft_wrap=True)


@prompts_app.command("overview")
def overview() -> None:
    """What CATS is and how it works."""
    _print_prompt(get_prompt(PromptName.OVERVIEW))


@prompts_app.command("setup")
def setup() -> None:
    """How to configure and deploy CATS."""
    _print_prompt(get_prompt(PromptName.SETUP))


@prompts_app.command("testing")
def testing() -> None:
    """How to write CATS tests."""
    _print_prompt(get_prompt(PromptName.TESTING))


@prompts_app.command("agentic")
def agentic() -> None:
    """Profile for agents connected to CATS over MCP: tools, patterns, when to escalate to pytest."""
    _print_prompt(get_prompt(PromptName.AGENTIC))


@cats_cli.command("mcp", rich_help_panel="Agentic AI")
def mcp_cmd() -> None:
    """Start CATS as a Model Context Protocol (MCP) server. Configure in your AI agent's MCP settings, not called directly.

    Requires the mcp extra: install with `pip install 'aws-cats\\[mcp]'`.
    """
    try:
        from cats.cli.mcp import main
    except ImportError as e:
        if e.name != "fastmcp":
            raise
        # stderr, never stdout: an MCP host reads stdout as the protocol channel and
        # would report a JSON parse error instead of showing this hint in its log.
        stderr_console.print(
            "cats mcp requires the MCP extra. Install it with: pip install 'aws-cats[mcp]'  (or: uv add 'aws-cats[mcp]')",
            markup=False,
        )
        raise typer.Exit(1) from e

    main()
