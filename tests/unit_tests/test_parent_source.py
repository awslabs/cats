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

"""Tests for cats.utils.aws_profile._parent_source: naming this process's identity in profile terms."""

from unittest.mock import MagicMock

import pytest
from botocore.credentials import (
    AssumeRoleProvider,
    AssumeRoleWithWebIdentityProvider,
    ConfigProvider,
    ContainerProvider,
    EnvProvider,
    InstanceMetadataProvider,
    ProcessProvider,
    SharedCredentialProvider,
    SSOProvider,
)

from cats.exceptions import CatsUsageError
from cats.utils.aws_profile import _parent_source, _ParentSource


def make_session(method: str | None, profile_name: str = "parent") -> MagicMock:
    """A boto3 Session whose credentials came from `method`; None means no credentials resolved."""
    session = MagicMock()
    session.profile_name = profile_name
    if method is None:
        session.get_credentials.return_value = None
    else:
        session.get_credentials.return_value.method = method
    return session


@pytest.fixture
def no_web_identity_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN", "AWS_ROLE_SESSION_NAME"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    "method",
    [SharedCredentialProvider.METHOD, ConfigProvider.METHOD, SSOProvider.METHOD, AssumeRoleProvider.METHOD, ProcessProvider.METHOD],
)
def test_profile_based_credentials_name_the_profile_as_source(method: str) -> None:
    # act
    result = _parent_source(make_session(method, profile_name="work"), "accA")

    # assert
    assert result == _ParentSource(key="source_profile", value="work")


@pytest.mark.parametrize(
    ("method", "credential_source"),
    [
        (EnvProvider.METHOD, "Environment"),
        (InstanceMetadataProvider.METHOD, "Ec2InstanceMetadata"),
        (ContainerProvider.METHOD, "EcsContainer"),
    ],
)
def test_ambient_credentials_name_a_credential_source(method: str, credential_source: str) -> None:
    # act
    result = _parent_source(make_session(method), "accA")

    # assert
    assert result == _ParentSource(key="credential_source", value=credential_source)


def test_web_identity_from_a_profile_names_the_profile_as_source(no_web_identity_environment: None) -> None:
    # act
    result = _parent_source(make_session(AssumeRoleWithWebIdentityProvider.METHOD, profile_name="oidc"), "accA")

    # assert
    assert result == _ParentSource(key="source_profile", value="oidc")


def test_web_identity_from_the_environment_materializes_a_parent_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    # arrange
    monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", "/var/run/secrets/token")
    monkeypatch.setenv("AWS_ROLE_ARN", "arn:aws:iam::222222222222:role/pod")
    monkeypatch.delenv("AWS_ROLE_SESSION_NAME", raising=False)

    # act
    result = _parent_source(make_session(AssumeRoleWithWebIdentityProvider.METHOD), "accA")

    # assert
    expected = _ParentSource(
        key="source_profile",
        value="cats-parent-accA",
        parent_profile=("cats-parent-accA", {"role_arn": "arn:aws:iam::222222222222:role/pod", "web_identity_token_file": "/var/run/secrets/token"}),
    )
    assert result == expected


def test_web_identity_session_name_from_the_environment_is_carried(monkeypatch: pytest.MonkeyPatch) -> None:
    # arrange
    monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", "/var/run/secrets/token")
    monkeypatch.setenv("AWS_ROLE_ARN", "arn:aws:iam::222222222222:role/pod")
    monkeypatch.setenv("AWS_ROLE_SESSION_NAME", "ci-job-7")

    # act
    result = _parent_source(make_session(AssumeRoleWithWebIdentityProvider.METHOD), "accA")

    # assert
    expected = {
        "role_arn": "arn:aws:iam::222222222222:role/pod",
        "web_identity_token_file": "/var/run/secrets/token",
        "role_session_name": "ci-job-7",
    }
    assert result.parent_profile == ("cats-parent-accA", expected)


def test_unknown_method_is_refused_by_name() -> None:
    # act / assert
    expected = (
        "This process holds AWS credentials from 'boto-config', which an AWS profile cannot name as its source. "
        + "Run with a named profile (AWS_PROFILE), environment variables, an instance role or a container role."
    )
    with pytest.raises(CatsUsageError) as excinfo:
        _parent_source(make_session("boto-config"), "accA")
    assert str(excinfo.value) == expected


def test_no_credentials_is_refused() -> None:
    # act / assert
    expected = "No AWS credentials resolved in this environment, so there is no identity for the profile to assume the role from."
    with pytest.raises(CatsUsageError) as excinfo:
        _parent_source(make_session(None), "accA")
    assert str(excinfo.value) == expected


def test_line_is_the_key_value_pair_as_written() -> None:
    # act
    line = _ParentSource(key="credential_source", value="Environment").line

    # assert
    assert line == "credential_source = Environment"
