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

"""Integration tests: parallel deploy (multiple accounts)."""

from collections.abc import Generator
from pathlib import Path
from typing import Any

import boto3
import pytest

from cats.cfn.deploy import get_stack_name, get_stack_status
from cats.report_run import ResultCounts
from cats.resources.account_under_test import AccountUnderTest
from tests.integration.constants import ACCOUNT_ID, REGION, skip_wrong_account, stack_prefix
from tests.integration.reporting_helpers import junit_properties, run_nested_cats_session
from tests.utils import deploy_and_destroy

STACK_PREFIX = stack_prefix("par")


@pytest.fixture(scope="module")
def deployed_parallel_stacks(
    tmp_path_factory: pytest.TempPathFactory,
    parallel_config: dict[str, Any],
) -> Generator[str | list[str], None, None]:
    """Deploy parallel stacks once for all tests in this module."""
    yield from deploy_and_destroy(tmp_path_factory, parallel_config, ["parallel_a", "parallel_b"], REGION, "parallel", deploy_all=True)


@skip_wrong_account
def test_parallel_stacks_create_successfully(deployed_parallel_stacks: list[str]) -> None:
    """Parallel stacks deploy with CREATE_COMPLETE status."""
    # arrange
    session = boto3.Session(region_name=REGION)
    expected_status = "CREATE_COMPLETE"

    # act / assert
    for account_keyname in deployed_parallel_stacks:
        stack_name = get_stack_name(STACK_PREFIX, account_keyname)
        status = get_stack_status(session, stack_name, REGION)
        assert status == expected_status


@skip_wrong_account
def test_parallel_accounts_have_independent_sessions(deployed_parallel_stacks: list[str]) -> None:
    """Each parallel account has a working session with correct identity."""
    # act / assert
    for account_keyname in deployed_parallel_stacks:
        account = AccountUnderTest(account_keyname)
        identity = account.session.client("sts").get_caller_identity()
        assert identity["Account"] == ACCOUNT_ID
        assert account_keyname in identity["Arn"]


@skip_wrong_account
def test_reporting_under_xdist_merges_both_workers_into_one_run(
    deployed_parallel_stacks: list[str], parallel_config: dict[str, Any], tmp_path: Path
) -> None:
    """A nested `-n 2` session: the controller collects both workers' entries, both accounts appear, and one run id names every file."""
    # arrange
    conftest_source = """
from cats import StandardMetadata


def pytest_cats_standards():
    return {"SOC2-CC6.6": StandardMetadata(control_ids=["cc6.6"], control_group="Logical access")}
"""
    test_source = """
from cats import AccountUnderTest, cats


@cats(standard_name="SOC2-CC6.6")
def test_role_a(parallel_a_account: AccountUnderTest) -> None:
    assert parallel_a_account.session.client("sts").get_caller_identity()["Account"] == parallel_a_account.account_id


@cats(standard_name="SOC2-CC6.6")
def test_role_b(parallel_b_account: AccountUnderTest) -> None:
    assert parallel_b_account.session.client("sts").get_caller_identity()["Account"] == parallel_b_account.account_id
"""

    # act
    run = run_nested_cats_session(
        parallel_config, ["cats_json", "oscal_ar", "junit"], test_source, tmp_path, conftest_source=conftest_source, pytest_args=["-n", "2"]
    )
    assert run.returncode == 0, run.summary()
    report = run.report()
    run_id = report["run"]["run_id"]
    assessment_results = run.assessment_results()
    metadata_props = {prop["name"]: prop["value"] for prop in assessment_results["assessment-results"]["metadata"]["props"]}
    testsuite = run.junit_testsuite()

    # assert
    expected_counts: ResultCounts = {"total": 2, "passed": 2, "failed": 0, "error": 0, "skipped": 0, "xfailed": 0, "xpassed": 0, "not_run": 0}
    expected = {
        "counts": expected_counts,
        "accounts": [
            {"account_name": "parallel_a", "account_id": ACCOUNT_ID},
            {"account_name": "parallel_b", "account_id": ACCOUNT_ID},
        ],
        "nodeids": ["test_nested.py::test_role_a", "test_nested.py::test_role_b"],
        "writers": [
            {"format": "cats_json", "file": f"report-{run_id}.json", "status": "ok", "detail": ""},
            {"format": "oscal_ar", "file": f"assessment-results-{run_id}.json", "status": "ok", "detail": ""},
            {"format": "junit", "file": f"junit-{run_id}.xml", "status": "ok", "detail": ""},
        ],
        "oscal_run_id": run_id,
        "oscal_findings": [("cc6.6", "satisfied"), ("cc6.6", "satisfied")],
        "junit_run_id": run_id,
        "junit_accounts": [f"parallel_a ({ACCOUNT_ID})", f"parallel_b ({ACCOUNT_ID})"],
    }
    testcases = sorted(testsuite.findall("./testcase"), key=lambda testcase: str(testcase.get("name")))
    assert expected == {
        "counts": report["run"]["counts"],
        "accounts": report["run"]["accounts"],
        "nodeids": [entry["nodeid"] for entry in report["tests"]],
        "writers": report["run"]["writers"],
        "oscal_run_id": metadata_props["cats-run-id"],
        "oscal_findings": sorted(
            (finding["target"]["target-id"], finding["target"]["status"]["state"])
            for finding in assessment_results["assessment-results"]["results"][0]["findings"]
        ),
        "junit_run_id": junit_properties(testsuite)["cats:run_id"],
        "junit_accounts": [junit_properties(testcase)["cats:account"] for testcase in testcases],
    }
