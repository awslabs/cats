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

"""Pins for the root `cats` command: covered behaviors a Typer or Click upgrade could move silently."""

import pytest
from typer.testing import CliRunner

from cats.cli import cats_cli
from cats.version import __version__


@pytest.mark.parametrize("flag", ["--version", "-v"])
def test_version_flag_prints_the_bare_installed_version(flag: str, cli_runner: CliRunner) -> None:
    """The version alone on stdout, so `cats --version` is scriptable."""
    # act
    result = cli_runner.invoke(cats_cli, [flag])

    # assert
    assert (result.exit_code, result.stdout) == (0, f"{__version__}\n")


def test_root_without_arguments_exits_2(cli_runner: CliRunner) -> None:
    """no_args_is_help prints the help and exits 2 (Click 8.2 behavior, part of the exit-code contract)."""
    # act
    result = cli_runner.invoke(cats_cli, [])

    # assert
    assert result.exit_code == 2
