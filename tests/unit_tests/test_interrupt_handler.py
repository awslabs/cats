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

"""Tests for cats.cli.infra._handle_interrupt: the Ctrl+C guidance names what was interrupted, and never more."""

import signal

import pytest
import typer

from cats.cli.infra import _handle_interrupt, _interrupt_handler


def test_interrupted_deploy_names_its_accounts_on_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    """Interrupting `deploy prod` in a ten-account config advises `destroy prod`, never `--all`: nine stacks the user did not touch."""
    # arrange
    expected_stderr = (
        "Interrupted. The deploy of prod, dev may still be in progress in CloudFormation.\n"
        + "Run 'cats infra status prod dev' to check the state.\n"
        + "Run 'cats infra destroy prod dev' to clean up.\n"
    )

    # act
    with pytest.raises(typer.Exit) as exc_info:
        _handle_interrupt("deploy", ["prod", "dev"])

    # assert
    assert (exc_info.value.exit_code, capsys.readouterr().err) == (130, expected_stderr)


def test_interrupted_destroy_says_the_deletion_continues(capsys: pytest.CaptureFixture[str]) -> None:
    # arrange
    expected_stderr = (
        "Interrupted. The destroy of prod may still be in progress in CloudFormation.\n"
        + "Run 'cats infra status prod' to check the state.\n"
        + "The deletion continues in CloudFormation; re-run 'cats infra destroy prod' to wait for it.\n"
    )

    # act
    with pytest.raises(typer.Exit) as exc_info:
        _handle_interrupt("destroy", ["prod"])

    # assert
    assert (exc_info.value.exit_code, capsys.readouterr().err) == (130, expected_stderr)


def test_interrupt_handler_installs_and_restores_the_sigint_handler() -> None:
    # arrange
    before = signal.getsignal(signal.SIGINT)

    # act
    with _interrupt_handler("deploy", ["prod"]):
        during = signal.getsignal(signal.SIGINT)
    after = signal.getsignal(signal.SIGINT)

    # assert
    assert (during is not before, after is before) == (True, True)
