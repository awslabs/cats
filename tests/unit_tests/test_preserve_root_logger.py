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

"""Tests for tests.logging_isolation.preserve_root_logger.

`cats mcp` replaces the root logger's handlers for the life of the process. Run in a
unit test, that handler outlived the test and wrote INFO lines into every later
CliRunner capture, failing the test_infra snapshots whenever pytest-randomly put the
mcp test first. The `pytest_runtest_protocol` wrapper in conftest wraps every test
in this context manager.
"""

import logging

import pytest
from pytest_mock import MockerFixture
from typer.testing import CliRunner

from cats.cli import cats_cli
from tests.logging_isolation import preserve_root_logger


def _wipe_handlers_and_raise() -> None:
    """Do what `cats mcp` does to the root logger, then fail."""
    logging.getLogger().handlers = [logging.NullHandler()]
    raise RuntimeError("boom")


def test_cats_mcp_handler_replacement_is_undone_on_exit(mocker: MockerFixture, cli_runner: CliRunner) -> None:
    # arrange
    mocker.patch("cats.cli.mcp.mcp.run")
    expected = list(logging.getLogger().handlers)

    # act
    with preserve_root_logger():
        cli_runner.invoke(cats_cli, ["mcp"])

    # assert
    assert logging.getLogger().handlers == expected


def test_handler_added_inside_block_is_removed_on_exit() -> None:
    # arrange
    root = logging.getLogger()
    expected = list(root.handlers)

    # act
    with preserve_root_logger():
        root.addHandler(logging.NullHandler())

    # assert
    assert root.handlers == expected


def test_level_change_is_undone_on_exit() -> None:
    # arrange
    root = logging.getLogger()
    expected = root.level

    # act
    with preserve_root_logger():
        root.setLevel(logging.CRITICAL)

    # assert
    assert root.level == expected


def test_handlers_restored_when_block_raises() -> None:
    # arrange
    root = logging.getLogger()
    expected = list(root.handlers)

    # act
    with pytest.raises(RuntimeError), preserve_root_logger():
        _wipe_handlers_and_raise()

    # assert
    assert root.handlers == expected
