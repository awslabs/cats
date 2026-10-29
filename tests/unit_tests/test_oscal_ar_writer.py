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

"""Tests for cats.report_writers.oscal_ar — OSCAL AR writer logic."""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from cats.exceptions import CatsError
from cats.hookspecs import StandardMetadata
from cats.oscal_models import AssessmentResults, InventoryItem, Observation, State
from cats.report_writers.oscal_ar import (
    OscalArWriter,
    build_assessment_results,
)
from cats.reporting import ReportEntry
from tests.utils import make_run_manifest


def _make_entry(
    result: str = "passed",
    test_name: str = "test_something",
    standards: list[str] | None = None,
    standard_metadata: dict[str, dict[str, Any]] | None = None,
    account_id: str = "111111111111",
    account_name: str = "TestAccount",
    tags: dict[str, str | None] | None = None,
    custom_markers: dict[str, list[str]] | None = None,
    error_message: str | None = None,
    xfail: bool = False,
) -> ReportEntry:
    """Build a ReportEntry for testing."""
    entry: ReportEntry = {
        "result": result,  # type: ignore[typeddict-item]
        "accounts_in_test": [
            {
                "account_name": account_name,
                "account_id": account_id,
                "tags": tags or {},
            }
        ],
        "test_name": test_name,
        "nodeid": f"tests/test_module.py::{test_name}",
        "test_docstring": f"Docstring for {test_name}",
        "standards": standards or [],
        "custom_markers": custom_markers or {},
        "standard_metadata": standard_metadata or {},
        "additional_metadata": {},
        "xfail": xfail,
        "error_message": error_message,
        "reason": None,
        "duration_s": 0.5,
        "recorded_at": "2026-09-24T10:53:17.250000Z",
    }
    return entry


def _inventory_items(ar: AssessmentResults) -> list[InventoryItem]:
    local_defs = ar.results[0].local_definitions
    assert local_defs is not None
    return local_defs.inventory_items


def _obs_prop(observation: Observation, name: str) -> str | None:
    """Return the value of a single prop on an observation, or None if missing."""
    for prop in observation.props:
        if prop.name == name:
            return prop.value
    return None


# ---------------------------------------------------------------------------
# Structure & wiring
# ---------------------------------------------------------------------------


def test_empty_reports_produces_valid_structure() -> None:
    """Empty report list produces a valid AssessmentResults with no observations."""
    # act
    ar = build_assessment_results([], None, make_run_manifest())

    # assert
    assert len(ar.results) == 1
    assert ar.results[0].observations == []
    assert ar.results[0].findings == []


def test_single_entry_creates_observation_and_finding() -> None:
    """One ReportEntry with one standard produces one observation and one finding."""
    # arrange
    entry = _make_entry(standards=["SC-7"])

    # act
    ar = build_assessment_results([entry], None, make_run_manifest())
    result = ar.results[0]

    # assert
    assert len(result.observations) == 1
    assert len(result.findings) == 1


def test_observation_title_is_test_name() -> None:
    """Observation.title is the test name."""
    # arrange
    entry = _make_entry(test_name="test_something", standards=["SC-7"])

    # act
    ar = build_assessment_results([entry], None, make_run_manifest())

    # assert
    expected_title = "test_something"
    assert ar.results[0].observations[0].title == expected_title


def test_finding_target_id_is_standard_name_by_default() -> None:
    """Without decorator metadata or registry, finding target-id is the standard name."""
    # arrange
    entry = _make_entry(standards=["SC-7"])

    # act
    ar = build_assessment_results([entry], None, make_run_manifest())

    # assert
    expected_target = "SC-7"
    assert ar.results[0].findings[0].target.target_id == expected_target


# ---------------------------------------------------------------------------
# Result mapping: pytest outcome → "result" prop and finding state
# ---------------------------------------------------------------------------


def test_passed_maps_to_pass_result_prop() -> None:
    """passed test produces observation prop result=Pass."""
    # arrange
    entry = _make_entry(result="passed", standards=["SC-7"])

    # act
    ar = build_assessment_results([entry], None, make_run_manifest())

    # assert
    expected_value = "Pass"
    assert _obs_prop(ar.results[0].observations[0], "result") == expected_value


def test_xpassed_maps_to_pass_result_prop() -> None:
    """xpassed test produces observation prop result=Pass."""
    # arrange
    entry = _make_entry(result="xpassed", standards=["SC-7"], xfail=True)

    # act
    ar = build_assessment_results([entry], None, make_run_manifest())

    # assert
    expected_value = "Pass"
    assert _obs_prop(ar.results[0].observations[0], "result") == expected_value


def test_failed_maps_to_fail_result_prop() -> None:
    """failed test produces observation prop result=Fail."""
    # arrange
    entry = _make_entry(result="failed", standards=["SC-7"], error_message="boom")

    # act
    ar = build_assessment_results([entry], None, make_run_manifest())

    # assert
    expected_value = "Fail"
    assert _obs_prop(ar.results[0].observations[0], "result") == expected_value


def test_xfailed_maps_to_fail_result_prop() -> None:
    """xfailed test produces observation prop result=Fail."""
    # arrange
    entry = _make_entry(result="xfailed", standards=["SC-7"], xfail=True)

    # act
    ar = build_assessment_results([entry], None, make_run_manifest())

    # assert
    expected_value = "Fail"
    assert _obs_prop(ar.results[0].observations[0], "result") == expected_value


def test_error_maps_to_error_result_prop() -> None:
    """error test produces observation prop result=Error."""
    # arrange
    entry = _make_entry(result="error", standards=["SC-7"])

    # act
    ar = build_assessment_results([entry], None, make_run_manifest())

    # assert
    expected_value = "Error"
    assert _obs_prop(ar.results[0].observations[0], "result") == expected_value


def test_skipped_maps_to_na_result_prop() -> None:
    """skipped test produces observation prop result=N/A."""
    # arrange
    entry = _make_entry(result="skipped", standards=["SC-7"])

    # act
    ar = build_assessment_results([entry], None, make_run_manifest())

    # assert
    expected_value = "N/A"
    assert _obs_prop(ar.results[0].observations[0], "result") == expected_value


def test_passed_finding_state_is_satisfied() -> None:
    """Passed test maps to OSCAL satisfied state."""
    # arrange
    entry = _make_entry(result="passed", standards=["SC-7"])

    # act
    ar = build_assessment_results([entry], None, make_run_manifest())

    # assert
    expected_state = State.SATISFIED
    assert ar.results[0].findings[0].target.status.state == expected_state


def test_failed_finding_state_is_not_satisfied() -> None:
    """Failed test maps to OSCAL not-satisfied state."""
    # arrange
    entry = _make_entry(result="failed", standards=["SC-7"], error_message="assert False")

    # act
    ar = build_assessment_results([entry], None, make_run_manifest())

    # assert
    expected_state = State.NOT_SATISFIED
    assert ar.results[0].findings[0].target.status.state == expected_state


def test_error_message_becomes_finding_status_remarks() -> None:
    """Error message from a failed test appears as remarks on the finding status."""
    # arrange
    entry = _make_entry(result="failed", standards=["SC-7"], error_message="assert 1 == 2")

    # act
    ar = build_assessment_results([entry], None, make_run_manifest())

    # assert
    expected_remarks = "assert 1 == 2"
    assert ar.results[0].findings[0].target.status.remarks == expected_remarks


def test_passed_finding_has_no_status_remarks() -> None:
    """Passed test has no remarks on the finding status."""
    # arrange
    entry = _make_entry(result="passed", standards=["SC-7"])

    # act
    ar = build_assessment_results([entry], None, make_run_manifest())

    # assert
    assert ar.results[0].findings[0].target.status.remarks is None


# ---------------------------------------------------------------------------
# Control group — decorator wins, registry fills, omit when None
# ---------------------------------------------------------------------------


def test_decorator_control_group_becomes_prop() -> None:
    """control_group from the decorator metadata appears as the control-group prop."""
    # arrange
    entry = _make_entry(
        standards=["ACME-G-DCP-1"],
        standard_metadata={"ACME-G-DCP-1": {"control_ids": [], "control_group": "Governance"}},
    )

    # act
    ar = build_assessment_results([entry], None, make_run_manifest())

    # assert
    expected_value = "Governance"
    assert _obs_prop(ar.results[0].observations[0], "control-group") == expected_value


def test_registry_control_group_used_when_decorator_absent() -> None:
    """With no decorator group, the registry's control_group populates the prop."""
    # arrange
    entry = _make_entry(standards=["ACME-G-DCP-1"])
    registry: Mapping[str, StandardMetadata] = {
        "ACME-G-DCP-1": StandardMetadata(control_ids=[], control_group="Governance"),
    }

    # act
    ar = build_assessment_results([entry], registry, make_run_manifest())

    # assert
    expected_value = "Governance"
    assert _obs_prop(ar.results[0].observations[0], "control-group") == expected_value


def test_decorator_control_group_overrides_registry() -> None:
    """Decorator control_group wins over registry control_group."""
    # arrange
    entry = _make_entry(
        standards=["ACME-G-DCP-1"],
        standard_metadata={"ACME-G-DCP-1": {"control_ids": [], "control_group": "Security"}},
    )
    registry: Mapping[str, StandardMetadata] = {
        "ACME-G-DCP-1": StandardMetadata(control_ids=[], control_group="Governance"),
    }

    # act
    ar = build_assessment_results([entry], registry, make_run_manifest())

    # assert
    expected_value = "Security"
    assert _obs_prop(ar.results[0].observations[0], "control-group") == expected_value


def test_no_group_anywhere_omits_prop() -> None:
    """With neither decorator nor registry group, no control-group prop is emitted."""
    # arrange
    entry = _make_entry(standards=["SC-7"])

    # act
    ar = build_assessment_results([entry], None, make_run_manifest())
    prop_names = [p.name for p in ar.results[0].observations[0].props]

    # assert
    assert "control-group" not in prop_names


# ---------------------------------------------------------------------------
# Control IDs — decorator wins, registry fills, fallback to standard name
# ---------------------------------------------------------------------------


def test_decorator_control_ids_drive_findings() -> None:
    """Decorator control_ids become the target-id of each emitted finding."""
    # arrange
    entry = _make_entry(
        standards=["ACME-G-DCP-1"],
        standard_metadata={"ACME-G-DCP-1": {"control_ids": ["SC-7", "AC-3"], "control_group": None}},
    )

    # act
    ar = build_assessment_results([entry], None, make_run_manifest())
    target_ids = sorted(f.target.target_id for f in ar.results[0].findings)

    # assert
    expected_target_ids = ["AC-3", "SC-7"]
    assert target_ids == expected_target_ids


def test_registry_control_ids_used_when_decorator_absent() -> None:
    """With no decorator control_ids, the registry's control_ids drive findings."""
    # arrange
    entry = _make_entry(standards=["ACME-G-DCP-1"])
    registry: Mapping[str, StandardMetadata] = {
        "ACME-G-DCP-1": StandardMetadata(control_ids=["SC-7", "AC-3"]),
    }

    # act
    ar = build_assessment_results([entry], registry, make_run_manifest())
    target_ids = sorted(f.target.target_id for f in ar.results[0].findings)

    # assert
    expected_target_ids = ["AC-3", "SC-7"]
    assert target_ids == expected_target_ids


def test_decorator_control_ids_override_registry() -> None:
    """Decorator control_ids override registry control_ids entirely (no merge)."""
    # arrange
    entry = _make_entry(
        standards=["ACME-G-DCP-1"],
        standard_metadata={"ACME-G-DCP-1": {"control_ids": ["SC-7"], "control_group": None}},
    )
    registry: Mapping[str, StandardMetadata] = {
        "ACME-G-DCP-1": StandardMetadata(control_ids=["AC-3", "AC-4"]),
    }

    # act
    ar = build_assessment_results([entry], registry, make_run_manifest())
    target_ids = sorted(f.target.target_id for f in ar.results[0].findings)

    # assert
    expected_target_ids = ["SC-7"]
    assert target_ids == expected_target_ids


def test_no_control_ids_falls_back_to_standard_name() -> None:
    """With neither decorator nor registry IDs, the standard name itself becomes the target-id."""
    # arrange
    entry = _make_entry(standards=["ACME-G-DCP-1"])

    # act
    ar = build_assessment_results([entry], None, make_run_manifest())
    target_ids = [f.target.target_id for f in ar.results[0].findings]

    # assert
    expected_target_ids = ["ACME-G-DCP-1"]
    assert target_ids == expected_target_ids


# ---------------------------------------------------------------------------
# Observation remarks carry the resolved control IDs for free-text scanning
# ---------------------------------------------------------------------------


def test_remarks_contains_control_ids_for_nist_regex() -> None:
    """Observation remarks is the resolved control IDs space-joined, so OSCAL consumers can extract them."""
    # arrange
    entry = _make_entry(
        standards=["ACME-G-DCP-1"],
        standard_metadata={"ACME-G-DCP-1": {"control_ids": ["AC-3", "SC-7", "sc-7.5"], "control_group": None}},
    )

    # act
    ar = build_assessment_results([entry], None, make_run_manifest())

    # assert
    expected_remarks = "AC-3 SC-7 sc-7.5"
    assert ar.results[0].observations[0].remarks == expected_remarks


def test_no_control_ids_resolved_produces_no_remarks() -> None:
    """When an entry has no standards at all, the observation has no remarks."""
    # arrange
    entry = _make_entry(standards=[])

    # act
    ar = build_assessment_results([entry], None, make_run_manifest())

    # assert
    assert ar.results[0].observations[0].remarks is None


# ---------------------------------------------------------------------------
# Datetime serialization uses RFC 3339 (T separator, not space)
# ---------------------------------------------------------------------------


def test_last_modified_serialised_with_t_separator(tmp_path: Path) -> None:
    """Metadata.last-modified is serialized with a 'T' separator, not a space."""
    # arrange
    entry = _make_entry(standards=["SC-7"])
    writer = OscalArWriter()

    # act
    writer.write([entry], tmp_path / "assessment-results.json", make_run_manifest(), None)
    parsed = json.loads((tmp_path / "assessment-results.json").read_text())
    last_modified = parsed["assessment-results"]["metadata"]["last-modified"]

    # assert
    assert "T" in last_modified
    assert " " not in last_modified


def test_collected_serialised_with_t_separator(tmp_path: Path) -> None:
    """Observation.collected is serialized with a 'T' separator, not a space."""
    # arrange
    entry = _make_entry(standards=["SC-7"])
    writer = OscalArWriter()

    # act
    writer.write([entry], tmp_path / "assessment-results.json", make_run_manifest(), None)
    parsed = json.loads((tmp_path / "assessment-results.json").read_text())
    collected = parsed["assessment-results"]["results"][0]["observations"][0]["collected"]

    # assert
    assert "T" in collected
    assert " " not in collected


def test_start_serialised_with_t_separator(tmp_path: Path) -> None:
    """Result.start is serialized with a 'T' separator, not a space."""
    # arrange
    entry = _make_entry(standards=["SC-7"])
    writer = OscalArWriter()

    # act
    writer.write([entry], tmp_path / "assessment-results.json", make_run_manifest(), None)
    parsed = json.loads((tmp_path / "assessment-results.json").read_text())
    start = parsed["assessment-results"]["results"][0]["start"]

    # assert
    assert "T" in start
    assert " " not in start


# ---------------------------------------------------------------------------
# cats-standard prop kept alongside new observation props
# ---------------------------------------------------------------------------


def test_cats_standard_prop_kept() -> None:
    """Existing cats-standard prop still emitted alongside new observation props."""
    # arrange
    entry = _make_entry(standards=["SC-7"])

    # act
    ar = build_assessment_results([entry], None, make_run_manifest())

    # assert
    expected_value = "SC-7"
    assert _obs_prop(ar.results[0].observations[0], "cats-standard") == expected_value


def test_multiple_standards_produce_multiple_cats_standard_props() -> None:
    """One cats-standard prop per standard is emitted on the observation."""
    # arrange
    entry = _make_entry(standards=["SC-7", "AC-3"])

    # act
    ar = build_assessment_results([entry], None, make_run_manifest())
    cats_standard_values = sorted(p.value for p in ar.results[0].observations[0].props if p.name == "cats-standard")

    # assert
    expected_values = ["AC-3", "SC-7"]
    assert cats_standard_values == expected_values


# ---------------------------------------------------------------------------
# Inventory items, subjects, and structural invariants
# ---------------------------------------------------------------------------


def test_inventory_items_deduplicated_by_account_id() -> None:
    """Same account_id across entries produces one inventory item."""
    # arrange
    entry1 = _make_entry(test_name="test_a", standards=["SC-7"])
    entry2 = _make_entry(test_name="test_b", standards=["CM-8"])

    # act
    ar = build_assessment_results([entry1, entry2], None, make_run_manifest())

    # assert
    expected_count = 1
    assert len(_inventory_items(ar)) == expected_count


def test_different_accounts_create_separate_inventory_items() -> None:
    """Different account_ids produce separate inventory items."""
    # arrange
    entry1 = _make_entry(account_id="111111111111", account_name="Alpha", standards=["SC-7"])
    entry2 = _make_entry(account_id="222222222222", account_name="Beta", standards=["SC-7"])

    # act
    ar = build_assessment_results([entry1, entry2], None, make_run_manifest())

    # assert
    expected_count = 2
    assert len(_inventory_items(ar)) == expected_count


def test_account_tags_become_inventory_item_props() -> None:
    """Account tags are serialized as props on the inventory item."""
    # arrange
    entry = _make_entry(tags={"infrastructure": None, "route": "private"}, standards=["SC-7"])

    # act
    ar = build_assessment_results([entry], None, make_run_manifest())
    item = _inventory_items(ar)[0]
    prop_dict = {p.name: p.value for p in item.props}

    # assert
    expected_props = {"account-id": "111111111111", "infrastructure": "true", "route": "private"}
    assert prop_dict == expected_props


def test_observation_subjects_reference_inventory_items() -> None:
    """Observation subjects reference the correct inventory item UUIDs."""
    # arrange
    entry = _make_entry(standards=["SC-7"])

    # act
    ar = build_assessment_results([entry], None, make_run_manifest())
    item_uuid = _inventory_items(ar)[0].uuid
    subject_uuid = ar.results[0].observations[0].subjects[0].subject_uuid

    # assert
    assert subject_uuid == item_uuid


def test_finding_references_its_observation() -> None:
    """Finding's related-observations references the correct observation UUID."""
    # arrange
    entry = _make_entry(standards=["SC-7"])

    # act
    ar = build_assessment_results([entry], None, make_run_manifest())
    obs_uuid = ar.results[0].observations[0].uuid
    finding_obs_ref = ar.results[0].findings[0].related_observations[0].observation_uuid

    # assert
    assert finding_obs_ref == obs_uuid


def test_reviewed_controls_contains_all_resolved_control_ids() -> None:
    """reviewed-controls aggregates every resolved control ID from every entry."""
    # arrange
    entry1 = _make_entry(
        standards=["ACME-G-DCP-1"],
        standard_metadata={"ACME-G-DCP-1": {"control_ids": ["SC-7", "AC-3"], "control_group": None}},
    )
    entry2 = _make_entry(
        standards=["ACME-G-DCP-2"],
        standard_metadata={"ACME-G-DCP-2": {"control_ids": ["CM-8"], "control_group": None}},
    )

    # act
    ar = build_assessment_results([entry1, entry2], None, make_run_manifest())
    control_ids = {c.control_id for c in ar.results[0].reviewed_controls.control_selections[0].include_controls}

    # assert
    expected_ids = {"AC-3", "CM-8", "SC-7"}
    assert control_ids == expected_ids


def test_entry_with_no_standards_creates_no_findings() -> None:
    """Entry with empty standards list creates an observation but no findings."""
    # arrange
    entry = _make_entry(standards=[])

    # act
    ar = build_assessment_results([entry], None, make_run_manifest())

    # assert
    assert len(ar.results[0].observations) == 1
    assert len(ar.results[0].findings) == 0


def test_xfail_true_adds_observation_prop() -> None:
    """xfail=True adds an xfail prop to the observation."""
    # arrange
    entry = _make_entry(standards=["SC-7"], xfail=True, result="xfailed")

    # act
    ar = build_assessment_results([entry], None, make_run_manifest())

    # assert
    expected_value = "true"
    assert _obs_prop(ar.results[0].observations[0], "xfail") == expected_value


def test_custom_markers_become_observation_props() -> None:
    """Custom markers appear as props on the observation."""
    # arrange
    entry = _make_entry(standards=["SC-7"], custom_markers={"test_suite": ["vpc"], "test_id": ["EW-2"]})

    # act
    ar = build_assessment_results([entry], None, make_run_manifest())

    # assert
    assert _obs_prop(ar.results[0].observations[0], "test-suite") == "vpc"
    assert _obs_prop(ar.results[0].observations[0], "test-id") == "EW-2"


# ---------------------------------------------------------------------------
# Writer output
# ---------------------------------------------------------------------------


def test_writer_creates_file(tmp_path: Path) -> None:
    """OscalArWriter writes to the path it is given."""
    # arrange
    entry = _make_entry(standards=["SC-7"])
    writer = OscalArWriter()
    output_path = tmp_path / "assessment-results.json"

    # act
    writer.write([entry], output_path, make_run_manifest(), None)

    # assert
    assert output_path.exists() is True


def test_writer_output_has_correct_wrapper_key(tmp_path: Path) -> None:
    """Written JSON has top-level assessment-results key."""
    # arrange
    entry = _make_entry(standards=["SC-7"])
    writer = OscalArWriter()

    # act
    writer.write([entry], tmp_path / "assessment-results.json", make_run_manifest(), None)
    parsed = json.loads((tmp_path / "assessment-results.json").read_text())

    # assert
    assert "assessment-results" in parsed


def test_writer_accepts_registry(tmp_path: Path) -> None:
    """OscalArWriter accepts a standards_registry mapping and uses it."""
    # arrange
    entry = _make_entry(standards=["ACME-G-DCP-1"])
    writer = OscalArWriter()
    registry: Mapping[str, StandardMetadata] = {
        "ACME-G-DCP-1": StandardMetadata(control_ids=["SC-7"], control_group="Governance"),
    }

    # act
    writer.write([entry], tmp_path / "assessment-results.json", make_run_manifest(), registry)
    parsed = json.loads((tmp_path / "assessment-results.json").read_text())
    findings = parsed["assessment-results"]["results"][0]["findings"]
    target_ids = [f["target"]["target-id"] for f in findings]

    # assert
    expected_target_ids = ["SC-7"]
    assert target_ids == expected_target_ids


# ---------------------------------------------------------------------------
# End-to-end realistic scenario: two entries, registry, decorator override
# ---------------------------------------------------------------------------


def test_realistic_two_entries_with_registry_and_decorator(tmp_path: Path) -> None:
    """Full scenario: two tests, one registry-resolved, one decorator-overridden."""
    # arrange
    entry_from_registry = _make_entry(
        test_name="test_workbook_driven",
        result="passed",
        standards=["ACME-G-DCP-1"],
    )
    entry_from_decorator = _make_entry(
        test_name="test_decorator_driven",
        result="failed",
        standards=["ACME-G-IAM-2"],
        standard_metadata={
            "ACME-G-IAM-2": {
                "control_ids": ["AC-2", "AC-3"],
                "control_group": "Identity",
            }
        },
        error_message="expected principal not found",
    )
    registry: Mapping[str, StandardMetadata] = {
        "ACME-G-DCP-1": StandardMetadata(control_ids=["SC-7", "AC-3"], control_group="Governance"),
        "ACME-G-IAM-2": StandardMetadata(control_ids=["AC-9"], control_group="Compliance"),
    }
    writer = OscalArWriter()

    # act
    writer.write([entry_from_registry, entry_from_decorator], tmp_path / "assessment-results.json", make_run_manifest(), registry)
    parsed = json.loads((tmp_path / "assessment-results.json").read_text())
    ar = parsed["assessment-results"]
    result = ar["results"][0]

    # assert — overall structure
    observations_by_title = {o["title"]: o for o in result["observations"]}
    findings_by_target = {(f["related-observations"][0]["observation-uuid"], f["target"]["target-id"]): f for f in result["findings"]}

    # registry-resolved observation
    workbook_obs = observations_by_title["test_workbook_driven"]
    workbook_props = {p["name"]: p["value"] for p in workbook_obs["props"]}
    assert workbook_props["result"] == "Pass"
    assert workbook_props["control-group"] == "Governance"
    assert workbook_obs["remarks"] == "SC-7 AC-3"

    # decorator-overridden observation
    decorator_obs = observations_by_title["test_decorator_driven"]
    decorator_props = {p["name"]: p["value"] for p in decorator_obs["props"]}
    assert decorator_props["result"] == "Fail"
    assert decorator_props["control-group"] == "Identity"
    assert decorator_obs["remarks"] == "AC-2 AC-3"

    # reviewed controls collects union of resolved IDs (not the unresolved registry default)
    reviewed_ids = sorted(c["control-id"] for c in result["reviewed-controls"]["control-selections"][0]["include-controls"])
    expected_reviewed = ["AC-2", "AC-3", "SC-7"]
    assert reviewed_ids == expected_reviewed

    # finding for decorator test carries the error_message as status remarks
    decorator_finding = findings_by_target[(decorator_obs["uuid"], "AC-2")]
    expected_finding_remarks = "expected principal not found"
    assert decorator_finding["target"]["status"]["remarks"] == expected_finding_remarks


# ---------------------------------------------------------------------------
# Prop names must be OSCAL tokens: colons and leading digits get sanitized
# ---------------------------------------------------------------------------


def test_tag_key_with_colons_becomes_token_prop_name() -> None:
    """aws:cloudformation:stack-name style tag keys are not legal OSCAL prop names."""
    # arrange
    entry = _make_entry(tags={"aws:cloudformation:stack-name": "CATS-prod"})

    # act
    ar = build_assessment_results([entry], None, make_run_manifest())

    # assert
    inventory = _inventory_items(ar)[0]
    prop_names = [prop.name for prop in inventory.props]
    expected = ["account-id", "aws-cloudformation-stack-name"]
    assert prop_names == expected


def test_metadata_key_starting_with_digit_gets_prefixed() -> None:
    # arrange
    entry = _make_entry()
    entry["additional_metadata"] = {"1st-check": "done"}

    # act
    ar = build_assessment_results([entry], None, make_run_manifest())

    # assert
    assert _obs_prop(ar.results[0].observations[0], "cats-1st-check") == "done"


def test_invalid_registry_control_id_fails_loudly() -> None:
    """A registry-supplied 'AC-2(1)' must raise with the culprit named, not emit invalid OSCAL."""
    # arrange
    entry = _make_entry(standards=["SOC2-CC6.6"])
    registry = {"SOC2-CC6.6": StandardMetadata(control_ids=["AC-2(1)"])}

    # act / assert
    with pytest.raises(CatsError, match=r"control id 'AC-2\(1\)' \(standard 'SOC2-CC6.6' on test 'test_something'\) is not a valid OSCAL token"):
        build_assessment_results([entry], registry, make_run_manifest())


def test_fallback_standard_name_with_space_fails_loudly() -> None:
    """With no control ids anywhere, the standard name becomes the target-id and must be a token."""
    # arrange
    entry = _make_entry(standards=["CIS 1.4"])

    # act / assert
    with pytest.raises(CatsError, match=r"control id 'CIS 1\.4'"):
        build_assessment_results([entry], None, make_run_manifest())


def test_result_window_is_the_runs_not_the_write_time() -> None:
    """result.start/end come from the session's started_at and finished_at; the write time is neither."""
    # arrange
    run = make_run_manifest()

    # act
    ar = build_assessment_results([_make_entry()], None, run)
    dumped = ar.results[0].model_dump(by_alias=True)

    # assert
    expected = ("2026-09-24T10:53:17.000000Z", "2026-09-24T10:53:19.000000Z")
    assert (dumped["start"], dumped["end"]) == expected


def test_observation_collected_is_the_entrys_recorded_at() -> None:
    # act
    ar = build_assessment_results([_make_entry()], None, make_run_manifest())
    dumped = ar.results[0].observations[0].model_dump(by_alias=True)

    # assert
    assert dumped["collected"] == "2026-09-24T10:53:17.250000Z"


# ---------------------------------------------------------------------------
# not_run: an observation that states the fact and the reason, and no finding
# ---------------------------------------------------------------------------


def _not_run_entry(reason: str, standards: list[str] | None = None, control_ids: list[str] | None = None) -> ReportEntry:
    entry = _make_entry(result="not_run", test_name="test_held_back", standards=standards or ["ACME-G-SCP-34"])
    entry["reason"] = reason
    if control_ids is not None:
        entry["standard_metadata"] = {standard: {"control_ids": control_ids, "control_group": None} for standard in entry["standards"]}
    return entry


def test_not_run_entry_is_an_observation_with_the_reason_as_remarks_and_no_finding() -> None:
    """A finding would claim a control state; none was assessed, so the test is observed, with the reason, and that is all."""
    # arrange
    entry = _not_run_entry("RDS not in use; accepted by CISO 2026-09-01")

    # act
    result = build_assessment_results([entry], None, make_run_manifest()).results[0]
    observation = result.observations[0]

    # assert
    expected: tuple[int, str, str, list[str], str, list[object]] = (
        1,
        "test_held_back",
        "Not run",
        ["ACME-G-SCP-34"],
        "RDS not in use; accepted by CISO 2026-09-01",
        [],
    )
    assert expected == (
        len(result.observations),
        observation.title,
        _obs_prop(observation, "result"),
        [prop.value for prop in observation.props if prop.name == "cats-standard"],
        observation.remarks,
        result.findings,
    )


def test_not_run_controls_are_not_reviewed_controls() -> None:
    """The control a held-back test attests to was not reviewed, so it is not in the selection; a run with none selects include-all."""
    # arrange
    held_back = _not_run_entry("requires {uc-plus}; not provided {uc-plus}", standards=["ACME-UC-1"], control_ids=["SC-7"])
    ran = _make_entry(standards=["AC-3"])

    # act
    result = build_assessment_results([held_back, ran], None, make_run_manifest()).results[0]

    # assert
    expected = ([["AC-3"]], ["AC-3"])
    assert expected == (
        [[control.control_id for control in selection.include_controls] for selection in result.reviewed_controls.control_selections],
        [finding.target.target_id for finding in result.findings],
    )


def test_not_run_control_ids_are_not_checked_as_oscal_tokens() -> None:
    """No finding is produced, so a control id the OSCAL grammar rejects does not fail a held-back test."""
    # arrange
    entry = _not_run_entry("the reason", control_ids=["AC-2(1)"])

    # act
    result = build_assessment_results([entry], None, make_run_manifest()).results[0]

    # assert
    assert (len(result.observations), result.findings) == (1, [])


def test_not_run_observation_still_names_its_accounts() -> None:
    # arrange
    entry = _not_run_entry("the reason")

    # act
    ar = build_assessment_results([entry], None, make_run_manifest())

    # assert
    expected = [_inventory_items(ar)[0].uuid]
    assert expected == [subject.subject_uuid for subject in ar.results[0].observations[0].subjects]
