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

"""Security Hub report writer — imports one finding per test per account under test.

Writes ``securityhub-findings.json`` (what was sent, and per-account delivery status)
when report files are on, and imports the findings either way.

The finding model (ASFF, ``AwsSecurityFinding``):

- One finding per (test entry, account in ``accounts_in_test``). A cross-account test
  produces a finding in every participating account's Security Hub; an entry with no
  account fixture has nowhere to go and is counted as unattributed.
- ``Id`` is ``cats/{cfn_stack_prefix}{keyname}/{aws_region}/{uuid5(nodeid)}``: stable across
  runs, so a re-run updates the finding in place and never duplicates it, and namespaced by
  the CATS stack name and deploy Region so two CATS projects testing one account, or two
  regional runs of one suite, do not collide.
- A pass is imported as ``PASSED`` and ``ARCHIVED``: the same import call that would
  raise a failure retires the previous one, with no second API or permission, and the
  evidence stays queryable (``RecordState=ARCHIVED, ProductName=CATS``).
- ``Severity`` and ``Types`` are set both top-level and in ``FindingProviderFields``,
  which is where Security Hub reads them on an update.
- ``Workflow``, ``Note``, ``UserDefinedFields`` and ``VerificationState`` are never set:
  the API ignores them on re-import, so setting them would promise an update that
  cannot happen.
"""

import json
import time
import warnings
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict
from uuid import NAMESPACE_URL, uuid5

from boto3.session import Session
from botocore.exceptions import BotoCoreError, ClientError

from cats.cfn.session import BOTO_RETRY_CONFIG
from cats.config import SecurityHubSeverity, Settings
from cats.exceptions import CatsError, CatsInfrastructureError
from cats.hookspecs import StandardMetadata
from cats.report_run import REPORT_SCHEMA_VERSION, ReportFile, RunManifest, rfc3339_timestamp
from cats.report_writers.standards import decorator_metadata_for, resolve_standard
from cats.reporting import AccountReportDetail, ReportEntry
from cats.resources.session_interact import get_role_arn, get_test_session

if TYPE_CHECKING:
    from mypy_boto3_securityhub.client import SecurityHubClient
    from mypy_boto3_securityhub.literals import ComplianceStatusType, RecordStateType, SeverityLabelType
    from mypy_boto3_securityhub.type_defs import AwsSecurityFindingTypeDef, BatchImportFindingsResponseTypeDef, ComplianceTypeDef, ResourceTypeDef

# Namespace for deterministic finding ids: uuid5(namespace, nodeid). Distinct from the
# OSCAL namespace so the two writers' identifiers never look interchangeable.
CATS_SECURITYHUB_NAMESPACE = uuid5(NAMESPACE_URL, "https://github.com/awslabs/cats/securityhub")

ASFF_SCHEMA_VERSION = "2018-10-08"
PRODUCT_NAME = "CATS"
# ASFF Types are namespace/category/classifier; the classifier is the standard name.
TYPE_PREFIX = "Software and Configuration Checks/Industry and Regulatory Standards"
# The classifier when a test carries no @cats standard.
TYPE_CLASSIFIER_NO_STANDARD = "CATS"

# Field limits from the AwsSecurityFinding, Compliance and Resource API references.
TITLE_LIMIT = 256
DESCRIPTION_LIMIT = 1024
GENERATOR_ID_LIMIT = 512
TYPES_LIMIT = 50
RELATED_REQUIREMENTS_LIMIT = 32
PRODUCT_FIELDS_LIMIT = 50
PRODUCT_FIELD_KEY_LIMIT = 128
PRODUCT_FIELD_VALUE_LIMIT = 2048
# Resources[].Tags follow the AWS tag limits (API_Resource: "Tags must follow AWS tag naming limits"), not the ProductFields ones.
RESOURCE_TAGS_LIMIT = 50
RESOURCE_TAG_KEY_LIMIT = 128
RESOURCE_TAG_VALUE_LIMIT = 256

# ASFF timestamps: RFC 3339 with millisecond precision and a literal Z.
_ASFF_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.{ms:03d}Z"


@dataclass(frozen=True)
class _Outcome:
    """How one pytest result appears in Security Hub."""

    compliance_status: "ComplianceStatusType"
    record_state: "RecordStateType"
    severity: SecurityHubSeverity | None  # None: the configured failed_severity


# pytest result → Security Hub outcome. ACTIVE for anything that needs attention; ARCHIVED
# retires an earlier failure with the same Id. Errors are NOT_AVAILABLE (evidence missing,
# not a control failing) and MEDIUM so they still surface; an expected failure is a known,
# accepted gap kept visible at LOW; a skip is still sent so a stale failure is retired when
# someone skips the test.
_OUTCOMES: dict[str, _Outcome] = {
    "failed": _Outcome("FAILED", "ACTIVE", None),
    "error": _Outcome("NOT_AVAILABLE", "ACTIVE", SecurityHubSeverity.MEDIUM),
    "xfailed": _Outcome("FAILED", "ACTIVE", SecurityHubSeverity.LOW),
    "passed": _Outcome("PASSED", "ARCHIVED", SecurityHubSeverity.INFORMATIONAL),
    "xpassed": _Outcome("PASSED", "ARCHIVED", SecurityHubSeverity.INFORMATIONAL),
    "skipped": _Outcome("NOT_AVAILABLE", "ARCHIVED", SecurityHubSeverity.INFORMATIONAL),
}

_SEVERITY_LABELS: dict[SecurityHubSeverity, "SeverityLabelType"] = {
    SecurityHubSeverity.CRITICAL: "CRITICAL",
    SecurityHubSeverity.HIGH: "HIGH",
    SecurityHubSeverity.MEDIUM: "MEDIUM",
    SecurityHubSeverity.LOW: "LOW",
    SecurityHubSeverity.INFORMATIONAL: "INFORMATIONAL",
}


@dataclass(frozen=True)
class FindingContext:
    """What every finding of one run shares: where it goes, who sent it, when."""

    partition: str
    region: str  # the Region whose Security Hub receives the findings
    aws_region: str  # the Region the stacks deploy into: part of the finding Id and Resources[0].Region
    cfn_stack_prefix: str
    failed_severity: SecurityHubSeverity
    run_id: str
    cats_version: str
    updated_at: str  # ASFF timestamp, see asff_timestamp()


@dataclass(frozen=True)
class AccountFindings:
    """Every finding bound for one account under test."""

    account_name: str  # config keyname: names the test role that imports
    account_id: str
    findings: list["AwsSecurityFindingTypeDef"]


def asff_timestamp(moment: datetime) -> str:
    """``2026-09-24T10:53:40.120Z``: the timestamp format Security Hub accepts. ``moment`` must be UTC."""
    return moment.strftime(_ASFF_TIMESTAMP_FORMAT).format(ms=moment.microsecond // 1000)


def product_arn(partition: str, region: str, account_id: str) -> str:
    """The account's own default product: the only product a custom integration may import into."""
    return f"arn:{partition}:securityhub:{region}:{account_id}:product/{account_id}/default"


def finding_id(cfn_stack_prefix: str, account_name: str, aws_region: str, nodeid: str) -> str:
    """``cats/{prefix}{keyname}/{aws_region}/{uuid5(nodeid)}``: the same test against the same account and Region is the same finding.

    An ``Id`` is sticky for 90 days in the customer's Security Hub, so a scheme change would orphan every
    ACTIVE finding; the scheme is part of the compatibility contract (docs/concepts/versioning.md). The
    Region component keeps two regional runs of one suite into one aggregated hub from overwriting each other.
    """
    return f"cats/{cfn_stack_prefix}{account_name}/{aws_region}/{uuid5(CATS_SECURITYHUB_NAMESPACE, nodeid)}"


def _clip(text: str, limit: int) -> str:
    return text[:limit]


def _description(entry: ReportEntry) -> str:
    """The docstring with its whitespace collapsed to single spaces, else the test name."""
    docstring = entry["test_docstring"]
    collapsed = " ".join(docstring.split()) if docstring is not None else ""
    return _clip(collapsed or entry["test_name"], DESCRIPTION_LIMIT)


def _types(entry: ReportEntry) -> list[str]:
    """One ASFF type per standard; a standard name's own slashes would add segments, so they become dashes."""
    classifiers = [standard.replace("/", "-") for standard in entry["standards"]] or [TYPE_CLASSIFIER_NO_STANDARD]
    return [f"{TYPE_PREFIX}/{classifier}" for classifier in classifiers][:TYPES_LIMIT]


def _related_requirements(entry: ReportEntry, standards_registry: Mapping[str, StandardMetadata] | None) -> list[str]:
    """Per standard in source order: the standard name, then its resolved control ids, without repeats."""
    requirements: list[str] = []
    for standard in entry["standards"]:
        resolved = resolve_standard(standard, decorator_metadata_for(entry, standard), standards_registry)
        for requirement in (standard, *resolved.control_ids):
            if requirement not in requirements:
                requirements.append(requirement)
    return requirements[:RELATED_REQUIREMENTS_LIMIT]


def _compliance(entry: ReportEntry, outcome: _Outcome, standards_registry: Mapping[str, StandardMetadata] | None) -> "ComplianceTypeDef":
    """Status and requirements only. ``StatusReasons`` is not used: the API validates its ``ReasonCode``
    against a closed list of AWS Config conditions, none of which describes a test result. The failure
    message and the skip reason travel in ``ProductFields`` (``cats/error_message``, ``cats/reason``)."""
    compliance: ComplianceTypeDef = {"Status": outcome.compliance_status}
    requirements = _related_requirements(entry, standards_registry)
    if requirements:
        compliance["RelatedRequirements"] = requirements
    return compliance


def _non_blank(pairs: Mapping[str, str], limit: int, key_limit: int, value_limit: int) -> dict[str, str]:
    """The API requires non-blank keys and values (pattern ``.*\\S.*``); clip both, drop blanks, cap the count."""
    kept: dict[str, str] = {}
    for key, value in pairs.items():
        if len(kept) == limit:
            break
        clipped_key, clipped_value = _clip(key, key_limit), _clip(value, value_limit)
        if clipped_key.strip() and clipped_value.strip():
            kept[clipped_key] = clipped_value
    return kept


def _resource(account: AccountReportDetail, region: str) -> "ResourceTypeDef":
    """The account itself, tagged as cats.yaml tags it; a valueless tag becomes ``"true"`` (the OSCAL and JUnit convention)."""
    tags = {key: (value if value is not None else "true") for key, value in account["tags"].items()}
    resource: ResourceTypeDef = {"Type": "AwsAccount", "Id": f"AWS::::Account:{account['account_id']}", "Region": region}
    if tags:
        resource["Tags"] = _non_blank(tags, RESOURCE_TAGS_LIMIT, RESOURCE_TAG_KEY_LIMIT, RESOURCE_TAG_VALUE_LIMIT)
    return resource


def _product_fields(entry: ReportEntry, account: AccountReportDetail, context: FindingContext) -> dict[str, str]:
    """The CATS fields first, so the cap can only ever drop marker and metadata pairs.

    ``cats/error_message`` and ``cats/reason`` are the ReportEntry columns of the same name; blank
    when they do not apply, and blanks are dropped below.
    """
    fields: dict[str, str] = {
        "cats/version": context.cats_version,
        "cats/run_id": context.run_id,
        "cats/nodeid": entry["nodeid"],
        "cats/account_name": account["account_name"],
        "cats/aws_region": context.aws_region,
        "cats/result": entry["result"],
        "cats/accounts_in_test": ",".join(participant["account_name"] for participant in entry["accounts_in_test"]),
        "cats/error_message": entry["error_message"] or "",
        "cats/reason": entry["reason"] or "",
    }
    for marker, values in entry["custom_markers"].items():
        fields[f"cats/marker/{marker}"] = ",".join(str(value) for value in values)
    for key, value in entry["additional_metadata"].items():
        fields[f"cats/metadata/{key}"] = str(value)
    return _non_blank(fields, PRODUCT_FIELDS_LIMIT, PRODUCT_FIELD_KEY_LIMIT, PRODUCT_FIELD_VALUE_LIMIT)


def _finding(
    entry: ReportEntry, account: AccountReportDetail, context: FindingContext, standards_registry: Mapping[str, StandardMetadata] | None
) -> "AwsSecurityFindingTypeDef":
    outcome = _OUTCOMES[entry["result"]]
    severity = _SEVERITY_LABELS[outcome.severity if outcome.severity is not None else context.failed_severity]
    types = _types(entry)
    return {
        "SchemaVersion": ASFF_SCHEMA_VERSION,
        "Id": finding_id(context.cfn_stack_prefix, account["account_name"], context.aws_region, entry["nodeid"]),
        "ProductArn": product_arn(context.partition, context.region, account["account_id"]),
        "ProductName": PRODUCT_NAME,
        "GeneratorId": _clip(entry["nodeid"], GENERATOR_ID_LIMIT),
        "AwsAccountId": account["account_id"],
        "CreatedAt": context.updated_at,
        "UpdatedAt": context.updated_at,
        "Title": _clip(entry["test_name"], TITLE_LIMIT),
        "Description": _description(entry),
        "Types": types,
        "Severity": {"Label": severity},
        "FindingProviderFields": {"Severity": {"Label": severity}, "Types": types},
        "Resources": [_resource(account, context.aws_region)],
        "Compliance": _compliance(entry, outcome, standards_registry),
        "RecordState": outcome.record_state,
        "ProductFields": _product_fields(entry, account, context),
        "Remediation": {
            "Recommendation": {
                "Text": "Updated on every 'cats test' run and archived when the test passes. "
                + f"See report.json and securityhub-findings.json for run {context.run_id}.",
            },
        },
    }


@dataclass(frozen=True)
class SkippedEntries:
    """Entries that produced no finding, and why, so the summary line and the findings file can say so."""

    unattributed: int  # no account fixture: nowhere to go
    no_standard: int  # no @cats standard: nothing to attest to (only_with_standard, the default)
    not_run: int = 0  # held back by test_selection: no control state was produced, so nothing to import


def build_account_findings(
    entries: Sequence[ReportEntry],
    context: FindingContext,
    standards_registry: Mapping[str, StandardMetadata] | None,
    only_with_standard: bool = True,
) -> tuple[list[AccountFindings], SkippedEntries]:
    """One finding per (entry, account in the entry), grouped by account in first-seen order.

    Pure. Entries with no account fixture have nowhere to go; with ``only_with_standard`` (the
    default) entries carrying no standard have nothing to attest to; a ``not_run`` entry produced
    no control state (Security Hub holds control state, and an exclusion is a risk acceptance,
    which is ``Workflow.Status: SUPPRESSED`` on the existing finding and the user's to set). All
    three are counted and reported rather than silently dropped.
    """
    by_account: dict[str, AccountFindings] = {}
    unattributed = 0
    no_standard = 0
    not_run = 0
    for entry in entries:
        if entry["result"] == "not_run":
            not_run += 1
            continue
        if not entry["accounts_in_test"]:
            unattributed += 1
            continue
        if only_with_standard is True and len(entry["standards"]) == 0:
            no_standard += 1
            continue
        for account in entry["accounts_in_test"]:
            group = by_account.setdefault(
                account["account_id"],
                AccountFindings(account_name=account["account_name"], account_id=account["account_id"], findings=[]),
            )
            group.findings.append(_finding(entry, account, context, standards_registry))
    return list(by_account.values()), SkippedEntries(unattributed=unattributed, no_standard=no_standard, not_run=not_run)


class SecurityHubWriter:
    """Build ASFF findings from the session's entries and import them into each account's Security Hub."""

    file = ReportFile(stem="securityhub-findings", suffix=".json")

    def write(
        self,
        reports: Sequence[ReportEntry],
        output_path: Path | None,
        run: RunManifest,
        standards_registry: Mapping[str, StandardMetadata] | None,
    ) -> list[str] | None:
        """Write what will be sent, import it, write what happened; raise if an account got nothing.

        The file goes to disk before the first API call, so an import that fails or is
        interrupted loses nothing: a file still holding ``"delivery": null`` after a run was
        never confirmed sent, and every account's list can be replayed as-is (import is
        idempotent by ``Id``). With report files off there is no file; the import still runs
        and the error text carries what the file would have.
        """
        settings = Settings()
        region = settings.effective_securityhub_region()
        if region is None:
            raise CatsError("securityhub writer invoked without securityhub in reporting.report_formats")
        context = FindingContext(
            partition=settings.cats_deployment_settings.partition,
            region=region,
            aws_region=settings.require_aws_region(),
            cfn_stack_prefix=settings.cats_deployment_settings.cfn_stack_prefix,
            failed_severity=settings.reporting.securityhub.failed_severity,
            run_id=run["run_id"],
            cats_version=run["cats_version"],
            updated_at=asff_timestamp(datetime.fromisoformat(run["finished_at"])),
        )
        accounts, skipped = build_account_findings(reports, context, standards_registry, settings.reporting.securityhub.only_with_standard)

        document = _document(context, accounts, skipped, deliveries=None)
        if output_path is not None:
            _dump(document, output_path)  # first write: nothing sent yet

        deliveries = import_findings(accounts, context)
        if output_path is not None:
            _dump(_document(context, accounts, skipped, deliveries), output_path)  # second write: the outcome

        not_enabled = [account.account_name for account, delivery in zip(accounts, deliveries, strict=True) if delivery["status"] == "not_enabled"]
        if not_enabled:
            warnings.warn(
                f"Security Hub is not enabled in {region} for {', '.join(not_enabled)}: their findings were not imported. "
                + "Enable Security Hub there, or remove securityhub from reporting.report_formats.",
                stacklevel=2,
            )

        undelivered = [(account, delivery) for account, delivery in zip(accounts, deliveries, strict=True) if delivery["status"] in UNDELIVERED]
        if undelivered:
            raise CatsError(_failure_text(undelivered, output_path))
        return [summary_line(region, accounts, deliveries, skipped)]


UNDELIVERED: frozenset[str] = frozenset({"access_denied", "failed"})
# Concurrent accounts: each on its own assumed-role session against a rate-limited API.
MAX_CONCURRENT_ACCOUNTS = 8
BATCH_SIZE = 100  # BatchImportFindings accepts at most 100 findings per call
# How many rejected findings the failure text names before summarizing.
REJECTIONS_SHOWN = 3
# How long an AccessDeniedException on the import is retried before it is diagnosed. The deploy
# readiness gate proves the test role's policy document live with cloudformation:DescribeStacks, which
# a stack *update* that adds the securityhub statement already allowed under the old document, so
# the gate cannot see that statement converge; IAM does so within seconds, and this covers them.
ACCESS_DENIED_RETRY_SECONDS = 30
ACCESS_DENIED_RETRY_INTERVAL_SECONDS = 3


class SecurityHubDelivery(TypedDict):
    """What happened when one account's findings were imported.

    - ``imported``: every finding was accepted.
    - ``not_enabled``: Security Hub is not enabled in the account and Region; skipped, not a failure.
    - ``access_denied``: the test role lacks the import grant; the stack predates the format.
    - ``failed``: the role could not be assumed, the API rejected findings, or a call failed after retries.
    """

    status: Literal["imported", "not_enabled", "access_denied", "failed"]
    findings_imported: int  # accepted by the API; can be partial on failed
    detail: str | None  # the API message or remediation; None when imported
    delivered_at: str  # ASFF timestamp of the outcome


class SecurityHubAccount(TypedDict):
    """One account's block in securityhub-findings.json: where the findings went, how, and exactly what was sent."""

    account_name: str
    account_id: str
    role_arn: str
    product_arn: str
    delivery: SecurityHubDelivery | None  # None until the import has been attempted
    findings: list["AwsSecurityFindingTypeDef"]


class SecurityHubDocument(TypedDict):
    """securityhub-findings.json: the document's shape version, the run, then one block per account under test in the evidence.

    ``schema_version`` follows the report.json rule (moves only with a breaking change; additive
    fields are minor). ``unattributed`` counts entries with no account fixture, which have no
    Security Hub to go to; ``no_standard`` counts entries left out because they carry no standard
    (``securityhub.only_with_standard``); ``not_run`` counts tests ``test_selection`` held back,
    which produced no control state to import. Every key is always present (the report.json convention).
    """

    schema_version: int
    run_id: str
    cats_version: str
    region: str
    aws_region: str
    unattributed: int
    no_standard: int
    not_run: int
    accounts: list[SecurityHubAccount]


def role_arn(context: FindingContext, account: AccountFindings) -> str:
    """The account's CATS default test role: the identity that imports, and the only one the grant is on."""
    return get_role_arn(f"{context.cfn_stack_prefix}{account.account_name}-default-test-role", account.account_id, context.partition)


def _document(
    context: FindingContext,
    accounts: Sequence[AccountFindings],
    skipped: SkippedEntries,
    deliveries: Sequence[SecurityHubDelivery] | None,
) -> SecurityHubDocument:
    return SecurityHubDocument(
        schema_version=REPORT_SCHEMA_VERSION,
        run_id=context.run_id,
        cats_version=context.cats_version,
        region=context.region,
        aws_region=context.aws_region,
        unattributed=skipped.unattributed,
        no_standard=skipped.no_standard,
        not_run=skipped.not_run,
        accounts=[
            SecurityHubAccount(
                account_name=account.account_name,
                account_id=account.account_id,
                role_arn=role_arn(context, account),
                product_arn=product_arn(context.partition, context.region, account.account_id),
                delivery=deliveries[index] if deliveries is not None else None,
                findings=account.findings,
            )
            for index, account in enumerate(accounts)
        ],
    )


def _dump(document: SecurityHubDocument, output_path: Path) -> None:
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(document, f, indent=2)


def _delivery(
    status: Literal["imported", "not_enabled", "access_denied", "failed"], findings_imported: int, detail: str | None
) -> SecurityHubDelivery:
    return SecurityHubDelivery(
        status=status, findings_imported=findings_imported, detail=detail, delivered_at=rfc3339_timestamp(datetime.now(tz=UTC))
    )


def _import_batch(client: "SecurityHubClient", batch: list["AwsSecurityFindingTypeDef"]) -> "BatchImportFindingsResponseTypeDef":
    """One BatchImportFindings call, retrying an AccessDeniedException within ACCESS_DENIED_RETRY_SECONDS; the last one propagates."""
    started = time.monotonic()
    while True:
        try:
            return client.batch_import_findings(Findings=batch)
        except ClientError as e:
            if e.response["Error"]["Code"] != "AccessDeniedException":
                raise
            if time.monotonic() - started >= ACCESS_DENIED_RETRY_SECONDS:
                raise
            time.sleep(ACCESS_DENIED_RETRY_INTERVAL_SECONDS)


def _import_account(account: AccountFindings, context: FindingContext, make_session: Callable[[str, str, str], Session]) -> SecurityHubDelivery:
    """Import one account's findings through its test role, 100 per call. Never raises: the outcome is the return."""
    try:
        session = make_session(role_arn(context, account), account.account_name, context.region)
        client: SecurityHubClient = session.client("securityhub", config=BOTO_RETRY_CONFIG)
    except (CatsInfrastructureError, BotoCoreError) as e:
        return _delivery("failed", 0, str(e))

    imported = 0
    for start in range(0, len(account.findings), BATCH_SIZE):
        batch = account.findings[start : start + BATCH_SIZE]
        try:
            response = _import_batch(client, batch)
        except BotoCoreError as e:
            return _delivery("failed", imported, str(e))
        except ClientError as e:
            code, message = e.response["Error"]["Code"], e.response["Error"].get("Message", "")
            if code == "InvalidAccessException":
                return _delivery("not_enabled", imported, f"{code}: {message}")
            if code == "AccessDeniedException":
                return _delivery(
                    "access_denied",
                    imported,
                    f"Test role lacks securityhub:BatchImportFindings after {ACCESS_DENIED_RETRY_SECONDS}s of retries: the stack was deployed "
                    + f"before the securityhub format was enabled (redeploy: cats infra deploy {account.account_name}), "
                    + "or an update that added it has not converged in IAM yet (run again).",
                )
            return _delivery("failed", imported, f"{code}: {message}")
        imported += response["SuccessCount"]
        if response["FailedCount"] > 0:
            rejected = response["FailedFindings"]
            shown = "; ".join(f"{finding['Id']}: {finding['ErrorCode']} {finding['ErrorMessage']}" for finding in rejected[:REJECTIONS_SHOWN])
            more = f"; and {len(rejected) - REJECTIONS_SHOWN} more" if len(rejected) > REJECTIONS_SHOWN else ""
            return _delivery("failed", imported, f"{response['FailedCount']} of {len(batch)} findings rejected: {shown}{more}")
    return _delivery("imported", imported, None)


def import_findings(
    accounts: Sequence[AccountFindings],
    context: FindingContext,
    make_session: Callable[[str, str, str], Session] = get_test_session,
) -> list[SecurityHubDelivery]:
    """Import every account's findings, accounts concurrently, each on its own assumed-role session.

    Returns one delivery per account in the same order. One account's outcome never affects
    another's, and nothing here raises: the writer decides what a failed delivery means.
    ``make_session`` is ``get_test_session``; tests inject a session onto a mocked client.
    """
    if not accounts:
        return []
    with ThreadPoolExecutor(max_workers=min(MAX_CONCURRENT_ACCOUNTS, len(accounts))) as pool:
        return list(pool.map(lambda account: _import_account(account, context, make_session), accounts))


def _describe(account: AccountFindings, delivery: SecurityHubDelivery) -> str:
    """One clause of the summary line: ``prod 42 findings imported`` or ``dev not enabled, skipped``.

    Only those two statuses reach here: ``write`` raises before ``summary_line`` when any
    account is ``access_denied`` or ``failed``, and ``_failure_text`` describes those.
    """
    if delivery["status"] == "imported":
        return f"{account.account_name} {delivery['findings_imported']} findings imported"
    return f"{account.account_name} not enabled, skipped"


def summary_line(region: str, accounts: Sequence[AccountFindings], deliveries: Sequence[SecurityHubDelivery], skipped: SkippedEntries) -> str:
    """``Security Hub (eu-west-1): prod 42 findings imported; dev not enabled, skipped; 3 tests unattributed (no account fixture); 2 tests
    without a standard (not imported); 4 tests not run (test_selection; not imported)``."""
    clauses = [_describe(account, delivery) for account, delivery in zip(accounts, deliveries, strict=True)]
    if skipped.unattributed > 0:
        clauses.append(f"{skipped.unattributed} tests unattributed (no account fixture)")
    if skipped.no_standard > 0:
        clauses.append(f"{skipped.no_standard} tests without a standard (not imported)")
    if skipped.not_run > 0:
        clauses.append(f"{skipped.not_run} tests not run (test_selection; not imported)")
    if not clauses:
        clauses.append("no findings: no test recorded an account under test")
    return f"Security Hub ({region}): " + "; ".join(clauses)


def _failure_text(undelivered: Sequence[tuple[AccountFindings, SecurityHubDelivery]], output_path: Path | None) -> str:
    """Self-contained: pytest's own summary section does not print when session finish raises."""
    accounts = "; ".join(f"{account.account_name}: {delivery['detail']}" for account, delivery in undelivered)
    if output_path is not None:
        where = f"What was sent is in {output_path} (replay: see 'The findings file' in docs/api-reference/Reports.md)."
    else:
        where = (
            "Report files are disabled, so nothing was written; "
            + "re-run with reporting.latest_files or reporting.timestamped_files to keep a replayable copy."
        )
    return f"{accounts}. {where}"
