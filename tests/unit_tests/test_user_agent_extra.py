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

"""Tests for cats.user_agent.user_agent_extra: the CATS token followed by every registered pack, by name."""

import pytest

from cats.user_agent import USER_AGENT_EXTRA, CatsTestPack, register_test_pack, user_agent_extra


@pytest.fixture(autouse=True)
def empty_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts with no packs registered in this process."""
    monkeypatch.setattr("cats.user_agent._registered_test_packs", {})


def test_no_packs_gives_the_cats_token_alone() -> None:
    # arrange
    expected = USER_AGENT_EXTRA

    # act
    result = user_agent_extra()

    # assert
    assert expected == result


def test_one_pack_follows_the_cats_token() -> None:
    # arrange
    register_test_pack(CatsTestPack(name="acme-controls", version="0.99.4"))
    expected = f"{USER_AGENT_EXTRA} AwsCatsPack/acme-controls#0.99.4"

    # act
    result = user_agent_extra()

    # assert
    assert expected == result


def test_packs_are_ordered_by_name_not_registration_order() -> None:
    # arrange
    register_test_pack(CatsTestPack(name="zeta-pack", version="2"))
    register_test_pack(CatsTestPack(name="alpha-pack", version="1"))
    expected = f"{USER_AGENT_EXTRA} AwsCatsPack/alpha-pack#1 AwsCatsPack/zeta-pack#2"

    # act
    result = user_agent_extra()

    # assert
    assert expected == result
