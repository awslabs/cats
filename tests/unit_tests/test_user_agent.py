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

"""Tests for cats.user_agent.attributed_session.

Assertions run against the real assembled User-Agent header captured via a
``before-send`` event hook, not against session config, because config-level
assertions cannot prove the suffix survives header assembly.
"""

import boto3
import botocore
import pytest
from botocore.config import Config

from cats.user_agent import USER_AGENT_EXTRA, CatsTestPack, attributed_session, register_test_pack
from cats.version import __version__
from tests.utils import capture_wire_user_agent


def make_session() -> boto3.Session:
    """Attributed session with fake credentials; requests never leave the host."""
    return attributed_session(
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",  # noqa: S106
        aws_session_token="testing",  # noqa: S106
    )


def test_user_agent_extra_is_name_and_version() -> None:
    # arrange
    expected = f"AwsCats/{__version__}"

    # act / assert
    assert expected == USER_AGENT_EXTRA


def test_attributed_session_sends_suffix_exactly_once_on_the_wire() -> None:
    # arrange
    session = make_session()
    expected = [USER_AGENT_EXTRA]

    # act
    user_agent = capture_wire_user_agent(session)

    # assert
    assert expected == [token for token in user_agent.split(" ") if token.startswith("AwsCats/")]


def test_second_client_from_same_session_does_not_double_stamp() -> None:
    # arrange
    session = make_session()
    expected = [USER_AGENT_EXTRA]
    capture_wire_user_agent(session)  # first client on this session

    # act
    user_agent = capture_wire_user_agent(session)

    # assert
    assert expected == [token for token in user_agent.split(" ") if token.startswith("AwsCats/")]


def test_caller_client_config_extra_composes_with_suffix() -> None:
    # arrange
    session = make_session()
    caller_extra = "CustomerThing/9"
    expected = [USER_AGENT_EXTRA, caller_extra]

    # act
    user_agent = capture_wire_user_agent(session, client_config=Config(user_agent_extra=caller_extra))

    # assert
    assert expected == [token for token in user_agent.split(" ") if token.startswith("AwsCats/") or token == caller_extra]


@pytest.fixture
def acme_pack_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    """One pack registered in an otherwise empty registry, as the pytest plugin would leave it."""
    monkeypatch.setattr("cats.user_agent._registered_test_packs", {})
    register_test_pack(CatsTestPack(name="acme-controls", version="0.99.4"))


def cats_tokens(user_agent: str) -> list[str]:
    """Every token CATS is responsible for, in wire order."""
    return [token for token in user_agent.split(" ") if token.startswith(("AwsCats/", "AwsCatsPack/"))]


def test_registered_pack_token_follows_the_cats_token_on_the_wire(acme_pack_registered: None) -> None:
    # arrange
    session = make_session()
    expected = [USER_AGENT_EXTRA, "AwsCatsPack/acme-controls#0.99.4"]

    # act
    user_agent = capture_wire_user_agent(session)

    # assert
    assert expected == cats_tokens(user_agent)


def test_pack_token_is_sent_exactly_once_across_two_clients(acme_pack_registered: None) -> None:
    # arrange
    session = make_session()
    expected = [USER_AGENT_EXTRA, "AwsCatsPack/acme-controls#0.99.4"]
    capture_wire_user_agent(session)  # first client on this session

    # act
    user_agent = capture_wire_user_agent(session)

    # assert
    assert expected == cats_tokens(user_agent)


def test_caller_client_config_extra_composes_after_the_pack_token(acme_pack_registered: None) -> None:
    # arrange
    session = make_session()
    caller_extra = "CustomerThing/9"
    expected = [USER_AGENT_EXTRA, "AwsCatsPack/acme-controls#0.99.4", caller_extra]

    # act
    user_agent = capture_wire_user_agent(session, client_config=Config(user_agent_extra=caller_extra))

    # assert
    assert expected == [*cats_tokens(user_agent), *(token for token in user_agent.split(" ") if token == caller_extra)]


def test_cats_tokens_precede_the_trailing_botocore_token(acme_pack_registered: None) -> None:
    """Pins the wire position: our tokens are the last thing before botocore's own trailer.

    Everything before them varies with the SDK and the environment (OS, Python, app id,
    business metrics), so only the tail is asserted.
    """
    # arrange
    session = make_session()
    expected = f"{USER_AGENT_EXTRA} AwsCatsPack/acme-controls#0.99.4 Botocore/{botocore.__version__}"

    # act
    user_agent = capture_wire_user_agent(session)

    # assert
    assert expected == " ".join(user_agent.split(" ")[-3:])
