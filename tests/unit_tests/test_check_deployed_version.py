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

"""Tests for check_deployed_version: the runtime gate between the installed release and a deployed stack."""

import logging

import pytest
from pytest_mock import MockerFixture

from cats.exceptions import CatsInfrastructureError, CatsVersionMismatchError
from cats.resources.account_under_test import check_deployed_version

STACK_NAME = "CATS-test-infra-prod"
ACCOUNT_NAME = "prod"


@pytest.fixture(autouse=True)
def installed_version(mocker: MockerFixture) -> str:
    """Pin the installed release so expected messages survive version bumps."""
    mocker.patch("cats.resources.account_under_test.__version__", "1.0.0")
    return "1.0.0"


@pytest.fixture(autouse=True)
def no_skip_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ignore any escape hatch set in the developer's shell."""
    monkeypatch.delenv("CATS_SKIP_VERSION_CHECK", raising=False)


def test_matching_version_returns_silently(caplog: pytest.LogCaptureFixture) -> None:
    # arrange
    caplog.set_level(logging.WARNING)
    expected_records: list[str] = []

    # act
    check_deployed_version("1.0.0", STACK_NAME, ACCOUNT_NAME)

    # assert
    assert expected_records == caplog.messages


def test_other_release_raises_with_recreate_remediation() -> None:
    # arrange
    expected_error = (
        "CATS stack 'CATS-test-infra-prod' in target account 'prod' was deployed by CATS 0.99.0 "
        + "but CATS 1.0.0 is installed. Deployed listeners and handlers may not match this library. "
        + "Recreate it: cats infra destroy prod && cats infra deploy prod "
        + "(set CATS_SKIP_VERSION_CHECK=1 to bypass this check)"
    )

    # act
    with pytest.raises(CatsVersionMismatchError) as exc_info:
        check_deployed_version("0.99.0", STACK_NAME, ACCOUNT_NAME)

    # assert
    assert str(exc_info.value) == expected_error


def test_missing_tag_raises_with_not_created_by_cats_explanation() -> None:
    # arrange
    expected_error = (
        "CATS stack 'CATS-test-infra-prod' in target account 'prod' has no cats:version tag, "
        + "so CATS cannot confirm it matches installed CATS 1.0.0. It was not created by 'cats infra deploy'. "
        + "Recreate it: cats infra destroy prod && cats infra deploy prod "
        + "(set CATS_SKIP_VERSION_CHECK=1 to bypass this check)"
    )

    # act
    with pytest.raises(CatsVersionMismatchError) as exc_info:
        check_deployed_version(None, STACK_NAME, ACCOUNT_NAME)

    # assert
    assert str(exc_info.value) == expected_error


def test_mismatch_error_is_an_infrastructure_error() -> None:
    # arrange
    expected = True

    # act
    result = issubclass(CatsVersionMismatchError, CatsInfrastructureError)

    # assert
    assert expected == result


def test_skip_env_var_downgrades_other_release_to_warning(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    # arrange
    monkeypatch.setenv("CATS_SKIP_VERSION_CHECK", "1")
    caplog.set_level(logging.WARNING)
    expected_records = [
        "CATS_SKIP_VERSION_CHECK is set: CATS stack 'CATS-test-infra-prod' in target account 'prod' was deployed by CATS 0.99.0 "
        + "but CATS 1.0.0 is installed. Deployed listeners and handlers may not match this library. "
        + "Results may not reflect this library."
    ]

    # act
    check_deployed_version("0.99.0", STACK_NAME, ACCOUNT_NAME)

    # assert
    assert expected_records == caplog.messages


def test_skip_env_var_downgrades_missing_tag_to_warning(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    # arrange
    monkeypatch.setenv("CATS_SKIP_VERSION_CHECK", "1")
    caplog.set_level(logging.WARNING)
    expected_records = [
        "CATS_SKIP_VERSION_CHECK is set: CATS stack 'CATS-test-infra-prod' in target account 'prod' has no cats:version tag, "
        + "so CATS cannot confirm it matches installed CATS 1.0.0. It was not created by 'cats infra deploy'. "
        + "Results may not reflect this library."
    ]

    # act
    check_deployed_version(None, STACK_NAME, ACCOUNT_NAME)

    # assert
    assert expected_records == caplog.messages


def test_skip_env_var_with_matching_version_logs_nothing(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    # arrange
    monkeypatch.setenv("CATS_SKIP_VERSION_CHECK", "1")
    caplog.set_level(logging.WARNING)
    expected_records: list[str] = []

    # act
    check_deployed_version("1.0.0", STACK_NAME, ACCOUNT_NAME)

    # assert
    assert expected_records == caplog.messages


def test_skip_env_var_set_to_zero_still_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # arrange
    monkeypatch.setenv("CATS_SKIP_VERSION_CHECK", "0")

    # act / assert
    with pytest.raises(CatsVersionMismatchError):
        check_deployed_version("0.99.0", STACK_NAME, ACCOUNT_NAME)
