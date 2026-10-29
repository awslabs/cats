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

"""Tests for SecurityHubWriter.write: the findings file around the import, and what the user is told.

import_findings is replaced by a fake that records what it was handed and answers with the
deliveries a test scripts; its own behavior is covered in test_securityhub_import.py.
"""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from pytest_mock import MockerFixture
from syrupy.assertion import SnapshotAssertion

from cats.exceptions import CatsError
from cats.report_writers.securityhub import AccountFindings, FindingContext, SecurityHubDelivery, SecurityHubWriter
from cats.reporting import AccountReportDetail, ReportEntry
from tests.unit_tests.test_securityhub_build_findings import make_entry
from tests.utils import make_config, make_run_manifest, write_config

PROD = AccountReportDetail(account_name="prod", account_id="111111111111", tags={"env": "prod"})
DEV = AccountReportDetail(account_name="dev", account_id="222222222222", tags={})
MANIFEST = make_run_manifest()
DELIVERED_AT = "2026-09-24T10:53:40.120Z"
REDEPLOY = "Test role lacks securityhub:BatchImportFindings: the stack was deployed before securityhub was enabled. Redeploy: cats infra deploy prod"


def delivery(status: str, findings_imported: int = 0, detail: str | None = None) -> SecurityHubDelivery:
    return {"status": status, "findings_imported": findings_imported, "detail": detail, "delivered_at": DELIVERED_AT}  # type: ignore[typeddict-item]


@pytest.fixture
def securityhub_config(cats_yaml: Path) -> None:
    """A cats.yaml with the securityhub format on: what Settings() reads inside write()."""
    config = make_config(accounts={"prod": "111111111111", "dev": "222222222222"}, aws_region="us-east-1")
    config["reporting"] = {"report_formats": ["cats_json", "securityhub"], "securityhub": {"aws_region": "eu-west-1", "failed_severity": "CRITICAL"}}
    write_config(cats_yaml, config)


class FakeImporter:
    """Records the accounts and context handed to import_findings and, optionally, the file at that moment."""

    def __init__(self, deliveries: Sequence[SecurityHubDelivery], watch: Path | None = None) -> None:
        self.deliveries = list(deliveries)
        self.watch = watch
        self.accounts: list[AccountFindings] = []
        self.context: FindingContext | None = None
        self.file_at_import: dict[str, Any] | None = None
        self.file_existed_at_import: bool | None = None

    def __call__(self, accounts: Sequence[AccountFindings], context: FindingContext) -> list[SecurityHubDelivery]:
        self.accounts, self.context = list(accounts), context
        if self.watch is not None:
            self.file_existed_at_import = self.watch.exists()
            self.file_at_import = json.loads(self.watch.read_text()) if self.file_existed_at_import else None
        return self.deliveries


def install(mocker: MockerFixture, importer: FakeImporter) -> None:
    mocker.patch("cats.report_writers.securityhub.import_findings", side_effect=importer)


def entries() -> list[ReportEntry]:
    return [
        make_entry("failed", accounts=[PROD, DEV], error_message="open", nodeid="tests/test_net.py::test_prod_cannot_reach_dev"),
        make_entry("passed", accounts=[PROD], nodeid="tests/test_net.py::test_prod_egress_blocked"),
        make_entry("passed", accounts=[], nodeid="tests/test_local.py::test_no_account"),
    ]


def test_snapshot_findings_file_after_delivery(securityhub_config: None, tmp_path: Path, snapshot: SnapshotAssertion, mocker: MockerFixture) -> None:
    """The whole document: run, region, unattributed count, one block per account with role, product, delivery and findings."""
    # arrange
    output_path = tmp_path / "securityhub-findings.json"
    install(mocker, FakeImporter([delivery("imported", 2), delivery("not_enabled", 0, "InvalidAccessException: not subscribed")]))

    # act
    with pytest.warns(UserWarning, match="Security Hub is not enabled"):  # dev is not_enabled, which warns by design
        SecurityHubWriter().write(entries(), output_path, MANIFEST, None)

    # assert
    assert output_path.read_text() == snapshot


def test_file_is_on_disk_with_no_delivery_before_the_first_api_call(securityhub_config: None, tmp_path: Path, mocker: MockerFixture) -> None:
    """The safety net: what will be sent is written before anything is sent."""
    # arrange
    output_path = tmp_path / "securityhub-findings.json"
    importer = FakeImporter([delivery("imported", 2), delivery("imported", 1)], watch=output_path)
    install(mocker, importer)

    # act
    SecurityHubWriter().write(entries(), output_path, MANIFEST, None)

    # assert
    assert importer.file_at_import is not None
    expected = (True, [None, None], ["prod", "dev"], [2, 1])
    assert expected == (
        importer.file_existed_at_import,
        [account["delivery"] for account in importer.file_at_import["accounts"]],
        [account["account_name"] for account in importer.file_at_import["accounts"]],
        [len(account["findings"]) for account in importer.file_at_import["accounts"]],
    )


def test_file_is_rewritten_with_each_accounts_delivery(securityhub_config: None, tmp_path: Path, mocker: MockerFixture) -> None:
    # arrange
    output_path = tmp_path / "securityhub-findings.json"
    install(mocker, FakeImporter([delivery("imported", 2), delivery("imported", 1)]))

    # act
    SecurityHubWriter().write(entries(), output_path, MANIFEST, None)
    document = json.loads(output_path.read_text())

    # assert
    expected = [
        {"status": "imported", "findings_imported": 2, "detail": None, "delivered_at": DELIVERED_AT},
        {"status": "imported", "findings_imported": 1, "detail": None, "delivered_at": DELIVERED_AT},
    ]
    assert expected == [account["delivery"] for account in document["accounts"]]


def test_context_comes_from_settings_and_the_manifest(securityhub_config: None, tmp_path: Path, mocker: MockerFixture) -> None:
    """Region and severity from reporting.securityhub, prefix and partition from deployment settings, run id and version from the manifest."""
    # arrange
    importer = FakeImporter([delivery("imported", 2), delivery("imported", 1)])
    install(mocker, importer)

    # act
    SecurityHubWriter().write(entries(), tmp_path / "securityhub-findings.json", MANIFEST, None)

    # assert
    expected = FindingContext(
        partition="aws",
        region="eu-west-1",
        aws_region="us-east-1",
        cfn_stack_prefix="CATS-test-infra-",
        failed_severity="CRITICAL",  # type: ignore[arg-type]
        run_id="20260924T105317Z",
        cats_version="0.99.3",
        updated_at="2026-09-24T10:53:19.000Z",
    )
    assert expected == importer.context


def test_no_output_path_still_imports_and_writes_nothing(securityhub_config: None, tmp_path: Path, mocker: MockerFixture) -> None:
    """Report files off: the delivery is the product, so it happens; the file is not."""
    # arrange
    importer = FakeImporter([delivery("imported", 2), delivery("imported", 1)])
    install(mocker, importer)

    # act
    notes = SecurityHubWriter().write(entries(), None, MANIFEST, None)

    # assert
    expected: tuple[list[str], list[Path], list[str]] = (
        ["prod", "dev"],
        [],
        ["Security Hub (eu-west-1): prod 2 findings imported; dev 1 findings imported; 1 tests unattributed (no account fixture)"],
    )
    assert expected == (
        [account.account_name for account in importer.accounts],
        [path for path in tmp_path.iterdir() if path.suffix == ".json"],
        notes,
    )


def test_summary_line_names_every_account_outcome_and_the_unattributed_count(securityhub_config: None, tmp_path: Path, mocker: MockerFixture) -> None:
    # arrange
    install(mocker, FakeImporter([delivery("imported", 2), delivery("not_enabled", 0, "InvalidAccessException: not subscribed")]))

    # act
    with pytest.warns(UserWarning, match="Security Hub is not enabled"):
        notes = SecurityHubWriter().write(entries(), tmp_path / "securityhub-findings.json", MANIFEST, None)

    # assert
    expected = ["Security Hub (eu-west-1): prod 2 findings imported; dev not enabled, skipped; 1 tests unattributed (no account fixture)"]
    assert expected == notes


def test_not_run_entries_are_named_in_the_summary_and_the_file_and_not_imported(
    securityhub_config: None, tmp_path: Path, mocker: MockerFixture
) -> None:
    """A held-back test produced no control state: it is counted where the unattributed are, and no finding carries it."""
    # arrange
    output_path = tmp_path / "securityhub-findings.json"
    held_back = make_entry("not_run", accounts=[PROD], reason="RDS not in use; accepted by CISO 2026-09-01", nodeid="tests/test_rds.py::test_rds")
    install(mocker, FakeImporter([delivery("imported", 1)]))

    # act
    notes = SecurityHubWriter().write([held_back, entries()[1]], output_path, MANIFEST, None)
    document = json.loads(output_path.read_text())

    # assert
    expected = (
        ["Security Hub (eu-west-1): prod 1 findings imported; 1 tests not run (test_selection; not imported)"],
        1,
        ["tests/test_net.py::test_prod_egress_blocked"],
    )
    assert expected == (notes, document["not_run"], [finding["GeneratorId"] for finding in document["accounts"][0]["findings"]])


def test_not_enabled_account_warns_naming_it_and_is_not_a_failure(securityhub_config: None, tmp_path: Path, mocker: MockerFixture) -> None:
    # arrange
    install(mocker, FakeImporter([delivery("imported", 2), delivery("not_enabled", 0, "InvalidAccessException: not subscribed")]))

    # act
    with pytest.warns(UserWarning, match="Security Hub is not enabled") as caught:
        SecurityHubWriter().write(entries(), tmp_path / "securityhub-findings.json", MANIFEST, None)

    # assert
    expected = [
        "Security Hub is not enabled in eu-west-1 for dev: their findings were not imported. "
        + "Enable Security Hub there, or remove securityhub from reporting.report_formats."
    ]
    assert expected == [str(warning.message) for warning in caught]


def test_access_denied_raises_after_the_rewrite_naming_the_redeploy_and_the_file(
    securityhub_config: None, tmp_path: Path, mocker: MockerFixture
) -> None:
    """The file carries the outcome before the raise, so the CatsError can point at it."""
    # arrange
    output_path = tmp_path / "securityhub-findings.json"
    install(mocker, FakeImporter([delivery("access_denied", 0, REDEPLOY), delivery("imported", 1)]))

    # act
    with pytest.raises(CatsError) as excinfo:
        SecurityHubWriter().write(entries(), output_path, MANIFEST, None)
    document = json.loads(output_path.read_text())

    # assert
    expected = (
        f"prod: {REDEPLOY}. What was sent is in {output_path} (replay: see 'The findings file' in docs/api-reference/Reports.md).",
        ["access_denied", "imported"],
    )
    assert expected == (str(excinfo.value), [account["delivery"]["status"] for account in document["accounts"]])


def test_failure_with_no_output_path_says_how_to_get_a_replayable_copy(securityhub_config: None, mocker: MockerFixture) -> None:
    # arrange
    install(mocker, FakeImporter([delivery("failed", 1, "InternalException: Internal server error"), delivery("imported", 1)]))

    # act
    with pytest.raises(CatsError) as excinfo:
        SecurityHubWriter().write(entries(), None, MANIFEST, None)

    # assert
    expected = (
        "prod: InternalException: Internal server error. Report files are disabled, so nothing was written; "
        + "re-run with reporting.latest_files or reporting.timestamped_files to keep a replayable copy."
    )
    assert expected == str(excinfo.value)


def test_every_undelivered_account_is_named_in_one_error(securityhub_config: None, tmp_path: Path, mocker: MockerFixture) -> None:
    # arrange
    output_path = tmp_path / "securityhub-findings.json"
    install(mocker, FakeImporter([delivery("access_denied", 0, REDEPLOY), delivery("failed", 0, "InternalException: Internal server error")]))

    # act
    with pytest.raises(CatsError) as excinfo:
        SecurityHubWriter().write(entries(), output_path, MANIFEST, None)

    # assert
    expected = (
        f"prod: {REDEPLOY}; dev: InternalException: Internal server error. "
        + f"What was sent is in {output_path} (replay: see 'The findings file' in docs/api-reference/Reports.md)."
    )
    assert expected == str(excinfo.value)


def test_no_attributed_entries_writes_an_empty_accounts_list_and_imports_nothing(
    securityhub_config: None, tmp_path: Path, mocker: MockerFixture
) -> None:
    # arrange
    output_path = tmp_path / "securityhub-findings.json"
    importer = FakeImporter([])
    install(mocker, importer)

    # act
    notes = SecurityHubWriter().write([make_entry("passed", accounts=[])], output_path, MANIFEST, None)
    document = json.loads(output_path.read_text())

    # assert
    expected: tuple[list[AccountFindings], list[Any], int, list[str]] = (
        [],
        [],
        1,
        ["Security Hub (eu-west-1): 1 tests unattributed (no account fixture)"],
    )
    assert expected == (importer.accounts, document["accounts"], document["unattributed"], notes)


def test_no_entries_at_all_says_so(securityhub_config: None, tmp_path: Path, mocker: MockerFixture) -> None:
    # arrange
    install(mocker, FakeImporter([]))

    # act
    notes = SecurityHubWriter().write([], tmp_path / "securityhub-findings.json", MANIFEST, None)

    # assert
    expected = ["Security Hub (eu-west-1): no findings: no test recorded an account under test"]
    assert expected == notes


def test_format_not_configured_is_a_cats_error(cats_yaml: Path, tmp_path: Path) -> None:
    """The plugin only dispatches configured formats; a direct call without the format has no Region to send to."""
    # arrange
    write_config(cats_yaml, make_config())

    # act
    with pytest.raises(CatsError) as excinfo:
        SecurityHubWriter().write([], tmp_path / "securityhub-findings.json", MANIFEST, None)

    # assert
    expected = "securityhub writer invoked without securityhub in reporting.report_formats"
    assert expected == str(excinfo.value)
