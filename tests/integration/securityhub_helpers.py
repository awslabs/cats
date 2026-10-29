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

"""Helpers shared by the Security Hub integration modules: synthetic evidence, reading findings back, cleanup."""

import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from boto3.session import Session

from cats.config import SecurityHubSeverity
from cats.report_run import RunManifest
from cats.report_writers.securityhub import FindingContext, asff_timestamp, build_account_findings
from cats.reporting import ReportEntry
from cats.version import __version__
from tests.utils import make_run_manifest

if TYPE_CHECKING:
    from mypy_boto3_securityhub.client import SecurityHubClient
    from mypy_boto3_securityhub.type_defs import AwsSecurityFindingOutputTypeDef

# GetFindings is eventually consistent after BatchImportFindings, and reads can be served by replicas
# at different lag, with consecutive reads flipping between an old and a new version. Waits key on
# the run id, so a stale read never passes. The lag is the same whether the member's own hub or its
# delegated administrator is read: over 36 CI runs (2026-09-25 to 2026-10-26) both waits had a median
# near 110 s, a 90th percentile near 190 s and a maximum of 249 s, and one administrator read exceeded
# the earlier 300 s bound. The bound costs nothing on a normal run: the wait returns on the first read
# that shows the finding, so only a slow service pays it.
FINDING_VISIBLE_SECONDS = 600
POLL_SECONDS = 3


def synthetic_entry(result: str, nodeid: str, error_message: str | None, account_name: str, account_id: str) -> ReportEntry:
    """One evidence entry attributed to a deployed account, as the plugin would record it."""
    return {
        "result": result,  # type: ignore[typeddict-item]
        "accounts_in_test": [{"account_name": account_name, "account_id": account_id, "tags": {}}],
        "test_name": nodeid.rsplit("::", 1)[1],
        "nodeid": nodeid,
        "test_docstring": "Synthetic evidence imported by the CATS integration suite.",
        "standards": ["SOC2-CC6.6"],
        "standard_metadata": {},
        "custom_markers": {},
        "additional_metadata": {},
        "xfail": False,
        "error_message": error_message,
        "reason": None,
        "duration_s": 0.1,
        "recorded_at": datetime.now(tz=UTC).isoformat(),
    }


def manifest_finishing_now(run_id: str) -> RunManifest:
    """A run manifest finishing now, so UpdatedAt is the real wall clock as it is in production.

    The BatchImportFindings reference documents no ordering rule for UpdatedAt; a monotonic
    timestamp is used here because that is what a real run sends, not because one is known
    to be required.
    """
    manifest = make_run_manifest(run_id=run_id)
    manifest["finished_at"] = datetime.now(tz=UTC).isoformat()
    return manifest


def wait_for_finding(client: "SecurityHubClient", finding_id: str, run_id: str) -> "AwsSecurityFindingOutputTypeDef":
    """The finding once the import from ``run_id`` is visible to ``client``, or fail after the deadline."""
    deadline = time.monotonic() + FINDING_VISIBLE_SECONDS
    while True:
        findings = client.get_findings(Filters={"Id": [{"Value": finding_id, "Comparison": "EQUALS"}]})["Findings"]
        if len(findings) == 1 and findings[0].get("ProductFields", {}).get("cats/run_id") == run_id:
            return findings[0]
        if time.monotonic() > deadline:
            raise AssertionError(f"finding {finding_id} from run {run_id} not visible after {FINDING_VISIBLE_SECONDS}s; found: {findings}")
        time.sleep(POLL_SECONDS)


def assume_role(session: Session, role_arn: str, region: str) -> Session:
    """A session in the role, chained from ``session``. Credentials stay inside the process."""
    credentials = session.client("sts").assume_role(RoleArn=role_arn, RoleSessionName="cats-integ-securityhub")["Credentials"]
    return Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
        region_name=region,
    )


def archive_synthetic_findings(
    client: "SecurityHubClient", region: str, cfn_stack_prefix: str, account_name: str, account_id: str, nodeids: Sequence[str]
) -> None:
    """Best-effort hygiene: re-import every synthetic finding as a pass, which archives it.

    Findings cannot be deleted through the API; archived ones drop out of the default views and
    Security Hub deletes findings not updated for 90 days. Never raises: cleanup must not prevent
    the stack destroy that follows.
    """
    context = FindingContext(
        partition="aws",
        region=region,
        aws_region=region,
        cfn_stack_prefix=cfn_stack_prefix,
        failed_severity=SecurityHubSeverity.HIGH,
        run_id="teardown",
        cats_version=__version__,
        updated_at=asff_timestamp(datetime.now(tz=UTC)),
    )
    entries = [synthetic_entry("passed", nodeid, None, account_name, account_id) for nodeid in nodeids]
    accounts, _ = build_account_findings(entries, context, None)
    try:
        client.batch_import_findings(Findings=accounts[0].findings)
    except Exception as e:
        print(f"[fixture] could not archive synthetic Security Hub findings: {e!r}")
