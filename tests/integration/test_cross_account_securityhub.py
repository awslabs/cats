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

"""Integration tests: the securityhub report format into a member account of another organization.

This is the deployment the format exists for. CATS runs from one account and imports findings
into an account under test through that account's own test role, so the finding lands in the
member's Security Hub and surfaces in the organization's delegated administrator with no
admin-side write. Here the member is CROSS_ACCOUNT_ID, its hub lives in CROSS_SECURITYHUB_REGION
(not REGION, where the stack deploys) and its administrator is SECURITYHUB_ADMIN_ACCOUNT_ID.

The administrator is read through SECURITYHUB_ADMIN_ROLE, a read-only role the member may assume
there (infra/securityhub-admin-read-role.yaml), chained from the deploy session. Synthetic findings
are archived on teardown.
"""

import json
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from boto3.session import Session

from cats.cfn.session import get_deploy_session
from cats.report_writers.securityhub import SecurityHubWriter, finding_id
from cats.reporting import ReportEntry
from tests.integration.constants import (
    CROSS_ACCOUNT_ID,
    CROSS_SECURITYHUB_REGION,
    REGION,
    SECURITYHUB_ADMIN_ACCOUNT_ID,
    SECURITYHUB_ADMIN_ROLE,
    skip_no_securityhub_admin,
    skip_wrong_account,
    stack_prefix,
)
from tests.integration.securityhub_helpers import (
    archive_synthetic_findings,
    assume_role,
    manifest_finishing_now,
    synthetic_entry,
    wait_for_finding,
)
from tests.utils import deploy_and_destroy

if TYPE_CHECKING:
    from mypy_boto3_securityhub.client import SecurityHubClient

STACK_PREFIX = stack_prefix("xash")
ACCOUNT_KEYNAME = "xacct_sechub"
TEST_ROLE_NAME = f"{STACK_PREFIX}{ACCOUNT_KEYNAME}-default-test-role"
NODEID_MEMBER = "tests/integration/test_cross_account_securityhub.py::synthetic_member_control_holds"
NODEID_ADMIN = "tests/integration/test_cross_account_securityhub.py::synthetic_control_seen_by_administrator"


def _member_session() -> Session:
    """The deploy role in the member account: Admin there, as the other cross-account modules use it."""
    return get_deploy_session(ACCOUNT_KEYNAME)


def _member_securityhub() -> "SecurityHubClient":
    client: SecurityHubClient = _member_session().client("securityhub", region_name=CROSS_SECURITYHUB_REGION)
    return client


def _administrator_securityhub() -> "SecurityHubClient":
    """The delegated administrator's hub, reached by chaining from the member into its read-only CATS role."""
    admin = assume_role(_member_session(), f"arn:aws:iam::{SECURITYHUB_ADMIN_ACCOUNT_ID}:role/{SECURITYHUB_ADMIN_ROLE}", CROSS_SECURITYHUB_REGION)
    client: SecurityHubClient = admin.client("securityhub")
    return client


@pytest.fixture(scope="module")
def deployed_cross_account_securityhub_stack(
    tmp_path_factory: pytest.TempPathFactory,
    cross_account_securityhub_config: dict[str, Any],
) -> Generator[str | list[str], None, None]:
    """Deploy the member stack once for the module; archive the imported findings before it is destroyed."""
    for keyname in deploy_and_destroy(tmp_path_factory, cross_account_securityhub_config, ACCOUNT_KEYNAME, REGION, "xacct-sechub"):
        try:
            yield keyname
        finally:
            archive_synthetic_findings(
                _member_securityhub(), CROSS_SECURITYHUB_REGION, STACK_PREFIX, ACCOUNT_KEYNAME, CROSS_ACCOUNT_ID, (NODEID_MEMBER, NODEID_ADMIN)
            )


def _entry(result: str, nodeid: str, error_message: str | None) -> ReportEntry:
    return synthetic_entry(result, nodeid, error_message, ACCOUNT_KEYNAME, CROSS_ACCOUNT_ID)


@skip_wrong_account
@skip_no_securityhub_admin
def test_member_test_role_grant_names_the_securityhub_region_not_the_deploy_region(deployed_cross_account_securityhub_stack: str) -> None:
    """reporting.securityhub.aws_region drives the grant: the product ARN is in the hub's Region, the stack is in REGION."""
    # arrange
    iam = _member_session().client("iam")

    # act
    document = iam.get_role_policy(RoleName=TEST_ROLE_NAME, PolicyName="CatsTestRolePolicy")["PolicyDocument"]
    assert isinstance(document, dict)  # the stub allows str | dict; IAM returns the decoded document
    statement = next(statement for statement in document["Statement"] if statement["Sid"] == "SecurityHubImportFindings")

    # assert
    expected = {
        "Sid": "SecurityHubImportFindings",
        "Effect": "Allow",
        "Action": "securityhub:BatchImportFindings",
        "Resource": f"arn:aws:securityhub:{CROSS_SECURITYHUB_REGION}:{CROSS_ACCOUNT_ID}:product/{CROSS_ACCOUNT_ID}/default",
    }
    assert expected == statement


@skip_wrong_account
@skip_no_securityhub_admin
def test_writer_imports_into_the_member_accounts_hub_in_its_own_region(deployed_cross_account_securityhub_stack: str, tmp_path: Path) -> None:
    """From the parent account, through the member's test role, into the member's hub in a Region the stack is not in."""
    # arrange
    run_id = "20260925T100000Z"
    output_path = tmp_path / "securityhub-findings.json"

    # act
    notes = SecurityHubWriter().write(
        [_entry("failed", NODEID_MEMBER, "assert REFUSED == TIMEOUT")], output_path, manifest_finishing_now(run_id), None
    )
    document = json.loads(output_path.read_text())
    landed = wait_for_finding(_member_securityhub(), finding_id(STACK_PREFIX, ACCOUNT_KEYNAME, REGION, NODEID_MEMBER), run_id)

    # assert
    expected = {
        "notes": [f"Security Hub ({CROSS_SECURITYHUB_REGION}): {ACCOUNT_KEYNAME} 1 findings imported"],
        "file": {
            "region": CROSS_SECURITYHUB_REGION,
            "role_arn": f"arn:aws:iam::{CROSS_ACCOUNT_ID}:role/{TEST_ROLE_NAME}",
            "product_arn": f"arn:aws:securityhub:{CROSS_SECURITYHUB_REGION}:{CROSS_ACCOUNT_ID}:product/{CROSS_ACCOUNT_ID}/default",
            "status": "imported",
        },
        "landed": {
            "AwsAccountId": CROSS_ACCOUNT_ID,
            "Region": CROSS_SECURITYHUB_REGION,
            "Compliance.Status": "FAILED",
            "RecordState": "ACTIVE",
            "Severity.Label": "HIGH",
            "cats/account_name": ACCOUNT_KEYNAME,
        },
    }
    account = document["accounts"][0]
    assert expected == {
        "notes": notes,
        "file": {
            "region": document["region"],
            "role_arn": account["role_arn"],
            "product_arn": account["product_arn"],
            "status": account["delivery"]["status"],
        },
        "landed": {
            "AwsAccountId": landed["AwsAccountId"],
            "Region": landed["Region"],
            "Compliance.Status": landed["Compliance"]["Status"],
            "RecordState": landed["RecordState"],
            "Severity.Label": landed["Severity"]["Label"],
            "cats/account_name": landed["ProductFields"]["cats/account_name"],
        },
    }


@skip_wrong_account
@skip_no_securityhub_admin
def test_finding_imported_into_the_member_surfaces_in_the_delegated_administrator(
    deployed_cross_account_securityhub_stack: str, tmp_path: Path
) -> None:
    """The bubble-up: no write to the administrator, yet the member's finding is there, attributed to the member."""
    # arrange
    run_id = "20260925T101000Z"
    wanted = finding_id(STACK_PREFIX, ACCOUNT_KEYNAME, REGION, NODEID_ADMIN)
    SecurityHubWriter().write([_entry("failed", NODEID_ADMIN, "open")], tmp_path / "securityhub-findings.json", manifest_finishing_now(run_id), None)

    # act
    seen_by_admin = wait_for_finding(_administrator_securityhub(), wanted, run_id)

    # assert
    expected = (wanted, CROSS_ACCOUNT_ID, "FAILED", "ACTIVE", ACCOUNT_KEYNAME)
    assert expected == (
        seen_by_admin["Id"],
        seen_by_admin["AwsAccountId"],
        seen_by_admin["Compliance"]["Status"],
        seen_by_admin["RecordState"],
        seen_by_admin["ProductFields"]["cats/account_name"],
    )
