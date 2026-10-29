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

"""Tests for tests.integration.constants module.

Covers the environment plumbing without AWS: whether a missing or mismatched account skips.
What no test can cover is whether those values still name live AWS resources - that needs
an integration run.
"""

import importlib
from types import ModuleType

import pytest
from pytest_mock import MockerFixture

import tests.integration.constants

CATS_INTEG_VARIABLES = (
    "CATS_INTEG_REGION",
    "CATS_INTEG_ACCOUNT_ID",
    "CATS_INTEG_CROSS_ACCOUNT_ID",
    "CATS_INTEG_RUN_ID",
)

ACCOUNT_A = "1" * 12
ACCOUNT_B = "2" * 12


def reload_constants(monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture, env: dict[str, str], current_account: str | None) -> ModuleType:
    """Reload the module under a given environment, with STS stubbed to current_account."""
    for name in CATS_INTEG_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    client = mocker.patch("boto3.client").return_value
    if current_account is None:
        client.get_caller_identity.side_effect = RuntimeError("no credentials")
    else:
        client.get_caller_identity.return_value = {"Account": current_account}
    return importlib.reload(tests.integration.constants)


def test_unset_account_id_skips_naming_the_variable(monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture) -> None:
    # act
    constants = reload_constants(monkeypatch, mocker, {}, current_account=ACCOUNT_A)

    # assert
    expected = (True, "Set CATS_INTEG_ACCOUNT_ID to the 12-digit account these tests deploy into")
    assert expected == (constants.skip_wrong_account.args[0], constants.skip_wrong_account.kwargs["reason"])


def test_matching_account_does_not_skip(monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture) -> None:
    # act
    constants = reload_constants(monkeypatch, mocker, {"CATS_INTEG_ACCOUNT_ID": ACCOUNT_A}, current_account=ACCOUNT_A)

    # assert
    assert constants.skip_wrong_account.args[0] is False


def test_mismatched_account_skips_naming_both_accounts(monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture) -> None:
    # act
    constants = reload_constants(monkeypatch, mocker, {"CATS_INTEG_ACCOUNT_ID": ACCOUNT_A}, current_account=ACCOUNT_B)

    # assert
    expected = (True, f"Requires AWS credentials for account {ACCOUNT_A} in eu-west-1, currently {ACCOUNT_B}")
    assert expected == (constants.skip_wrong_account.args[0], constants.skip_wrong_account.kwargs["reason"])


def test_region_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture) -> None:
    # act
    constants = reload_constants(monkeypatch, mocker, {"CATS_INTEG_REGION": "us-east-1"}, current_account=None)

    # assert
    assert constants.REGION == "us-east-1"


def test_unset_cross_account_skips_naming_the_variable(monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture) -> None:
    """The cross account's network comes from its base stack, so the account ID is all a run needs."""
    # act
    constants = reload_constants(monkeypatch, mocker, {}, current_account=None)

    # assert
    expected = (True, "Set CATS_INTEG_CROSS_ACCOUNT_ID for cross-account tests")
    assert expected == (constants.skip_no_cross_account.args[0], constants.skip_no_cross_account.kwargs["reason"])


def test_cross_account_id_alone_does_not_skip(monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture) -> None:
    # act
    constants = reload_constants(monkeypatch, mocker, {"CATS_INTEG_CROSS_ACCOUNT_ID": ACCOUNT_B}, current_account=None)

    # assert
    assert constants.skip_no_cross_account.args[0] is False


def test_cross_account_network_is_read_by_parameter_name(monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture) -> None:
    """The names infra/integ-test-cross-vpc.yaml writes; CATS resolves them in the cross account."""
    # act
    constants = reload_constants(monkeypatch, mocker, {}, current_account=None)

    # assert
    expected = ("/cats/integ-test/cross-vpc-id", "/cats/integ-test/cross-subnet-id")
    assert expected == (constants.CROSS_VPC_SSM_PARAM, constants.CROSS_SUBNET_SSM_PARAM)


def test_stack_prefix_uses_run_id(monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture) -> None:
    # act
    constants = reload_constants(
        monkeypatch,
        mocker,
        {"CATS_INTEG_RUN_ID": "12345678"},
        current_account=None,
    )

    # assert
    assert constants.stack_prefix("lam") == "C-i12345678-lam-"


def test_stack_prefix_defaults_to_stable_local_namespace(
    monkeypatch: pytest.MonkeyPatch,
    mocker: MockerFixture,
) -> None:
    # act
    constants = reload_constants(monkeypatch, mocker, {}, current_account=None)

    # assert
    assert constants.stack_prefix("lam") == "C-ilocal-lam-"
