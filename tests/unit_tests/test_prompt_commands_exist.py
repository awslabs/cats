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

"""Every `cats ...` command and every `cats_...` tool the rendered prompts name must exist.

The base prompt once told agents to run `cats prompts current` (no such prompt, exit 2)
and named a default test role that CATS never creates. Prompts ship inside the wheel and
are read verbatim by agents, so a command that does not exist is a first-run failure
for every agent that follows the prompt.
"""

import asyncio
import re

import pytest
import typer
from typer._click.core import Command, Context

from cats.cli import cats_cli
from cats.cli.mcp import mcp
from cats.prompts import PromptName, get_prompt

# A backticked CLI invocation: `cats`, then words, options and placeholders. Compound
# `a && b` snippets are split into their commands before resolution.
_CLI_SNIPPET = re.compile(r"`(cats(?: [^`]*)?)`")
# A backticked MCP tool name, with or without a call signature.
_TOOL_SNIPPET = re.compile(r"`(cats_[a-z_]+)(?:\([^`]*\))?`")
# Backticked `cats_...` names that are config vocabulary or a decorator, not MCP tools.
_NOT_TOOL_NAMES = frozenset({"cats_deployment_settings", "cats_json", "cats_types", "cats_requires", "cats_"})
# Words in a snippet that are not command names: options, placeholders and shell syntax.
_NOT_COMMAND_WORDS = re.compile(r"^(--?[a-z].*|<.*|\$.*|\|.*|>.*|[A-Z_]+)$")


def _command_words(snippet: str) -> list[str]:
    """`cats infra deploy <account...>` -> ["infra", "deploy"]: the words to resolve in the Typer tree."""
    words = snippet.split()[1:]
    command_words: list[str] = []
    for word in words:
        if _NOT_COMMAND_WORDS.match(word) is not None:
            break
        command_words.append(word)
    return command_words


def _cli_snippets() -> list[tuple[str, str]]:
    snippets: list[tuple[str, str]] = []
    for name in PromptName:
        for match in _CLI_SNIPPET.finditer(get_prompt(name)):
            for command in match.group(1).split("&&"):
                command = command.strip()
                if command.startswith("cats ") or command == "cats":
                    snippets.append((name.value, command))
    return sorted(set(snippets))


def _tool_snippets() -> list[tuple[str, str]]:
    snippets: list[tuple[str, str]] = []
    for name in PromptName:
        for match in _TOOL_SNIPPET.finditer(get_prompt(name)):
            if match.group(1) not in _NOT_TOOL_NAMES:
                snippets.append((name.value, match.group(1)))
    return sorted(set(snippets))


def _resolve(command_words: list[str]) -> Command | None:
    """Walk the Typer tree; None if any word is not a subcommand at its level.

    Typer ships its own click core, and its group class does not subclass the group
    type, so a group is recognized by its `get_command` method rather than its type.
    """
    current: Command = typer.main.get_command(cats_cli)
    for word in command_words:
        get_command = getattr(current, "get_command", None)
        if get_command is None:
            return None
        found: Command | None = get_command(Context(current), word)
        if found is None:
            return None
        current = found
    return current


@pytest.mark.parametrize(("prompt_name", "snippet"), _cli_snippets(), ids=lambda v: str(v))
def test_prompt_cli_command_exists(prompt_name: str, snippet: str) -> None:
    # act
    resolved = _resolve(_command_words(snippet))

    # assert
    assert resolved is not None, f"{prompt_name} names `{snippet}`, which is not a cats command"


def test_prompt_cli_options_exist() -> None:
    """Each `--option` a snippet shows must be an option of the command it follows."""
    # act
    missing: list[str] = []
    for prompt_name, snippet in _cli_snippets():
        command = _resolve(_command_words(snippet))
        assert command is not None
        declared = {opt for param in command.params for opt in param.opts}
        for word in snippet.split():
            if word.startswith("--") and word not in declared:
                missing.append(f"{prompt_name}: `{snippet}` uses {word}")

    # assert
    assert missing == []


@pytest.mark.parametrize(("prompt_name", "tool_name"), _tool_snippets(), ids=lambda v: str(v))
def test_prompt_mcp_tool_exists(prompt_name: str, tool_name: str) -> None:
    # arrange
    registered = {tool.name for tool in asyncio.run(mcp.list_tools())}

    # act & assert
    assert tool_name in registered, f"{prompt_name} names `{tool_name}`, which is not a registered MCP tool"


def test_prompts_name_commands_and_tools_at_all() -> None:
    """If the fence syntax changes, the parametrized tests must not silently become empty."""
    # act
    counts = (len(_cli_snippets()), len(_tool_snippets()))

    # assert
    assert counts >= (20, 8)
