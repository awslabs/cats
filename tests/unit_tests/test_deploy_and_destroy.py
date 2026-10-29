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

"""Tests for the tests.utils.deploy_and_destroy integration-test helper."""

import os
from collections.abc import Generator
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from cats.cli import cats_cli
from tests.utils import deploy_and_destroy, make_config

REGION = "us-east-1"
ACCOUNT_KEYNAME = "testaccount"


@pytest.fixture
def mock_runner(mocker: MockerFixture) -> MagicMock:
    """CliRunner instance used by deploy_and_destroy, with every invoke succeeding."""
    runner: MagicMock = mocker.patch("tests.utils.CliRunner").return_value
    runner.invoke.return_value = mocker.MagicMock(exit_code=0, output="", exception=None)
    return runner


@pytest.fixture
def mock_failing_runner(mock_runner: MagicMock, mocker: MockerFixture) -> MagicMock:
    """CliRunner instance whose deploy invocation fails but cleanup succeeds."""
    mock_runner.invoke.side_effect = [
        mocker.MagicMock(exit_code=1, output="deploy blew up", exception=None),
        mocker.MagicMock(exit_code=0, output="", exception=None),
    ]
    return mock_runner


@pytest.fixture(autouse=True)
def mock_deleted_stack(mocker: MockerFixture) -> MagicMock:
    """Treat stacks as absent after successful CLI cleanup unless a test overrides it."""
    mock: MagicMock = mocker.patch("tests.utils.get_stack_status", create=True, return_value=None)
    return mock


def make_fixture_generator(tmp_path_factory: pytest.TempPathFactory, dir_name: str) -> Generator[str | list[str], None, None]:
    """Build the deploy_and_destroy generator under test."""
    return deploy_and_destroy(tmp_path_factory, make_config(), ACCOUNT_KEYNAME, REGION, dir_name)


def test_failed_deploy_still_destroys_stack(tmp_path_factory: pytest.TempPathFactory, mock_failing_runner: MagicMock, mocker: MockerFixture) -> None:
    # arrange
    generator = make_fixture_generator(tmp_path_factory, "failed-deploy-destroys")
    expected = [
        mocker.call(cats_cli, ["infra", "deploy", ACCOUNT_KEYNAME]),
        mocker.call(cats_cli, ["infra", "destroy", ACCOUNT_KEYNAME]),
    ]

    # act
    with pytest.raises(RuntimeError):
        next(generator)

    # assert
    assert expected == mock_failing_runner.invoke.call_args_list


def test_failed_deploy_restores_existing_cats_config(
    tmp_path_factory: pytest.TempPathFactory,
    mock_failing_runner: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # arrange
    original_config = "/original/cats.yaml"
    monkeypatch.setenv("CATS_CONFIG", original_config)
    generator = make_fixture_generator(tmp_path_factory, "failed-deploy-restores-env")

    # act
    with pytest.raises(RuntimeError):
        next(generator)

    # assert
    assert original_config == os.environ["CATS_CONFIG"]


def test_failed_deploy_unsets_cats_config_when_previously_absent(
    tmp_path_factory: pytest.TempPathFactory,
    mock_failing_runner: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # arrange
    monkeypatch.delenv("CATS_CONFIG", raising=False)
    generator = make_fixture_generator(tmp_path_factory, "failed-deploy-unsets-env")

    # act
    with pytest.raises(RuntimeError):
        next(generator)

    # assert
    assert os.environ.get("CATS_CONFIG") is None


def test_failed_deploy_raises_runtime_error_with_cli_output(
    tmp_path_factory: pytest.TempPathFactory,
    mock_failing_runner: MagicMock,
) -> None:
    # arrange
    generator = make_fixture_generator(tmp_path_factory, "failed-deploy-raises")
    expected_message = "Deploy failed: deploy blew up"

    # act
    with pytest.raises(RuntimeError) as exc_info:
        next(generator)

    # assert
    assert expected_message == str(exc_info.value)


def test_successful_deploy_destroys_after_yield(
    tmp_path_factory: pytest.TempPathFactory,
    mock_runner: MagicMock,
    mocker: MockerFixture,
) -> None:
    # arrange
    generator = make_fixture_generator(tmp_path_factory, "successful-deploy")
    expected = [
        mocker.call(cats_cli, ["infra", "deploy", ACCOUNT_KEYNAME]),
        mocker.call(cats_cli, ["infra", "destroy", ACCOUNT_KEYNAME]),
    ]

    # act
    next(generator)
    with pytest.raises(StopIteration):
        next(generator)

    # assert
    assert expected == mock_runner.invoke.call_args_list


def test_successful_deploy_yields_account_keyname(
    tmp_path_factory: pytest.TempPathFactory,
    mock_runner: MagicMock,
) -> None:
    # arrange
    generator = make_fixture_generator(tmp_path_factory, "successful-deploy-yield")

    # act
    result = next(generator)
    generator.close()  # run the destroy/teardown half so the env var is restored

    # assert
    assert result == ACCOUNT_KEYNAME


def test_destroy_cli_failure_raises_runtime_error(
    tmp_path_factory: pytest.TempPathFactory,
    mock_runner: MagicMock,
    mocker: MockerFixture,
) -> None:
    # arrange
    mock_runner.invoke.side_effect = [
        mocker.MagicMock(exit_code=0, output="", exception=None),
        mocker.MagicMock(exit_code=1, output="destroy blew up", exception=None),
    ]
    generator = make_fixture_generator(tmp_path_factory, "destroy-cli-failure")
    next(generator)
    expected_message = "Destroy failed for CATS-test-infra-testaccount: destroy blew up"

    # act
    with pytest.raises(RuntimeError) as exc_info:
        next(generator)

    # assert
    assert expected_message == str(exc_info.value)


def test_destroy_remaining_stack_raises_runtime_error(
    tmp_path_factory: pytest.TempPathFactory,
    mock_runner: MagicMock,
    mock_deleted_stack: MagicMock,
) -> None:
    # arrange
    mock_deleted_stack.return_value = "DELETE_FAILED"
    generator = make_fixture_generator(tmp_path_factory, "destroy-remaining-stack")
    next(generator)
    expected_message = "Destroy completed but stack CATS-test-infra-testaccount remains in DELETE_FAILED"

    # act
    with pytest.raises(RuntimeError) as exc_info:
        next(generator)

    # assert
    assert expected_message == str(exc_info.value)


def test_destroy_failure_restores_existing_cats_config(
    tmp_path_factory: pytest.TempPathFactory,
    mock_runner: MagicMock,
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # arrange
    original_config = "/original/cats.yaml"
    monkeypatch.setenv("CATS_CONFIG", original_config)
    mock_runner.invoke.side_effect = [
        mocker.MagicMock(exit_code=0, output="", exception=None),
        mocker.MagicMock(exit_code=1, output="destroy blew up", exception=None),
    ]
    generator = make_fixture_generator(tmp_path_factory, "destroy-restores-env")
    next(generator)

    # act
    with pytest.raises(RuntimeError):
        next(generator)

    # assert
    assert original_config == os.environ["CATS_CONFIG"]


def test_destroy_all_checks_every_stack_is_absent(
    tmp_path_factory: pytest.TempPathFactory,
    mock_runner: MagicMock,
    mock_deleted_stack: MagicMock,
) -> None:
    # arrange
    account_keynames = ["account-a", "account-b"]
    generator = deploy_and_destroy(
        tmp_path_factory,
        make_config(accounts={name: "111111111111" for name in account_keynames}),
        account_keynames,
        REGION,
        "destroy-all-final-state",
        deploy_all=True,
    )
    expected_stack_names = [
        "CATS-test-infra-account-a",
        "CATS-test-infra-account-b",
    ]

    # act
    next(generator)
    with pytest.raises(StopIteration):
        next(generator)

    # assert
    actual_stack_names = [call.args[1] for call in mock_deleted_stack.call_args_list]
    assert expected_stack_names == actual_stack_names
