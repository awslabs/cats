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

"""Tests for cats.report_writers.securityhub.build_account_findings: entries → ASFF findings, pure."""

from typing import Any
from uuid import uuid5

import pytest

from cats.config import SecurityHubSeverity
from cats.hookspecs import StandardMetadata
from cats.report_writers.securityhub import (
    CATS_SECURITYHUB_NAMESPACE,
    AccountFindings,
    FindingContext,
    SkippedEntries,
    build_account_findings,
)
from cats.reporting import AccountReportDetail, ReportEntry, ResultType

UPDATED_AT = "2026-09-24T10:53:40.120Z"
NODEID = "tests/test_network.py::test_prod_cannot_reach_dev"
PROD = AccountReportDetail(account_name="prod", account_id="111111111111", tags={"env": "prod", "pci": None})
DEV = AccountReportDetail(account_name="dev", account_id="222222222222", tags={})
CONTEXT = FindingContext(
    partition="aws",
    region="eu-west-1",
    aws_region="us-east-1",
    cfn_stack_prefix="CATS-test-infra-",
    failed_severity=SecurityHubSeverity.HIGH,
    run_id="20260924T105317Z",
    cats_version="0.99.3",
    updated_at=UPDATED_AT,
)
REMEDIATION = {
    "Recommendation": {
        "Text": "Updated on every 'cats test' run and archived when the test passes. "
        + "See report.json and securityhub-findings.json for run 20260924T105317Z."
    }
}
TYPE_SOC2 = "Software and Configuration Checks/Industry and Regulatory Standards/SOC2-CC6.6"
TYPE_CATS = "Software and Configuration Checks/Industry and Regulatory Standards/CATS"


def make_entry(
    result: ResultType = "failed",
    accounts: list[AccountReportDetail] | None = None,
    standards: list[str] | None = None,
    standard_metadata: dict[str, dict[str, Any]] | None = None,
    custom_markers: dict[str, list[Any]] | None = None,
    additional_metadata: dict[str, Any] | None = None,
    docstring: str | None = "Prove the production network cannot reach development.",
    error_message: str | None = None,
    reason: str | None = None,
    nodeid: str = NODEID,
    test_name: str = "test_prod_cannot_reach_dev",
) -> ReportEntry:
    return {
        "result": result,
        "accounts_in_test": [PROD] if accounts is None else accounts,
        "test_name": test_name,
        "nodeid": nodeid,
        "test_docstring": docstring,
        "standards": ["SOC2-CC6.6"] if standards is None else standards,
        "standard_metadata": standard_metadata or {},
        "custom_markers": custom_markers or {},
        "additional_metadata": additional_metadata or {},
        "xfail": result in ("xfailed", "xpassed"),
        "error_message": error_message,
        "reason": reason,
        "duration_s": 0.5,
        "recorded_at": "2026-09-24T10:53:17+00:00",
    }


def expected_finding(
    result: str,
    compliance: dict[str, Any],
    record_state: str,
    severity: str,
    account: AccountReportDetail = PROD,
    accounts_in_test: str = "prod",
    types: list[str] | None = None,
    product_fields_extra: dict[str, str] | None = None,
    error_message: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """The finding the builder is expected to produce for make_entry() defaults, with one outcome's fields filled in."""
    types = [TYPE_SOC2] if types is None else types
    message_fields = {key: value for key, value in (("cats/error_message", error_message), ("cats/reason", reason)) if value is not None}
    resource: dict[str, Any] = {"Type": "AwsAccount", "Id": f"AWS::::Account:{account['account_id']}", "Region": "us-east-1"}
    if account["tags"]:
        resource["Tags"] = {"env": "prod", "pci": "true"}
    return {
        "SchemaVersion": "2018-10-08",
        "Id": f"cats/CATS-test-infra-{account['account_name']}/us-east-1/{uuid5(CATS_SECURITYHUB_NAMESPACE, NODEID)}",
        "ProductArn": f"arn:aws:securityhub:eu-west-1:{account['account_id']}:product/{account['account_id']}/default",
        "ProductName": "CATS",
        "GeneratorId": NODEID,
        "AwsAccountId": account["account_id"],
        "CreatedAt": UPDATED_AT,
        "UpdatedAt": UPDATED_AT,
        "Title": "test_prod_cannot_reach_dev",
        "Description": "Prove the production network cannot reach development.",
        "Types": types,
        "Severity": {"Label": severity},
        "FindingProviderFields": {"Severity": {"Label": severity}, "Types": types},
        "Resources": [resource],
        "Compliance": compliance,
        "RecordState": record_state,
        "ProductFields": {
            "cats/version": "0.99.3",
            "cats/run_id": "20260924T105317Z",
            "cats/nodeid": NODEID,
            "cats/account_name": account["account_name"],
            "cats/aws_region": "us-east-1",
            "cats/result": result,
            "cats/accounts_in_test": accounts_in_test,
            **message_fields,
            **(product_fields_extra or {}),
        },
        "Remediation": REMEDIATION,
    }


def only_finding(entries: list[ReportEntry], registry: dict[str, StandardMetadata] | None = None, only_with_standard: bool = True) -> dict[str, Any]:
    """The single finding a one-entry, one-account build produces."""
    groups, _ = build_account_findings(entries, CONTEXT, registry, only_with_standard)
    [group] = groups
    [finding] = group.findings
    return dict(finding)


# --- the outcome table, one full expected object per row ---


def test_failed_is_an_active_failed_finding_at_the_configured_severity() -> None:
    # arrange
    entry = make_entry("failed", error_message="AssertionError: assert REFUSED == TIMEOUT")

    # act
    finding = only_finding([entry])

    # assert
    expected = expected_finding(
        "failed",
        compliance={"Status": "FAILED", "RelatedRequirements": ["SOC2-CC6.6"]},
        record_state="ACTIVE",
        severity="HIGH",
        error_message="AssertionError: assert REFUSED == TIMEOUT",
    )
    assert expected == finding


def test_failed_severity_follows_the_configured_label() -> None:
    # arrange
    context = FindingContext(**{**CONTEXT.__dict__, "failed_severity": SecurityHubSeverity.CRITICAL})

    # act
    groups, _ = build_account_findings([make_entry("failed")], context, None)

    # assert
    expected = ({"Label": "CRITICAL"}, {"Label": "CRITICAL"})
    assert expected == (groups[0].findings[0]["Severity"], groups[0].findings[0]["FindingProviderFields"]["Severity"])


def test_error_is_active_not_available_at_medium() -> None:
    """Evidence is missing, not a control failing: still needs attention, so it stays ACTIVE."""
    # arrange
    entry = make_entry("error", error_message="fixture 'prod_account' raised CatsInfrastructureError")

    # act
    finding = only_finding([entry])

    # assert
    expected = expected_finding(
        "error",
        compliance={"Status": "NOT_AVAILABLE", "RelatedRequirements": ["SOC2-CC6.6"]},
        record_state="ACTIVE",
        severity="MEDIUM",
        error_message="fixture 'prod_account' raised CatsInfrastructureError",
    )
    assert expected == finding


def test_xfailed_is_an_active_failed_finding_at_low() -> None:
    """A known, accepted gap stays visible."""
    # arrange
    entry = make_entry("xfailed", reason="known gap until the new SCP lands")

    # act
    finding = only_finding([entry])

    # assert
    expected = expected_finding(
        "xfailed",
        compliance={"Status": "FAILED", "RelatedRequirements": ["SOC2-CC6.6"]},
        record_state="ACTIVE",
        severity="LOW",
        reason="known gap until the new SCP lands",
    )
    assert expected == finding


@pytest.mark.parametrize("result", ["passed", "xpassed"])
def test_pass_is_an_archived_passed_finding(result: ResultType) -> None:
    """ARCHIVED retires an earlier ACTIVE failure with the same Id in the same import call."""
    # arrange
    entry = make_entry(result)

    # act
    finding = only_finding([entry])

    # assert
    expected = expected_finding(
        result,
        compliance={"Status": "PASSED", "RelatedRequirements": ["SOC2-CC6.6"]},
        record_state="ARCHIVED",
        severity="INFORMATIONAL",
    )
    assert expected == finding


def test_skipped_is_an_archived_not_available_finding_with_the_reason() -> None:
    """Still sent, so a stale failure is retired when someone skips the test."""
    # arrange
    entry = make_entry("skipped", reason="no lambda handler in this account")

    # act
    finding = only_finding([entry])

    # assert
    expected = expected_finding(
        "skipped",
        compliance={"Status": "NOT_AVAILABLE", "RelatedRequirements": ["SOC2-CC6.6"]},
        record_state="ARCHIVED",
        severity="INFORMATIONAL",
        reason="no lambda handler in this account",
    )
    assert expected == finding


# --- identity and grouping ---


def test_finding_id_is_stable_across_runs() -> None:
    """A re-run must update the finding in place, so nothing run-specific may enter the Id."""
    # arrange
    later = FindingContext(**{**CONTEXT.__dict__, "run_id": "20260925T080000Z", "updated_at": "2026-09-25T08:00:00.000Z"})

    # act
    first, _ = build_account_findings([make_entry("failed")], CONTEXT, None)
    second, _ = build_account_findings([make_entry("passed")], later, None)

    # assert
    assert first[0].findings[0]["Id"] == second[0].findings[0]["Id"]


def test_finding_id_is_namespaced_by_the_cats_resource_prefix() -> None:
    """Two CATS projects testing the same account must not overwrite each other's findings."""
    # arrange
    other_project = FindingContext(**{**CONTEXT.__dict__, "cfn_stack_prefix": "CATS-other-"})

    # act
    ours, _ = build_account_findings([make_entry()], CONTEXT, None)
    theirs, _ = build_account_findings([make_entry()], other_project, None)

    # assert
    expected = (
        f"cats/CATS-test-infra-prod/us-east-1/{uuid5(CATS_SECURITYHUB_NAMESPACE, NODEID)}",
        f"cats/CATS-other-prod/us-east-1/{uuid5(CATS_SECURITYHUB_NAMESPACE, NODEID)}",
    )
    assert expected == (ours[0].findings[0]["Id"], theirs[0].findings[0]["Id"])


def test_cross_account_entry_produces_one_finding_per_account() -> None:
    """test_prod_cannot_reach_dev(prod, dev) surfaces in both accounts' Security Hub, each naming every participant."""
    # arrange
    entry = make_entry("failed", accounts=[PROD, DEV], error_message="open")

    # act
    groups, skipped = build_account_findings([entry], CONTEXT, None)

    # assert
    compliance = {"Status": "FAILED", "RelatedRequirements": ["SOC2-CC6.6"]}
    expected = (
        [
            ("prod", "111111111111", [expected_finding("failed", compliance, "ACTIVE", "HIGH", PROD, "prod,dev", error_message="open")]),
            ("dev", "222222222222", [expected_finding("failed", compliance, "ACTIVE", "HIGH", DEV, "prod,dev", error_message="open")]),
        ],
        SkippedEntries(unattributed=0, no_standard=0),
    )
    assert expected == ([(group.account_name, group.account_id, [dict(finding) for finding in group.findings]) for group in groups], skipped)


def test_accounts_are_grouped_in_first_seen_order_across_entries() -> None:
    # arrange
    entries = [
        make_entry(accounts=[DEV], nodeid="tests/test_a.py::test_one"),
        make_entry(accounts=[PROD, DEV], nodeid="tests/test_a.py::test_two"),
        make_entry(accounts=[PROD], nodeid="tests/test_a.py::test_three"),
    ]

    # act
    groups, _ = build_account_findings(entries, CONTEXT, None)

    # assert
    expected = [("dev", 2), ("prod", 2)]
    assert expected == [(group.account_name, len(group.findings)) for group in groups]


def test_entry_without_an_account_fixture_is_counted_not_sent() -> None:
    # arrange
    entries = [make_entry(accounts=[]), make_entry(accounts=[], nodeid="tests/test_a.py::test_other"), make_entry()]

    # act
    groups, skipped = build_account_findings(entries, CONTEXT, None)

    # assert
    expected = (["prod"], 1, SkippedEntries(unattributed=2, no_standard=0))
    assert expected == ([group.account_name for group in groups], len(groups[0].findings), skipped)


def test_no_entries_produces_no_groups() -> None:
    # act
    result = build_account_findings([], CONTEXT, None)

    # assert
    expected: tuple[list[AccountFindings], SkippedEntries] = ([], SkippedEntries(unattributed=0, no_standard=0))
    assert expected == result


# --- standards, types and requirements ---


def test_no_standard_uses_the_cats_classifier_and_no_requirements() -> None:
    """With only_with_standard: false, a test carrying no standard is still imported, typed CATS."""
    # arrange
    entry = make_entry("passed", standards=[])

    # act
    finding = only_finding([entry], only_with_standard=False)

    # assert
    expected = expected_finding("passed", compliance={"Status": "PASSED"}, record_state="ARCHIVED", severity="INFORMATIONAL", types=[TYPE_CATS])
    assert expected == finding


def test_related_requirements_are_standards_then_their_resolved_control_ids() -> None:
    """Decorator control ids win, the registry fills gaps, and a standard with neither is its own requirement."""
    # arrange
    entry = make_entry(
        standards=["SOC2-CC6.6", "NIST-800-53", "ISO-27001"],
        standard_metadata={"SOC2-CC6.6": {"control_ids": ["CC6.6", "CC6.7"], "control_group": None}},
    )
    registry = {"NIST-800-53": StandardMetadata(control_ids=["SC-7", "CC6.7"], control_group="Network")}

    # act
    finding = only_finding([entry], registry)

    # assert
    expected = ["SOC2-CC6.6", "CC6.6", "CC6.7", "NIST-800-53", "SC-7", "ISO-27001"]
    assert expected == finding["Compliance"]["RelatedRequirements"]


def test_related_requirements_are_capped_at_the_asff_limit() -> None:
    # arrange
    entry = make_entry(standards=["STD"], standard_metadata={"STD": {"control_ids": [f"c-{i}" for i in range(40)], "control_group": None}})

    # act
    finding = only_finding([entry])

    # assert
    expected = ["STD", *(f"c-{i}" for i in range(31))]
    assert expected == finding["Compliance"]["RelatedRequirements"]


def test_a_slash_in_a_standard_name_becomes_a_dash_in_the_type() -> None:
    """ASFF types are namespace/category/classifier: a fourth segment would be malformed."""
    # arrange
    entry = make_entry(standards=["PCI-DSS/4.0"])

    # act
    finding = only_finding([entry])

    # assert
    expected = ["Software and Configuration Checks/Industry and Regulatory Standards/PCI-DSS-4.0"]
    assert expected == finding["Types"]


def test_types_are_capped_at_the_asff_limit() -> None:
    # arrange
    entry = make_entry(standards=[f"STD-{i}" for i in range(60)])

    # act
    finding = only_finding([entry])

    # assert
    expected = 50
    assert expected == len(finding["Types"])


# --- text fields ---


def test_docstring_whitespace_is_collapsed_in_the_description() -> None:
    # arrange
    entry = make_entry(docstring="  Prove the production network\n    cannot reach development.\n  ")

    # act
    finding = only_finding([entry])

    # assert
    expected = "Prove the production network cannot reach development."
    assert expected == finding["Description"]


@pytest.mark.parametrize("docstring", [None, "", "   \n  "])
def test_missing_or_blank_docstring_falls_back_to_the_test_name(docstring: str | None) -> None:
    # act
    finding = only_finding([make_entry(docstring=docstring)])

    # assert
    expected = "test_prod_cannot_reach_dev"
    assert expected == finding["Description"]


def test_title_description_and_generator_id_are_clipped_to_their_limits() -> None:
    # arrange
    entry = make_entry(test_name="t" * 300, docstring="d" * 1100, nodeid="n" * 600)

    # act
    finding = only_finding([entry])

    # assert
    expected = ("t" * 256, "d" * 1024, "n" * 512)
    assert expected == (finding["Title"], finding["Description"], finding["GeneratorId"])


def test_error_message_is_clipped_to_the_product_field_limit_and_absent_when_none() -> None:
    """The API's Compliance.StatusReasons has a closed ReasonCode list, so the message rides in ProductFields."""
    # arrange
    long_failure = make_entry("failed", error_message="x" * 3000)
    silent_failure = make_entry("failed", error_message=None)

    # act
    clipped = only_finding([long_failure])["ProductFields"]["cats/error_message"]
    silent = only_finding([silent_failure])["ProductFields"]

    # assert
    expected = ("x" * 2048, False, False)
    assert expected == (clipped, "cats/error_message" in silent, "cats/reason" in silent)


# --- product fields and tags ---


def test_markers_and_metadata_land_in_namespaced_product_fields() -> None:
    """cats/marker/* and cats/metadata/* cannot collide with each other or with the fixed cats/* keys."""
    # arrange
    entry = make_entry(
        "passed",
        custom_markers={"team": ["payments", "platform"], "severity": ["p1"]},
        additional_metadata={"regions": ["us-east-1", "eu-west-1"], "resources_checked": 42},
    )

    # act
    finding = only_finding([entry])

    # assert
    expected = expected_finding(
        "passed",
        compliance={"Status": "PASSED", "RelatedRequirements": ["SOC2-CC6.6"]},
        record_state="ARCHIVED",
        severity="INFORMATIONAL",
        product_fields_extra={
            "cats/marker/team": "payments,platform",
            "cats/marker/severity": "p1",
            "cats/metadata/regions": "['us-east-1', 'eu-west-1']",
            "cats/metadata/resources_checked": "42",
        },
    )
    assert expected == finding


def test_product_fields_are_capped_with_the_cats_fields_kept_first() -> None:
    # arrange
    entry = make_entry(additional_metadata={f"k{i:02d}": f"v{i}" for i in range(60)})

    # act
    fields = only_finding([entry])["ProductFields"]

    # assert
    expected = (50, [f"cats/metadata/k{i:02d}" for i in range(43)])
    assert expected == (len(fields), [key for key in fields if key.startswith("cats/metadata/")])


def test_blank_product_field_values_are_dropped() -> None:
    """The API rejects blank keys and values (pattern .*\\S.*); a test with no participants has an empty accounts list."""
    # arrange
    entry = make_entry(additional_metadata={"note": "   ", "kept": "yes"})

    # act
    fields = only_finding([entry])["ProductFields"]

    # assert
    expected = {"cats/metadata/kept": "yes"}
    assert expected == {key: value for key, value in fields.items() if key.startswith("cats/metadata/")}


def test_product_field_values_are_clipped_to_the_asff_limit() -> None:
    # arrange
    entry = make_entry(additional_metadata={"blob": "b" * 3000})

    # act
    fields = only_finding([entry])["ProductFields"]

    # assert
    expected = "b" * 2048
    assert expected == fields["cats/metadata/blob"]


def test_account_without_tags_has_no_tags_key_on_its_resource() -> None:
    # act
    finding = only_finding([make_entry(accounts=[DEV])])

    # assert
    expected = [{"Type": "AwsAccount", "Id": "AWS::::Account:222222222222", "Region": "us-east-1"}]
    assert expected == finding["Resources"]


def test_resource_tag_values_are_clipped_at_the_aws_tag_limit() -> None:
    """Resources[].Tags follow the AWS tag limits (value <= 256), not the ProductFields limits."""
    # arrange
    long_tagged = AccountReportDetail(account_name="prod", account_id="111111111111", tags={"owner": "x" * 300})

    # act
    finding = only_finding([make_entry(accounts=[long_tagged])])

    # assert
    expected = {"owner": "x" * 256}
    assert expected == finding["Resources"][0]["Tags"]


# --- only_with_standard and the deploy Region in the identity ---


def test_entry_without_a_standard_is_counted_not_sent_by_default() -> None:
    """A compliance finding needs a control to attest to; report.json still records the test."""
    # arrange
    entries = [make_entry("passed", standards=[]), make_entry("passed", standards=[], nodeid="tests/test_a.py::test_other"), make_entry()]

    # act
    groups, skipped = build_account_findings(entries, CONTEXT, None)

    # assert
    expected = (["prod"], 1, SkippedEntries(unattributed=0, no_standard=2))
    assert expected == ([group.account_name for group in groups], len(groups[0].findings), skipped)


def test_unattributed_is_counted_before_no_standard() -> None:
    """An entry with neither an account nor a standard has nowhere to go first."""
    # arrange
    entries = [make_entry("passed", accounts=[], standards=[])]

    # act
    _, skipped = build_account_findings(entries, CONTEXT, None)

    # assert
    assert skipped == SkippedEntries(unattributed=1, no_standard=0)


def test_not_run_entry_is_counted_and_produces_no_finding() -> None:
    """A test test_selection held back produced no control state: with an account and a standard, it is still not imported."""
    # arrange
    entries = [make_entry("not_run", reason="RDS not in use; accepted by CISO 2026-09-01"), make_entry()]

    # act
    groups, skipped = build_account_findings(entries, CONTEXT, None)

    # assert
    expected = (["prod"], 1, SkippedEntries(unattributed=0, no_standard=0, not_run=1))
    assert expected == ([group.account_name for group in groups], len(groups[0].findings), skipped)


def test_not_run_is_counted_before_unattributed_and_no_standard() -> None:
    """A held-back test with no account and no standard is counted once, as not run."""
    # arrange
    entries = [make_entry("not_run", accounts=[], standards=[], reason="requires {uc-plus}; not provided {uc-plus}")]

    # act
    _, skipped = build_account_findings(entries, CONTEXT, None, only_with_standard=False)

    # assert
    assert skipped == SkippedEntries(unattributed=0, no_standard=0, not_run=1)


def test_finding_id_carries_the_deploy_region() -> None:
    """Two regional runs of one suite into one aggregated hub must not overwrite each other."""
    # arrange
    other_region = FindingContext(**{**CONTEXT.__dict__, "aws_region": "eu-central-1"})

    # act
    ours, _ = build_account_findings([make_entry()], CONTEXT, None)
    theirs, _ = build_account_findings([make_entry()], other_region, None)

    # assert
    expected = (
        f"cats/CATS-test-infra-prod/us-east-1/{uuid5(CATS_SECURITYHUB_NAMESPACE, NODEID)}",
        f"cats/CATS-test-infra-prod/eu-central-1/{uuid5(CATS_SECURITYHUB_NAMESPACE, NODEID)}",
    )
    assert expected == (ours[0].findings[0]["Id"], theirs[0].findings[0]["Id"])


def test_resource_region_is_the_deploy_region_not_the_hub_region() -> None:
    # act
    finding = only_finding([make_entry()])

    # assert
    expected = ("us-east-1", "arn:aws:securityhub:eu-west-1:111111111111:product/111111111111/default")
    assert expected == (finding["Resources"][0]["Region"], finding["ProductArn"])
