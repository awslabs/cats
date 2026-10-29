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
from tempfile import TemporaryDirectory

import pytest
from jinja2 import Environment, FileSystemLoader
from typer.testing import CliRunner

from cats.cli import cats_cli
from cats.prompts import PROMPTS_DIR, PromptName, get_all_prompts, get_prompt


@pytest.fixture
def cli_runner_narrow() -> CliRunner:
    """CLI runner with narrow terminal for help output formatting tests."""
    return CliRunner(env={"COLUMNS": "120", "NO_COLOR": "1"})


@pytest.fixture
def cli_runner_wide() -> CliRunner:
    """CLI runner with very wide terminal to prevent any wrapping."""
    return CliRunner(env={"COLUMNS": "1000", "NO_COLOR": "1"})


def test_prompts_without_arg_exits_2(cli_runner_narrow: CliRunner) -> None:
    # act
    result = cli_runner_narrow.invoke(cats_cli, ["prompts"])

    # assert - exits 2 because no_args_is_help=True shows help
    expected_exit_code = 2
    assert result.exit_code == expected_exit_code


def test_prompts_without_arg_shows_help(cli_runner_narrow: CliRunner) -> None:
    # arrange
    expected_output = """\
                                                                                                                        
 Usage: root prompts [OPTIONS] COMMAND [ARGS]...                                                                        
                                                                                                                        
 Context prompts for AI agents working with CATS. Start with `cats prompts overview`.                                   
                                                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────────────────────────╮
│ --all                     Output all prompts combined                                                                │
│ --dump-dir        <path>  Write all prompts as markdown files to directory                                           │
│ --help                    Show this message and exit.                                                                │
╰──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────────────────────────────────────────────╮
│ overview  What CATS is and how it works.                                                                             │
│ setup     How to configure and deploy CATS.                                                                          │
│ testing   How to write CATS tests.                                                                                   │
│ agentic   Profile for agents connected to CATS over MCP: tools, patterns, when to escalate to pytest.                │
╰──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
"""  # noqa: W291, W293

    # act
    result = cli_runner_narrow.invoke(cats_cli, ["prompts"])

    # assert
    assert result.output == expected_output


def test_prompts_setup_exits_0(cli_runner_wide: CliRunner) -> None:
    # act
    result = cli_runner_wide.invoke(cats_cli, ["prompts", "setup"])

    # assert
    expected_exit_code = 0
    assert result.exit_code == expected_exit_code


def test_prompts_setup_outputs_template(cli_runner_wide: CliRunner) -> None:
    # arrange
    expected_output = get_prompt(PromptName.SETUP) + "\n"

    # act
    result = cli_runner_wide.invoke(cats_cli, ["prompts", "setup"])

    # assert
    assert result.stdout == expected_output


def test_get_prompt_setup_returns_template() -> None:
    # arrange
    env = Environment(loader=FileSystemLoader(PROMPTS_DIR), autoescape=True)
    template = env.get_template("setup.md.j2")
    expected = template.render(standalone=True)

    # act
    content = get_prompt(PromptName.SETUP)

    # assert
    assert content == expected


def test_prompts_all_outputs_all_prompts(cli_runner_wide: CliRunner) -> None:
    # arrange
    expected_output = get_all_prompts() + "\n"

    # act
    result = cli_runner_wide.invoke(cats_cli, ["prompts", "--all"])

    # assert
    expected_exit_code = 0
    assert result.exit_code == expected_exit_code
    assert result.stdout == expected_output


def test_prompts_dump_dir_writes_files(cli_runner_wide: CliRunner) -> None:
    with TemporaryDirectory() as tmpdir:
        # act
        result = cli_runner_wide.invoke(cats_cli, ["prompts", "--dump-dir", tmpdir])

        # assert
        expected_exit_code = 0
        assert result.exit_code == expected_exit_code
        for name in PromptName:
            filepath = Path(tmpdir) / f"{name.value}.md"
            assert filepath.exists()
            expected_content = get_prompt(name)
            assert filepath.read_text() == expected_content


def test_get_all_prompts_is_the_base_followed_by_each_body() -> None:
    """`--all` carries the shared base once, then every other prompt without its own copy."""
    # arrange
    bodies = [get_prompt(name, standalone=False) for name in PromptName if name is not PromptName.OVERVIEW]
    expected = "\n\n---\n\n".join([get_prompt(PromptName.OVERVIEW), *bodies])

    # act
    result = get_all_prompts()

    # assert
    assert result == expected


def test_get_all_prompts_renders_the_base_once() -> None:
    """Concatenating standalone prompts repeated the base once per prompt, a third of the document."""
    # arrange
    base = get_prompt(PromptName.OVERVIEW)

    # act
    count = get_all_prompts().count(base)

    # assert
    assert count == 1


def test_get_prompt_body_only_opens_with_its_own_heading() -> None:
    # arrange
    expected_first_line = "# Writing CATS Tests"

    # act
    body = get_prompt(PromptName.TESTING, standalone=False)

    # assert
    assert body.splitlines()[0] == expected_first_line


def test_get_prompt_body_only_renders_the_base_zero_times() -> None:
    # arrange
    base = get_prompt(PromptName.OVERVIEW)

    # act
    count = get_prompt(PromptName.TESTING, standalone=False).count(base)

    # assert
    assert count == 0


def test_get_prompt_standalone_is_the_base_then_the_body() -> None:
    """A single prompt stands alone in an agent's context: the base, a rule, then its own body."""
    # arrange
    expected = get_prompt(PromptName.OVERVIEW) + "\n\n---\n\n" + get_prompt(PromptName.TESTING, standalone=False)

    # act
    result = get_prompt(PromptName.TESTING)

    # assert
    assert result == expected


def test_get_prompt_overview_is_the_base_whichever_way_it_is_rendered() -> None:
    # act
    renders = (get_prompt(PromptName.OVERVIEW), get_prompt(PromptName.OVERVIEW, standalone=False))

    # assert
    assert renders[0] == renders[1]


def test_prompts_all_with_a_subcommand_is_a_usage_error(cli_runner_wide: CliRunner) -> None:
    """`cats prompts --all overview` is a usage error naming the conflict."""
    # act
    result = cli_runner_wide.invoke(cats_cli, ["prompts", "--all", "overview"])

    # assert
    assert (result.exit_code, result.stdout) == (2, "")


def test_prompts_dump_dir_with_a_subcommand_is_a_usage_error(cli_runner_wide: CliRunner) -> None:
    # act
    with TemporaryDirectory() as tmpdir:
        result = cli_runner_wide.invoke(cats_cli, ["prompts", "--dump-dir", tmpdir, "overview"])
        written = sorted(Path(tmpdir).iterdir())

    # assert
    assert (result.exit_code, result.stdout, written) == (2, "", [])


def test_prompts_all_and_dump_dir_are_exclusive(cli_runner_wide: CliRunner) -> None:
    """`--all --dump-dir dd` is a usage error naming the conflict."""
    # act
    with TemporaryDirectory() as tmpdir:
        result = cli_runner_wide.invoke(cats_cli, ["prompts", "--all", "--dump-dir", tmpdir])

    # assert
    assert (result.exit_code, result.stdout) == (2, "")


def test_prompts_piped_output_is_not_hard_wrapped() -> None:
    """An agent harness reads the prompt through a pipe, where Rich assumes 80 columns; a wrapped code line is a broken one."""
    # arrange
    narrow_runner = CliRunner(env={"COLUMNS": "40", "NO_COLOR": "1"})
    expected_output = get_prompt(PromptName.TESTING) + "\n"

    # act
    result = narrow_runner.invoke(cats_cli, ["prompts", "testing"])

    # assert
    assert result.stdout == expected_output


def test_prompts_all_piped_output_is_not_hard_wrapped() -> None:
    # arrange
    narrow_runner = CliRunner(env={"COLUMNS": "40", "NO_COLOR": "1"})
    expected_output = get_all_prompts() + "\n"

    # act
    result = narrow_runner.invoke(cats_cli, ["prompts", "--all"])

    # assert
    assert result.stdout == expected_output
