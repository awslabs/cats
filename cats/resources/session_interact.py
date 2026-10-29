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

import boto3
from boto3.session import Session
from botocore.credentials import CredentialProvider, RefreshableCredentials
from botocore.exceptions import BotoCoreError, ClientError

from cats.config import CatsAdditionalTestRole
from cats.exceptions import CatsInfrastructureError
from cats.user_agent import attributed_botocore_session, attributed_session

# The default role session length; STS refuses anything longer unless the role's
# MaxSessionDuration was raised, so the sessions below refresh instead of asking for more.
ROLE_SESSION_DURATION_SECONDS = 3600

DEFAULT_ROLE_SESSION_NAME = "cats-cli"
"""The RoleSessionName outside a reported pytest run: ``cats run``, the MCP tools, a script.

CloudTrail then tells the ways CATS assumes a role apart: a deploy is ``cats-infra``
(``cats.cfn.session``), a reported test run is ``cats-<run_id>`` (set by the pytest plugin), a
role assumed through a profile ``cats run write-profile`` wrote is ``cats-profile``
(:data:`PROFILE_ROLE_SESSION_NAME`), and anything else is this.
"""

PROFILE_ROLE_SESSION_NAME = "cats-profile"
"""The RoleSessionName carried by a profile that ``cats run write-profile`` writes.

A role assumed by an AWS tool THROUGH that profile is still CATS-attributable but is distinct in
CloudTrail from CATS's own ``cats run`` / MCP calls (``cats-cli``): it says the caller used a
CATS-written profile, not the CATS CLI itself. CATS never assumes a role under this name; it only
writes it into the stanza for the user's tool to use.
"""

_role_session_name = DEFAULT_ROLE_SESSION_NAME


def set_role_session_name(name: str) -> None:
    """Set the RoleSessionName every later :func:`get_test_session` in this process uses.

    The pytest plugin sets ``cats-<run_id>`` in ``pytest_configure``, on the controller and on
    every xdist worker, so every AssumeRole of one run is one principal in CloudTrail.
    """
    global _role_session_name
    _role_session_name = name


def current_role_session_name() -> str:
    """The RoleSessionName this process assumes test roles under."""
    return _role_session_name


class _AssumedRoleProvider(CredentialProvider):
    """Hands botocore the refreshable credentials of one assumed role, ahead of every ambient source."""

    METHOD = "cats-assume-role"

    def __init__(self, credentials: RefreshableCredentials) -> None:
        super().__init__()
        self._credentials = credentials

    def load(self) -> RefreshableCredentials:
        return self._credentials


def _assume_role(role_arn: str, account_name: str, role_session_name: str) -> dict[str, str]:
    """One AssumeRole call, shaped as the metadata botocore's refreshable credentials expect."""
    try:
        environment_sts_client = attributed_session().client("sts")
        creds = environment_sts_client.assume_role(
            RoleArn=role_arn,
            RoleSessionName=role_session_name,
            DurationSeconds=ROLE_SESSION_DURATION_SECONDS,
        )["Credentials"]
    except BotoCoreError as e:
        raise CatsInfrastructureError(
            f"Cannot reach AWS to assume test role '{role_arn}' for target account '{account_name}': {str(e).rstrip('.')}. "
            + "The environment CATS runs in needs a working AWS identity (a profile, SSO, or environment variables) and a Region."
        ) from e
    except ClientError as e:
        raise CatsInfrastructureError(
            f"Failed to assume test role '{role_arn}' for target account '{account_name}' (defined in cats.yaml). "
            + "Test roles are created by 'cats infra deploy' and let tests run with scoped permissions. "
            + "Check: 1) You have valid AWS credentials, "
            + f"2) CATS infrastructure is deployed: cats infra deploy {account_name}, "
            + "3) The role trusts your current identity. "
            + f"AWS error: {e.response['Error']['Code']}"
        ) from e
    return {
        "access_key": creds["AccessKeyId"],
        "secret_key": creds["SecretAccessKey"],
        "token": creds["SessionToken"],
        "expiry_time": creds["Expiration"].isoformat(),
    }


def get_test_session(role_arn: str, account_name: str, aws_region: str, role_session_name: str | None = None) -> Session:
    """Assume the test role from the environment's credential chain; the returned Session refreshes its own credentials.

    The first AssumeRole happens here, so a role that cannot be assumed fails at once. Later
    ones happen inside botocore as the hour-long role session nears expiry, so a Session held
    for a whole test run never raises ExpiredToken.

    ``role_session_name`` defaults to :func:`current_role_session_name`, resolved once here:
    the refreshes reuse it, so one Session is one principal for its whole life.
    """
    name = role_session_name if role_session_name is not None else current_role_session_name()

    def refresh() -> dict[str, str]:
        return _assume_role(role_arn, account_name, name)

    credentials = RefreshableCredentials.create_from_metadata(metadata=refresh(), refresh_using=refresh, method=_AssumedRoleProvider.METHOD)
    botocore_session = attributed_botocore_session()
    botocore_session.get_component("credential_provider").insert_before("env", _AssumedRoleProvider(credentials))
    return boto3.Session(botocore_session=botocore_session, region_name=aws_region)


def get_role_arn(role_name: str, account_id: str, partition: str) -> str:
    return f"arn:{partition}:iam::{account_id}:role/{role_name}"


class SessionHandler:
    """
    Allows AccountUnderTest to use additional roles by using a class property, by
    implementing a custom attribute lookup.

    For example: read_only_session = AccountUnderTest.role.read_only

    `default` is the one public name; every other attribute is underscore-private, so a role name
    never collides with the handler's own plumbing.

    Sessions are cached in the dict the owning AccountUnderTest passes in, one per role,
    so every probe and client on an account shares one assumed-role session instead of
    paying an AssumeRole each; the sessions refresh their own credentials.
    """

    def __init__(
        self,
        target_account_id: str,
        target_account_name: str,
        resource_prefix: str,
        additional_roles: dict[str, CatsAdditionalTestRole],
        aws_region: str,
        partition: str,
        sessions: dict[str, Session],
    ) -> None:
        self._role_names = frozenset(additional_roles)
        self._target_account_id = target_account_id
        self._target_account_name = target_account_name
        self._resource_prefix = resource_prefix
        self._aws_region = aws_region
        self._partition = partition
        self._sessions = sessions

    def _role_name_for(self, attr: str) -> str:
        """The IAM role name behind attribute ``attr``: ``default`` is the CATS-managed test role."""
        return f"{self._resource_prefix}-default-test-role" if attr == "default" else f"{self._resource_prefix}-{attr}"

    def _role_arn_for(self, attr: str) -> str:
        """The ARN the session for ``attr`` assumes.

        The one home of the derivation: ``cats run write-profile`` writes this ARN into a profile,
        so the profile and the session can never name different roles.
        """
        return get_role_arn(role_name=self._role_name_for(attr), account_id=self._target_account_id, partition=self._partition)

    def _session_for(self, attr: str) -> Session:
        role_name = self._role_name_for(attr)
        if role_name not in self._sessions:
            self._sessions[role_name] = get_test_session(
                role_arn=self._role_arn_for(attr),
                account_name=self._target_account_name,
                aws_region=self._aws_region,
            )
        return self._sessions[role_name]

    def __getattr__(self, attr: str) -> Session:
        """When an undefined attr on this class is referenced, try and return a boto3
        Session linked to an additional_role_name, or raise an AttributeError naming the roles that exist.
        """
        if attr.startswith("_") or attr not in self._role_names:
            available = ", ".join(sorted(self._role_names)) or "(none)"
            raise AttributeError(f"Role '{attr}' is not a name under additional_roles in cats.yaml. Available roles: default, {available}")
        return self._session_for(attr)

    @property
    def default(self) -> Session:
        return self._session_for("default")
