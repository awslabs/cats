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

"""Tests for get_current_account_id: the deploy identity's account, read from the environment's credential chain."""

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import NoCredentialsError, NoRegionError
from pytest_mock import MockerFixture

from cats.cfn.session import get_current_account_id
from cats.exceptions import CatsInfrastructureError


@pytest.fixture
def mock_sts(mocker: MockerFixture) -> MagicMock:
    """Replace the environment session's STS client; returns the client mock."""
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "111111111111", "Arn": "arn:aws:iam::111111111111:user/deployer", "UserId": "AIDAEXAMPLE"}
    environment_session = MagicMock()
    environment_session.client.return_value = sts
    mocker.patch("cats.cfn.session.attributed_session", return_value=environment_session)
    return sts


def test_returns_the_account_of_the_caller_identity(mock_sts: MagicMock) -> None:
    # act
    result = get_current_account_id()

    # assert
    assert result == "111111111111"


def test_environment_without_an_identity_is_an_infrastructure_error(mock_sts: MagicMock) -> None:
    """No credentials is the environment's failure, reported as a CatsError (one diagnostic line), never a botocore traceback."""
    # arrange
    mock_sts.get_caller_identity.side_effect = NoCredentialsError()
    expected_message = (
        "Cannot reach AWS to read the caller identity: Unable to locate credentials. "
        + "The environment CATS runs in needs a working AWS identity (a profile, SSO, or environment variables) and a Region."
    )

    # act & assert
    with pytest.raises(CatsInfrastructureError) as exc_info:
        get_current_account_id()
    assert str(exc_info.value) == expected_message


def test_a_botocore_message_that_ends_with_a_period_reads_as_one_sentence(mock_sts: MagicMock) -> None:
    """NoRegionError's text is 'You must specify a region.'; the diagnostic must not say 'region.. The'."""
    # arrange
    mock_sts.get_caller_identity.side_effect = NoRegionError()
    expected_message = (
        "Cannot reach AWS to read the caller identity: You must specify a region. "
        + "The environment CATS runs in needs a working AWS identity (a profile, SSO, or environment variables) and a Region."
    )

    # act & assert
    with pytest.raises(CatsInfrastructureError) as exc_info:
        get_current_account_id()
    assert str(exc_info.value) == expected_message
