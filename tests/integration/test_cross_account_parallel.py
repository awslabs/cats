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

"""Integration tests: cross-account parallel deploy."""

from collections.abc import Generator
from typing import Any

import pytest

from cats.resources.account_under_test import AccountUnderTest
from tests.integration.constants import CROSS_ACCOUNT_ID, REGION, skip_no_cross_account, skip_wrong_account
from tests.utils import deploy_and_destroy

NUM_PARALLEL_STACKS = 20


@pytest.fixture(scope="module")
def deployed_cross_account_parallel_stacks(
    tmp_path_factory: pytest.TempPathFactory,
    cross_account_parallel_config: dict[str, Any],
) -> Generator[str | list[str], None, None]:
    """Deploy parallel stacks to cross-account once for all tests in this module."""
    account_keynames = [f"parallel_{i}" for i in range(NUM_PARALLEL_STACKS)]
    yield from deploy_and_destroy(tmp_path_factory, cross_account_parallel_config, account_keynames, REGION, "xacct-parallel", deploy_all=True)


@skip_wrong_account
@skip_no_cross_account
def test_cross_account_parallel_role_assumed(deployed_cross_account_parallel_stacks: list[str]) -> None:
    """Cross-account parallel deploy assumes role successfully."""
    # act
    account = AccountUnderTest("parallel_0")
    assumed_identity = account.session.client("sts").get_caller_identity()

    # assert
    assert assumed_identity["Account"] == CROSS_ACCOUNT_ID


@skip_wrong_account
@skip_no_cross_account
def test_cross_account_parallel_multiple_accounts_work(deployed_cross_account_parallel_stacks: list[str]) -> None:
    """Multiple cross-account parallel stacks have working sessions."""
    # arrange - test a sample of accounts (first, middle, last)
    sample_indices = [0, NUM_PARALLEL_STACKS // 2, NUM_PARALLEL_STACKS - 1]

    # act / assert
    for i in sample_indices:
        account = AccountUnderTest(f"parallel_{i}")
        identity = account.session.client("sts").get_caller_identity()
        assert identity["Account"] == CROSS_ACCOUNT_ID
        assert f"parallel_{i}" in identity["Arn"]
