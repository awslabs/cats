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

"""Tests for cats.user_agent.register_test_pack: redeclaration rules for the per-process pack registry."""

import logging

import pytest

from cats.user_agent import CatsTestPack, register_test_pack, registered_test_packs


@pytest.fixture(autouse=True)
def empty_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts with no packs registered in this process."""
    monkeypatch.setattr("cats.user_agent._registered_test_packs", {})


def test_identical_redeclaration_is_a_no_op(caplog: pytest.LogCaptureFixture) -> None:
    # arrange
    register_test_pack(CatsTestPack(name="acme-controls", version="0.99.4"))
    expected = (CatsTestPack(name="acme-controls", version="0.99.4"),)

    # act
    with caplog.at_level(logging.WARNING, logger="cats.user_agent"):
        register_test_pack(CatsTestPack(name="acme-controls", version="0.99.4"))

    # assert
    assert expected == registered_test_packs()
    assert caplog.records == []


def test_conflicting_redeclaration_keeps_the_first_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    # arrange
    register_test_pack(CatsTestPack(name="acme-controls", version="0.99.4"))
    expected_packs = (CatsTestPack(name="acme-controls", version="0.99.4"),)
    expected_warning = (
        "Test pack 'acme-controls' declared twice with different values; "
        + "keeping AwsCatsPack/acme-controls#0.99.4, ignoring AwsCatsPack/acme-controls#1.0.0"
    )

    # act
    with caplog.at_level(logging.WARNING, logger="cats.user_agent"):
        register_test_pack(CatsTestPack(name="acme-controls", version="1.0.0"))

    # assert
    assert expected_packs == registered_test_packs()
    assert [expected_warning] == [record.getMessage() for record in caplog.records]
