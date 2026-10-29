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

"""Tests for cats/resources/session_interact.py - one AssumeRole per role, credentials that refresh themselves."""

import json
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import boto3
import botocore
import pytest
from botocore.credentials import RefreshableCredentials
from botocore.exceptions import ClientError, NoCredentialsError
from moto import mock_aws
from pytest_mock import MockerFixture

from cats.config import CatsAdditionalTestRole
from cats.exceptions import CatsInfrastructureError
from cats.resources.session_interact import (
    DEFAULT_ROLE_SESSION_NAME,
    ROLE_SESSION_DURATION_SECONDS,
    SessionHandler,
    current_role_session_name,
    get_role_arn,
    get_test_session,
    set_role_session_name,
)
from cats.user_agent import USER_AGENT_EXTRA, CatsTestPack, register_test_pack, user_agent_extra
from tests.utils import capture_wire_user_agent

ACCOUNT_ID = "123456789012"
ACCOUNT_NAME = "testaccount"
REGION = "eu-west-1"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT_ID}:role/CATS-test-infra-testaccount-default-test-role"
TRUST_ANYONE = json.dumps({"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"AWS": "*"}, "Action": "sts:AssumeRole"}]})


@pytest.fixture
def assumable_role() -> Generator[None, None, None]:
    """A moto account holding the default test role."""
    with mock_aws():
        iam = boto3.client("iam", region_name="us-east-1")
        iam.create_role(RoleName="CATS-test-infra-testaccount-default-test-role", AssumeRolePolicyDocument=TRUST_ANYONE)
        yield


def _credentials(access_key: str) -> dict[str, object]:
    return {
        "AccessKeyId": access_key,
        "SecretAccessKey": "secret",
        "SessionToken": "token",
        "Expiration": datetime.now(UTC) + timedelta(hours=1),
    }


@pytest.fixture
def mock_sts(mocker: MockerFixture) -> MagicMock:
    """Replace the environment session's STS client; returns the client mock with assume_role pre-programmed."""
    sts = MagicMock()
    sts.assume_role.return_value = {"Credentials": _credentials("AKIAFIRST")}
    environment_session = MagicMock()
    environment_session.client.return_value = sts
    mocker.patch("cats.resources.session_interact.attributed_session", return_value=environment_session)
    return sts


# =============================================================================
# get_test_session
# =============================================================================


def test_session_carries_refreshable_credentials_for_the_role(assumable_role: None) -> None:
    # act
    session = get_test_session(ROLE_ARN, ACCOUNT_NAME, REGION)
    credentials = session.get_credentials()

    # assert
    assert (type(credentials), session.region_name) == (RefreshableCredentials, REGION)


def test_session_calls_are_made_as_the_assumed_role(assumable_role: None) -> None:
    """Outside a reported pytest run the principal ends in the cats-cli default: what `cats run`, MCP and scripts show in CloudTrail."""
    # act
    session = get_test_session(ROLE_ARN, ACCOUNT_NAME, REGION)

    # assert
    assert (
        session.client("sts").get_caller_identity()["Arn"]
        == f"arn:aws:sts::{ACCOUNT_ID}:assumed-role/CATS-test-infra-testaccount-default-test-role/cats-cli"
    )


@pytest.fixture
def empty_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test pack declared in this process."""
    monkeypatch.setattr("cats.user_agent._registered_test_packs", {})


@pytest.fixture
def registered_pack(monkeypatch: pytest.MonkeyPatch) -> None:
    """One pack in an otherwise empty registry, as the pytest plugin leaves it after configure."""
    monkeypatch.setattr("cats.user_agent._registered_test_packs", {})
    register_test_pack(CatsTestPack(name="acme-controls", version="0.99.4"))


def cats_tokens(user_agent: str) -> list[str]:
    return [token for token in user_agent.split(" ") if token.startswith(("AwsCats/", "AwsCatsPack/"))]


def test_session_identifies_cats_in_the_user_agent(assumable_role: None, empty_registry: None) -> None:
    # arrange
    expected = [USER_AGENT_EXTRA]
    session = get_test_session(ROLE_ARN, ACCOUNT_NAME, REGION)

    # act
    user_agent = capture_wire_user_agent(session)

    # assert
    assert expected == cats_tokens(user_agent)


def test_test_role_session_sends_the_pack_token_on_the_wire(assumable_role: None, registered_pack: None) -> None:
    """The test-role session is the one behind every probe and account.role client; it must carry the pack too."""
    # arrange
    expected = [USER_AGENT_EXTRA, "AwsCatsPack/acme-controls#0.99.4"]
    session = get_test_session(ROLE_ARN, ACCOUNT_NAME, REGION)

    # act
    user_agent = capture_wire_user_agent(session)

    # assert
    assert expected == cats_tokens(user_agent)


def test_test_role_session_tail_matches_the_shared_suffix(assumable_role: None, registered_pack: None) -> None:
    """get_test_session and attributed_session must never diverge: one stamping site, then boto3's trailer."""
    # arrange
    expected = f"{user_agent_extra()} Botocore/{botocore.__version__}"
    session = get_test_session(ROLE_ARN, ACCOUNT_NAME, REGION)

    # act
    user_agent = capture_wire_user_agent(session)

    # assert
    assert expected == " ".join(user_agent.split(" ")[-3:])


def test_role_is_assumed_once_at_construction(mock_sts: MagicMock) -> None:
    # act - a role that cannot be assumed must fail here, not on the first client call
    get_test_session(ROLE_ARN, ACCOUNT_NAME, REGION)

    # assert
    mock_sts.assume_role.assert_called_once_with(RoleArn=ROLE_ARN, RoleSessionName="cats-cli", DurationSeconds=ROLE_SESSION_DURATION_SECONDS)


def test_role_session_name_is_passed_through(mock_sts: MagicMock) -> None:
    # act
    get_test_session(ROLE_ARN, ACCOUNT_NAME, REGION, role_session_name="cats-ci")

    # assert
    assert mock_sts.assume_role.call_args.kwargs["RoleSessionName"] == "cats-ci"


@pytest.fixture
def run_session_name() -> str:
    """The process-level name the pytest plugin sets for a reported run; the unit conftest resets it per test."""
    set_role_session_name("cats-20260924T105317Z")
    return "cats-20260924T105317Z"


def test_default_session_name_is_cats_cli() -> None:
    # assert - what CloudTrail shows for cats run, the MCP tools, and a bare AccountUnderTest in a script
    assert (DEFAULT_ROLE_SESSION_NAME, current_role_session_name()) == ("cats-cli", "cats-cli")


def test_process_level_session_name_is_used_when_none_is_passed(mock_sts: MagicMock, run_session_name: str) -> None:
    """SessionHandler passes no name, so this is the path every account fixture's AssumeRole takes."""
    # act
    get_test_session(ROLE_ARN, ACCOUNT_NAME, REGION)

    # assert
    assert mock_sts.assume_role.call_args.kwargs["RoleSessionName"] == run_session_name


def test_credential_refresh_reuses_the_session_name_it_was_created_with(mock_sts: MagicMock, run_session_name: str) -> None:
    """A two-hour run is one principal end to end: the refresh must not pick up a name set later in the process."""
    # arrange - the first credentials expire at once, so the first client call refreshes
    mock_sts.assume_role.side_effect = [
        {"Credentials": {**_credentials("AKIAFIRST"), "Expiration": datetime.now(UTC) - timedelta(seconds=1)}},
        {"Credentials": _credentials("AKIASECOND")},
    ]
    session = get_test_session(ROLE_ARN, ACCOUNT_NAME, REGION)
    set_role_session_name("cats-20260924T120000Z")

    # act
    credentials = session.get_credentials()
    assert credentials is not None
    credentials.get_frozen_credentials()

    # assert
    expected = [run_session_name, run_session_name]
    assert expected == [call.kwargs["RoleSessionName"] for call in mock_sts.assume_role.call_args_list]


def test_expired_credentials_are_refreshed_by_assuming_the_role_again(mock_sts: MagicMock) -> None:
    # arrange - the first credentials expire at once; botocore must refresh before handing them out
    mock_sts.assume_role.side_effect = [
        {"Credentials": {**_credentials("AKIAFIRST"), "Expiration": datetime.now(UTC) - timedelta(seconds=1)}},
        {"Credentials": _credentials("AKIASECOND")},
    ]
    session = get_test_session(ROLE_ARN, ACCOUNT_NAME, REGION)

    # act
    credentials = session.get_credentials()
    assert credentials is not None
    frozen = credentials.get_frozen_credentials()

    # assert
    assert (frozen.access_key, mock_sts.assume_role.call_count) == ("AKIASECOND", 2)


def test_assume_role_failure_is_an_infrastructure_error(mock_sts: MagicMock) -> None:
    # arrange
    mock_sts.assume_role.side_effect = ClientError({"Error": {"Code": "AccessDenied", "Message": "not allowed"}}, "AssumeRole")
    expected_message = (
        f"Failed to assume test role '{ROLE_ARN}' for target account '{ACCOUNT_NAME}' (defined in cats.yaml). "
        + "Test roles are created by 'cats infra deploy' and let tests run with scoped permissions. "
        + "Check: 1) You have valid AWS credentials, "
        + f"2) CATS infrastructure is deployed: cats infra deploy {ACCOUNT_NAME}, "
        + "3) The role trusts your current identity. "
        + "AWS error: AccessDenied"
    )

    # act & assert
    with pytest.raises(CatsInfrastructureError) as exc_info:
        get_test_session(ROLE_ARN, ACCOUNT_NAME, REGION)
    assert str(exc_info.value) == expected_message


# =============================================================================
# get_role_arn
# =============================================================================


def test_role_arn_uses_the_partition() -> None:
    # act
    result = get_role_arn("CATS-test-infra-testaccount-default-test-role", ACCOUNT_ID, "aws-cn")

    # assert
    assert result == f"arn:aws-cn:iam::{ACCOUNT_ID}:role/CATS-test-infra-testaccount-default-test-role"


# =============================================================================
# SessionHandler
# =============================================================================


@pytest.fixture
def mock_get_test_session(mocker: MockerFixture) -> MagicMock:
    mock: MagicMock = mocker.patch(
        "cats.resources.session_interact.get_test_session", side_effect=lambda **kwargs: MagicMock(name=kwargs["role_arn"])
    )
    return mock


def make_handler(sessions: dict[str, boto3.Session], additional_roles: dict[str, CatsAdditionalTestRole] | None = None) -> SessionHandler:
    return SessionHandler(
        target_account_id=ACCOUNT_ID,
        target_account_name=ACCOUNT_NAME,
        resource_prefix="CATS-test-infra-testaccount",
        additional_roles=additional_roles
        if additional_roles is not None
        else {"read_only": CatsAdditionalTestRole(managed_policies=["ReadOnlyAccess"])},
        aws_region=REGION,
        partition="aws",
        sessions=sessions,
    )


def test_default_session_assumes_the_default_test_role(mock_get_test_session: MagicMock) -> None:
    # act
    _ = make_handler({}).default

    # assert
    mock_get_test_session.assert_called_once_with(role_arn=ROLE_ARN, account_name=ACCOUNT_NAME, aws_region=REGION)


def test_default_session_is_assumed_once_and_shared(mock_get_test_session: MagicMock) -> None:
    # arrange - every probe on an account reads .session; each read must not be an AssumeRole
    sessions: dict[str, boto3.Session] = {}
    handler = make_handler(sessions)

    # act
    first = handler.default
    second = handler.default

    # assert
    assert (first is second, mock_get_test_session.call_count) == (True, 1)


def test_default_session_is_shared_across_handler_instances(mock_get_test_session: MagicMock) -> None:
    # arrange - AccountUnderTest builds a fresh SessionHandler on every .role access; the cache lives in the account
    sessions: dict[str, boto3.Session] = {}

    # act
    first = make_handler(sessions).default
    second = make_handler(sessions).default

    # assert
    assert (first is second, mock_get_test_session.call_count) == (True, 1)


def test_additional_role_assumes_the_prefixed_role(mock_get_test_session: MagicMock) -> None:
    # act
    _ = make_handler({}).read_only

    # assert
    mock_get_test_session.assert_called_once_with(
        role_arn=f"arn:aws:iam::{ACCOUNT_ID}:role/CATS-test-infra-testaccount-read_only", account_name=ACCOUNT_NAME, aws_region=REGION
    )


def test_additional_role_session_is_cached_separately_from_default(mock_get_test_session: MagicMock) -> None:
    # arrange
    handler = make_handler({})

    # act
    default = handler.default
    read_only = handler.read_only
    read_only_again = handler.read_only

    # assert
    assert (default is read_only, read_only is read_only_again, mock_get_test_session.call_count) == (False, True, 2)


def test_unknown_role_attribute_raises_attribute_error(mock_get_test_session: MagicMock) -> None:
    """The message lists the roles that exist, as AccountUnderTest's does for targets."""
    # act & assert
    with pytest.raises(
        AttributeError, match=r"^Role 'auditor' is not a name under additional_roles in cats\.yaml\. Available roles: default, read_only$"
    ):
        _ = make_handler({}).auditor
    assert mock_get_test_session.called is False


def test_environment_without_an_identity_is_an_infrastructure_error(mock_sts: MagicMock) -> None:
    """The environment's own botocore failures (no identity, no endpoint) are wrapped: callers see a CatsError."""
    # arrange
    mock_sts.assume_role.side_effect = NoCredentialsError()
    expected_message = (
        f"Cannot reach AWS to assume test role '{ROLE_ARN}' for target account '{ACCOUNT_NAME}': Unable to locate credentials. "
        + "The environment CATS runs in needs a working AWS identity (a profile, SSO, or environment variables) and a Region."
    )

    # act & assert
    with pytest.raises(CatsInfrastructureError) as exc_info:
        get_test_session(ROLE_ARN, ACCOUNT_NAME, REGION)
    assert str(exc_info.value) == expected_message
