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

"""Tests for cats.report_run.build_run_manifest: the ``run`` block of report.json."""

from datetime import UTC, datetime
from pathlib import Path

from cats.report_run import (
    JUNIT_FILE,
    CallerIdentity,
    ReportFile,
    ReportRun,
    RunGit,
    RunManifest,
    RunProvenance,
    RunSelection,
    RunTestPack,
    build_run_manifest,
)
from cats.reporting import AccountReportDetail, ReportEntry, ResultType

REPORT = ReportFile(stem="report", suffix=".json")
STARTED_AT = datetime(2026, 9, 24, 10, 53, 17, tzinfo=UTC)
FINISHED_AT = datetime(2026, 9, 24, 10, 55, 2, tzinfo=UTC)
FULL_RUN = RunSelection(keyword="", markexpr="", last_failed=False, deselect=(), explicit_args=(), stopped_early="")
PROD = AccountReportDetail(account_name="prod", account_id="111111111111", tags={})
DEV = AccountReportDetail(account_name="dev", account_id="222222222222", tags={"env": "dev"})
CI_RUNNER = CallerIdentity(account_id="999999999999", arn="arn:aws:sts::999999999999:assumed-role/ci-runner/job-8812")
CLEAN_CHECKOUT = RunProvenance(
    test_packs=[RunTestPack(name="acme-controls", version="0.99.4", metadata={})],
    git=RunGit(commit="3f2a9c1e0d4b7f8a9c2e1d3f5b6a7c8d9e0f1a2b", dirty=False),
    config_sha256="71aa0c2d6e8f1b3a5c7d9e0f2a4b6c8d0e1f3a5b7c9d1e3f5a7b9c1d3e5f7a9b",
)


def make_entry(result: ResultType, accounts: list[AccountReportDetail]) -> ReportEntry:
    return {
        "result": result,
        "accounts_in_test": accounts,
        "test_name": f"test_{result}",
        "nodeid": f"tests/test_a.py::test_{result}",
        "test_docstring": None,
        "standards": [],
        "standard_metadata": {},
        "custom_markers": {},
        "additional_metadata": {},
        "xfail": False,
        "error_message": None,
        "reason": None,
        "duration_s": 0.1,
        "recorded_at": "2026-09-24T10:53:17+00:00",
    }


def build(
    run: ReportRun,
    selection: RunSelection,
    entries: list[ReportEntry],
    files: list[ReportFile],
    exit_status: int = 0,
    caller_identity: CallerIdentity | None = CI_RUNNER,
    provenance: RunProvenance = CLEAN_CHECKOUT,
) -> RunManifest:
    return build_run_manifest(
        run=run,
        selection=selection,
        entries=entries,
        files=files,
        finished_at=FINISHED_AT,
        exit_status=exit_status,
        pytest_args=("-vv", "-ra"),
        config_path=Path("cats.yaml"),
        aws_region="eu-west-1",
        partition="aws",
        cfn_stack_prefix="CATS-test-infra-",
        cats_version="0.99.3",
        python_version="3.14.6",
        pytest_version="9.1.1",
        caller_identity=caller_identity,
        provenance=provenance,
    )


def test_full_run_manifest() -> None:
    # arrange
    run = ReportRun(report_dir=Path("reports"), run_id="20260924T105317Z", started_at=STARTED_AT, latest_files=True, timestamped_files=True)
    entries = [make_entry("passed", [PROD, DEV]), make_entry("failed", [PROD])]

    # act
    manifest = build(run, FULL_RUN, entries, [REPORT, JUNIT_FILE], exit_status=1)

    # assert
    expected: RunManifest = {
        "run_id": "20260924T105317Z",
        "started_at": "2026-09-24T10:53:17.000000Z",
        "finished_at": "2026-09-24T10:55:02.000000Z",
        "role_session_name": "cats-20260924T105317Z",
        "caller_identity": {"account_id": "999999999999", "arn": "arn:aws:sts::999999999999:assumed-role/ci-runner/job-8812"},
        "test_packs": [{"name": "acme-controls", "version": "0.99.4", "metadata": {}}],
        "git": {"commit": "3f2a9c1e0d4b7f8a9c2e1d3f5b6a7c8d9e0f1a2b", "dirty": False},
        "config_sha256": "71aa0c2d6e8f1b3a5c7d9e0f2a4b6c8d0e1f3a5b7c9d1e3f5a7b9c1d3e5f7a9b",
        "cats_version": "0.99.3",
        "python_version": "3.14.6",
        "pytest_version": "9.1.1",
        "pytest_args": ["-vv", "-ra"],
        "config_path": "cats.yaml",
        "aws_region": "eu-west-1",
        "partition": "aws",
        "cfn_stack_prefix": "CATS-test-infra-",
        "exit_status": 1,
        "selection": {
            "keyword": "",
            "markexpr": "",
            "last_failed": False,
            "deselect": [],
            "explicit_args": [],
            "stopped_early": "",
            "provided": [],
            "config_excluded": [],
            "unmatched_exclusions": [],
        },
        "counts": {"total": 2, "passed": 1, "failed": 1, "error": 0, "skipped": 0, "xfailed": 0, "xpassed": 0, "not_run": 0},
        "accounts": [{"account_name": "prod", "account_id": "111111111111"}, {"account_name": "dev", "account_id": "222222222222"}],
        "outputs": ["report-20260924T105317Z.json", "report.json", "junit-20260924T105317Z.xml", "junit.xml"],
        "writers": [],
    }
    assert expected == manifest


def test_narrowed_run_records_the_selection_as_given() -> None:
    """Every selector and the early stop land in run.selection verbatim; the manifest carries no verdict on them."""
    # arrange
    run = ReportRun(report_dir=Path("reports"), run_id="20260924T105317Z", started_at=STARTED_AT, latest_files=True, timestamped_files=False)
    selection = RunSelection(
        keyword="ssh", markexpr="", last_failed=True, deselect=("a.py::t",), explicit_args=("tests/a.py",), stopped_early="interrupted"
    )

    # act
    manifest = build(run, selection, [make_entry("passed", [PROD])], [REPORT], exit_status=2)

    # assert
    expected = (
        {
            "keyword": "ssh",
            "markexpr": "",
            "last_failed": True,
            "deselect": ["a.py::t"],
            "explicit_args": ["tests/a.py"],
            "stopped_early": "interrupted",
            "provided": [],
            "config_excluded": [],
            "unmatched_exclusions": [],
        },
        ["report.json"],
    )
    assert expected == (manifest["selection"], manifest["outputs"])


def test_accounts_are_deduplicated_in_first_seen_order() -> None:
    # arrange
    run = ReportRun(report_dir=Path("reports"), run_id="20260924T105317Z", started_at=STARTED_AT, latest_files=True, timestamped_files=False)
    entries = [make_entry("passed", [DEV]), make_entry("passed", [PROD, DEV]), make_entry("passed", [DEV])]

    # act
    manifest = build(run, FULL_RUN, entries, [REPORT])

    # assert
    expected = [{"account_name": "dev", "account_id": "222222222222"}, {"account_name": "prod", "account_id": "111111111111"}]
    assert expected == manifest["accounts"]


def test_two_keynames_for_one_account_id_are_both_kept() -> None:
    """run.accounts is keyed by (keyname, account id), so both keynames survive and join back to accounts_in_test."""
    # arrange
    run = ReportRun(report_dir=Path("reports"), run_id="20260924T105317Z", started_at=STARTED_AT, latest_files=True, timestamped_files=False)
    prod_again = AccountReportDetail(account_name="prod_readonly", account_id="111111111111", tags={})
    entries = [make_entry("passed", [PROD]), make_entry("passed", [prod_again])]

    # act
    manifest = build(run, FULL_RUN, entries, [REPORT])

    # assert
    expected = [{"account_name": "prod", "account_id": "111111111111"}, {"account_name": "prod_readonly", "account_id": "111111111111"}]
    assert expected == manifest["accounts"]


def test_no_file_naming_means_no_outputs() -> None:
    # arrange
    run = ReportRun(report_dir=Path("reports"), run_id="20260924T105317Z", started_at=STARTED_AT, latest_files=False, timestamped_files=False)

    # act
    manifest = build(run, FULL_RUN, [], [REPORT, JUNIT_FILE])

    # assert
    expected: list[str] = []
    assert expected == manifest["outputs"]


def test_unknown_caller_identity_is_recorded_as_null() -> None:
    """The key is always present: a run that touched no account, or whose GetCallerIdentity failed, records null."""
    # arrange
    run = ReportRun(report_dir=Path("reports"), run_id="20260924T105317Z", started_at=STARTED_AT, latest_files=True, timestamped_files=False)

    # act
    manifest = build(run, FULL_RUN, [], [REPORT], caller_identity=None)

    # assert
    assert manifest["caller_identity"] is None


def test_unknown_git_and_config_hash_are_recorded_as_null() -> None:
    """A run outside a checkout, or whose config could not be re-read, records null for each and an empty pack list."""
    # arrange
    run = ReportRun(report_dir=Path("reports"), run_id="20260924T105317Z", started_at=STARTED_AT, latest_files=True, timestamped_files=False)

    # act
    manifest = build(run, FULL_RUN, [], [REPORT], provenance=RunProvenance(test_packs=[], git=None, config_sha256=None))

    # assert
    expected: tuple[list[RunTestPack], RunGit | None, str | None] = ([], None, None)
    assert expected == (manifest["test_packs"], manifest["git"], manifest["config_sha256"])
