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

"""Tests for cats.resources.prepare_udp_receiver.

The receiving account is constructed so its receipt token is registered, and HOST must be one of its listeners.
"""

import re
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from cats.exceptions import CatsUsageError
from cats.resources import prepare_udp_receiver
from tests.utils import make_deployed_ec2

LISTENER_DNS = "ip-10-0-1-5.eu-west-1.compute.internal"
OTHER_LISTENER_DNS = "ip-10-0-2-9.eu-west-1.compute.internal"


@pytest.fixture
def mock_get_account(mocker: MockerFixture) -> MagicMock:
    """Patch get_account as prepare_udp_receiver sees it; the receiver has two listeners."""
    receiver = MagicMock()
    receiver._deployed_network_handlers = {
        "private": MagicMock(ec2=make_deployed_ec2(dns_name=LISTENER_DNS)),
        "public": MagicMock(ec2=make_deployed_ec2(dns_name=OTHER_LISTENER_DNS)),
    }
    mock: MagicMock = mocker.patch("cats.resources.get_account", return_value=receiver)
    return mock


def test_constructs_the_receiving_account_from_the_given_config(mock_get_account: MagicMock) -> None:
    # act - construction is what registers the receiver's receipt token for udp_call to find
    prepare_udp_receiver("/tmp/cats.yaml", "receiver", LISTENER_DNS)  # noqa: S108

    # assert
    mock_get_account.assert_called_once_with("/tmp/cats.yaml", "receiver")  # noqa: S108


def test_returns_the_receiving_account(mock_get_account: MagicMock) -> None:
    # act
    result = prepare_udp_receiver(None, "receiver", LISTENER_DNS)

    # assert
    assert result is mock_get_account.return_value


def test_host_that_is_not_one_of_its_listeners_is_a_usage_error(mock_get_account: MagicMock) -> None:
    # arrange - sending anyway would give SENT and then received=False for a datagram CATS itself dropped
    expected_message = (
        "host '10.0.1.5' is not a CATS listener of receiving account 'receiver'. "
        + f"Address a listener by its ec2_dns_name: {LISTENER_DNS}, {OTHER_LISTENER_DNS}. "
        + "For a UDP target that is not a CATS listener, omit the receiving account."
    )

    # act & assert
    with pytest.raises(CatsUsageError, match=f"^{re.escape(expected_message)}$"):
        prepare_udp_receiver(None, "receiver", "10.0.1.5")


def test_account_with_no_listeners_says_so(mock_get_account: MagicMock) -> None:
    # arrange
    mock_get_account.return_value._deployed_network_handlers = {}
    expected_message = (
        f"host '{LISTENER_DNS}' is not a CATS listener of receiving account 'receiver'. "
        + "Address a listener by its ec2_dns_name: (no listeners deployed). "
        + "For a UDP target that is not a CATS listener, omit the receiving account."
    )

    # act & assert
    with pytest.raises(CatsUsageError, match=f"^{re.escape(expected_message)}$"):
        prepare_udp_receiver(None, "receiver", LISTENER_DNS)
