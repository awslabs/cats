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

"""Integration tests: the securityhub report format against real Security Hub, same account.

The unit tests prove the finding model and every failure path with moto and mocked clients.
This module proves what they cannot: the grant lands on a real test role and is scoped to
this account's own product; that role can import into the account's Security Hub; the
finding is readable there with the fields CATS set; and a later run updates it in place
rather than adding a second one. test_cross_account_securityhub.py covers the member account
and delegated-administrator path.

Security Hub is enabled in the account by the base stack (infra/integ-test-vpc.yaml) with no
standards subscribed. Findings cannot be deleted through the API: the module archives the
ones it imported on teardown, and Security Hub drops findings not updated for 90 days.
"""

import json
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
import pytest

from cats.report_run import ResultCounts
from cats.report_writers.securityhub import SecurityHubWriter, finding_id
from cats.reporting import ReportEntry
from tests.integration.constants import ACCOUNT_ID, REGION, skip_wrong_account, stack_prefix
from tests.integration.reporting_helpers import NESTED_TEST_FILE, junit_properties, run_nested_cats_session
from tests.integration.securityhub_helpers import archive_synthetic_findings, manifest_finishing_now, synthetic_entry, wait_for_finding
from tests.utils import deploy_and_destroy

if TYPE_CHECKING:
    from mypy_boto3_securityhub.client import SecurityHubClient

STACK_PREFIX = stack_prefix("sh")
ACCOUNT_KEYNAME = "sechub"
TEST_ROLE_NAME = f"{STACK_PREFIX}{ACCOUNT_KEYNAME}-default-test-role"
# Synthetic tests whose "results" are imported. Distinct per test so their order does not matter.
NODEID_LANDS = "tests/integration/test_securityhub.py::synthetic_control_holds"
NODEID_UPDATES = "tests/integration/test_securityhub.py::synthetic_control_fixed_later"
# The one real test the nested reporting session runs; its nodeid is relative to that session's rootdir.
NODEID_NESTED = f"{NESTED_TEST_FILE}::test_control_that_fails"


def _securityhub() -> "SecurityHubClient":
    """The deploy identity's client, used to read findings back and to archive them."""
    client: SecurityHubClient = boto3.Session(region_name=REGION).client("securityhub")
    return client


@pytest.fixture(scope="module")
def deployed_securityhub_stack(
    tmp_path_factory: pytest.TempPathFactory,
    securityhub_config: dict[str, Any],
) -> Generator[str | list[str], None, None]:
    """Deploy the stack once for the module; archive the imported findings before it is destroyed."""
    for keyname in deploy_and_destroy(tmp_path_factory, securityhub_config, ACCOUNT_KEYNAME, REGION, "sechub"):
        try:
            yield keyname
        finally:
            archive_synthetic_findings(
                _securityhub(), REGION, STACK_PREFIX, ACCOUNT_KEYNAME, ACCOUNT_ID, (NODEID_LANDS, NODEID_UPDATES, NODEID_NESTED)
            )


def _entry(result: str, nodeid: str, error_message: str | None) -> ReportEntry:
    return synthetic_entry(result, nodeid, error_message, ACCOUNT_KEYNAME, ACCOUNT_ID)


@skip_wrong_account
def test_test_role_has_the_import_grant_on_its_own_product_only(deployed_securityhub_stack: str) -> None:
    """The deployed grant is one action on this account's default product ARN in the configured Region."""
    # arrange
    iam = boto3.Session(region_name=REGION).client("iam")

    # act
    document = iam.get_role_policy(RoleName=TEST_ROLE_NAME, PolicyName="CatsTestRolePolicy")["PolicyDocument"]
    assert isinstance(document, dict)  # the stub allows str | dict; IAM returns the decoded document
    statement = next(statement for statement in document["Statement"] if statement["Sid"] == "SecurityHubImportFindings")

    # assert
    expected = {
        "Sid": "SecurityHubImportFindings",
        "Effect": "Allow",
        "Action": "securityhub:BatchImportFindings",
        "Resource": f"arn:aws:securityhub:{REGION}:{ACCOUNT_ID}:product/{ACCOUNT_ID}/default",
    }
    assert expected == statement


@skip_wrong_account
def test_writer_imports_through_the_test_role_and_the_finding_lands(deployed_securityhub_stack: str, tmp_path: Path) -> None:
    """The whole chain: config → findings → assume the deployed test role → BatchImportFindings → file → summary."""
    # arrange
    run_id = "20260925T090000Z"
    output_path = tmp_path / "securityhub-findings.json"
    entries = [_entry("failed", NODEID_LANDS, "assert REFUSED == TIMEOUT")]

    # act
    notes = SecurityHubWriter().write(entries, output_path, manifest_finishing_now(run_id), None)
    document = json.loads(output_path.read_text())
    landed = wait_for_finding(_securityhub(), finding_id(STACK_PREFIX, ACCOUNT_KEYNAME, REGION, NODEID_LANDS), run_id)

    # assert
    expected = {
        "notes": [f"Security Hub ({REGION}): {ACCOUNT_KEYNAME} 1 findings imported"],
        "file_delivery": {"status": "imported", "findings_imported": 1, "detail": None},
        "file_role_arn": f"arn:aws:iam::{ACCOUNT_ID}:role/{TEST_ROLE_NAME}",
        "landed": {
            "AwsAccountId": ACCOUNT_ID,
            "ProductName": "CATS",
            "Title": "synthetic_control_holds",
            "Compliance.Status": "FAILED",
            "Compliance.RelatedRequirements": ["SOC2-CC6.6"],
            "RecordState": "ACTIVE",
            "Severity.Label": "HIGH",
            "cats/result": "failed",
            "cats/error_message": "assert REFUSED == TIMEOUT",
            "cats/account_name": ACCOUNT_KEYNAME,
        },
    }
    delivery = document["accounts"][0]["delivery"]
    assert expected == {
        "notes": notes,
        "file_delivery": {key: delivery[key] for key in ("status", "findings_imported", "detail")},
        "file_role_arn": document["accounts"][0]["role_arn"],
        "landed": {
            "AwsAccountId": landed["AwsAccountId"],
            "ProductName": landed["ProductName"],
            "Title": landed["Title"],
            "Compliance.Status": landed["Compliance"]["Status"],
            "Compliance.RelatedRequirements": landed["Compliance"]["RelatedRequirements"],
            "RecordState": landed["RecordState"],
            "Severity.Label": landed["Severity"]["Label"],
            "cats/result": landed["ProductFields"]["cats/result"],
            "cats/error_message": landed["ProductFields"]["cats/error_message"],
            "cats/account_name": landed["ProductFields"]["cats/account_name"],
        },
    }


@skip_wrong_account
def test_a_later_passing_run_archives_the_same_finding(deployed_securityhub_stack: str, tmp_path: Path) -> None:
    """Same node id, later run: Security Hub holds one finding, now PASSED and ARCHIVED, not a second one."""
    # arrange
    writer = SecurityHubWriter()
    wanted = finding_id(STACK_PREFIX, ACCOUNT_KEYNAME, REGION, NODEID_UPDATES)
    writer.write([_entry("failed", NODEID_UPDATES, "open")], tmp_path / "first.json", manifest_finishing_now("20260925T091000Z"), None)
    wait_for_finding(_securityhub(), wanted, "20260925T091000Z")

    # act
    writer.write([_entry("passed", NODEID_UPDATES, None)], tmp_path / "second.json", manifest_finishing_now("20260925T092000Z"), None)
    updated = wait_for_finding(_securityhub(), wanted, "20260925T092000Z")
    matching = _securityhub().get_findings(Filters={"Id": [{"Value": wanted, "Comparison": "EQUALS"}]})["Findings"]

    # assert
    expected = (1, "PASSED", "ARCHIVED", "INFORMATIONAL", "passed")
    assert expected == (
        len(matching),
        updated["Compliance"]["Status"],
        updated["RecordState"],
        updated["Severity"]["Label"],
        updated["ProductFields"]["cats/result"],
    )


@skip_wrong_account
def test_reporting_all_four_formats_from_a_real_failing_test(
    deployed_securityhub_stack: str, securityhub_config: dict[str, Any], tmp_path: Path
) -> None:
    """One nested session writes all four formats from a real failing test; the finding lands under the Id the file records."""
    # arrange
    test_source = '''
import pytest

from cats import AccountUnderTest, cats


@cats(standard_name="SOC2-CC6.6", control_ids=["CC6.6"])
def test_control_that_fails(sechub_account: AccountUnderTest) -> None:
    """A control that does not hold, so Security Hub gets a FAILED finding."""
    identity = sechub_account.session.client("sts").get_caller_identity()
    pytest.fail(f"the control does not hold in {identity['Account']}")
'''

    # act
    run = run_nested_cats_session(
        securityhub_config, ["cats_json", "oscal_ar", "junit", "securityhub"], test_source, tmp_path, securityhub_region=REGION
    )
    assert run.returncode == 1, run.summary()  # the test failed; every writer still ran
    report = run.report()
    run_id = report["run"]["run_id"]
    document = run.securityhub_document()
    wanted = finding_id(STACK_PREFIX, ACCOUNT_KEYNAME, REGION, NODEID_NESTED)
    landed = wait_for_finding(_securityhub(), wanted, run_id)
    ar_findings = run.assessment_results()["assessment-results"]["results"][0]["findings"]

    # assert
    expected_counts: ResultCounts = {"total": 1, "passed": 0, "failed": 1, "error": 0, "skipped": 0, "xfailed": 0, "xpassed": 0, "not_run": 0}
    expected = {
        "counts": expected_counts,
        "writers": [
            {"format": "cats_json", "file": f"report-{run_id}.json", "status": "ok", "detail": ""},
            {"format": "oscal_ar", "file": f"assessment-results-{run_id}.json", "status": "ok", "detail": ""},
            {"format": "junit", "file": f"junit-{run_id}.xml", "status": "ok", "detail": ""},
            {
                "format": "securityhub",
                "file": f"securityhub-findings-{run_id}.json",
                "status": "ok",
                "detail": f"Security Hub ({REGION}): {ACCOUNT_KEYNAME} 1 findings imported",
            },
        ],
        "file_account": {
            "role_arn": f"arn:aws:iam::{ACCOUNT_ID}:role/{TEST_ROLE_NAME}",
            "delivery_status": "imported",
            "findings_imported": 1,
            "finding_ids": [wanted],
        },
        "landed": {
            "Compliance.Status": "FAILED",
            "Compliance.RelatedRequirements": ["SOC2-CC6.6", "CC6.6"],
            "RecordState": "ACTIVE",
            "cats/run_id": run_id,
            "cats/nodeid": NODEID_NESTED,
            "cats/result": "failed",
        },
        "oscal_findings": [("CC6.6", "not-satisfied")],
        "junit_run_id": run_id,
    }
    account = document["accounts"][0]
    assert expected == {
        "counts": report["run"]["counts"],
        "writers": report["run"]["writers"],
        "file_account": {
            "role_arn": account["role_arn"],
            "delivery_status": account["delivery"]["status"],
            "findings_imported": account["delivery"]["findings_imported"],
            "finding_ids": [finding["Id"] for finding in account["findings"]],
        },
        "landed": {
            "Compliance.Status": landed["Compliance"]["Status"],
            "Compliance.RelatedRequirements": landed["Compliance"]["RelatedRequirements"],
            "RecordState": landed["RecordState"],
            "cats/run_id": landed["ProductFields"]["cats/run_id"],
            "cats/nodeid": landed["ProductFields"]["cats/nodeid"],
            "cats/result": landed["ProductFields"]["cats/result"],
        },
        "oscal_findings": [(finding["target"]["target-id"], finding["target"]["status"]["state"]) for finding in ar_findings],
        "junit_run_id": junit_properties(run.junit_testsuite())["cats:run_id"],
    }
