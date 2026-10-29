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

"""Tests for cats.oscal_models — OSCAL pydantic model serialization."""

import json
from datetime import UTC, datetime

from cats.oscal_models import (
    AssessmentResults,
    ControlSelection,
    Finding,
    FindingTarget,
    InventoryItem,
    LocalDefinitions,
    Metadata,
    ObjectiveStatus,
    Observation,
    Property,
    RelatedObservation,
    Result,
    ReviewedControls,
    SelectControlById,
    State,
    SubjectReference,
)


def test_property_serializes_as_plain_object() -> None:
    """Property model serializes with name and value keys."""
    # arrange
    prop = Property(name="account-id", value="111111111111")

    # act
    result = prop.model_dump(by_alias=True)

    # assert
    expected = {"name": "account-id", "value": "111111111111"}
    assert result == expected


def test_subject_reference_uses_kebab_case() -> None:
    """SubjectReference serializes subject_uuid as subject-uuid."""
    # arrange
    ref = SubjectReference(subject_uuid="abc-123")

    # act
    result = ref.model_dump(by_alias=True)

    # assert
    expected = {"subject-uuid": "abc-123", "type": "inventory-item"}
    assert result == expected


def test_inventory_item_includes_props() -> None:
    """InventoryItem serializes with uuid, description, and props."""
    # arrange
    item = InventoryItem(
        uuid="fixed-uuid",
        description="TestAccount (111111111111)",
        props=[Property(name="account-id", value="111111111111")],
    )

    # act
    result = item.model_dump(by_alias=True)

    # assert
    expected = {
        "uuid": "fixed-uuid",
        "description": "TestAccount (111111111111)",
        "props": [{"name": "account-id", "value": "111111111111"}],
    }
    assert result == expected


def test_observation_methods_default_to_test() -> None:
    """Observation.methods defaults to ["TEST"]."""
    # arrange
    obs = Observation(
        uuid="obs-uuid",
        title="test_something",
        description="A test",
        collected=datetime(2026, 1, 1, tzinfo=UTC),
    )

    # act
    result = obs.model_dump(by_alias=True)

    # assert
    assert result["methods"] == ["TEST"]
    assert result["title"] == "test_something"


def test_finding_target_uses_kebab_case_keys() -> None:
    """FindingTarget serializes target-id and nested status correctly."""
    # arrange
    target = FindingTarget(
        target_id="sc-7",
        status=ObjectiveStatus(state=State.SATISFIED),
    )

    # act
    result = target.model_dump(by_alias=True, exclude_none=True)

    # assert
    expected = {
        "type": "objective-id",
        "target-id": "sc-7",
        "status": {"state": "satisfied"},
    }
    assert result == expected


def test_finding_target_includes_remarks_when_set() -> None:
    """ObjectiveStatus includes remarks when provided."""
    # arrange
    target = FindingTarget(
        target_id="sc-7",
        status=ObjectiveStatus(state=State.NOT_SATISFIED, remarks="assertion failed"),
    )

    # act
    result = target.model_dump(by_alias=True, exclude_none=True)

    # assert
    expected_remarks = "assertion failed"
    assert result["status"]["remarks"] == expected_remarks


def test_finding_related_observations_kebab_case() -> None:
    """Finding serializes related-observations with observation-uuid."""
    # arrange
    finding = Finding(
        uuid="f-uuid",
        title="sc-7 assessment",
        description="Assessed by test_x",
        target=FindingTarget(target_id="sc-7", status=ObjectiveStatus(state=State.SATISFIED)),
        related_observations=[RelatedObservation(observation_uuid="obs-uuid")],
    )

    # act
    result = finding.model_dump(by_alias=True, exclude_none=True)

    # assert
    expected_related = [{"observation-uuid": "obs-uuid"}]
    assert result["related-observations"] == expected_related


def test_reviewed_controls_structure() -> None:
    """ReviewedControls serializes with control-selections and include-controls."""
    # arrange
    reviewed = ReviewedControls(control_selections=[ControlSelection(include_controls=[SelectControlById(control_id="sc-7")])])
    # act
    result = reviewed.model_dump(by_alias=True)

    # assert
    expected = {"control-selections": [{"include-controls": [{"control-id": "sc-7"}], "include-all": None}]}
    assert result == expected


def test_metadata_defaults() -> None:
    """Metadata has correct defaults for title and oscal-version."""
    # arrange
    meta = Metadata()

    # act
    result = meta.model_dump(by_alias=True)

    # assert
    assert result["title"] == "CATS Assessment Results"
    assert result["oscal-version"] == "1.2.1"
    # `version` is the *document* version: a fresh per-run timestamp identifier.
    datetime.fromisoformat(result["version"])


def test_full_assessment_results_wrapper_structure() -> None:
    """AssessmentResults wraps into the correct top-level OSCAL structure."""
    # arrange
    ar = AssessmentResults(
        uuid="ar-uuid",
        metadata=Metadata(),
        results=[
            Result(
                uuid="r-uuid",
                start=datetime(2026, 1, 1, tzinfo=UTC),
                local_definitions=LocalDefinitions(),
                reviewed_controls=ReviewedControls(control_selections=[]),
            )
        ],
    )

    # act
    wrapper = {"assessment-results": ar.model_dump(by_alias=True, exclude_none=True)}
    output = json.dumps(wrapper, default=str)
    parsed = json.loads(output)

    # assert
    assert "assessment-results" in parsed
    assert parsed["assessment-results"]["uuid"] == "ar-uuid"
    assert parsed["assessment-results"]["import-ap"]["href"] == ""
    assert len(parsed["assessment-results"]["results"]) == 1


def test_state_enum_values() -> None:
    """State enum has the correct OSCAL string values."""
    # assert
    assert State.SATISFIED.value == "satisfied"
    assert State.NOT_SATISFIED.value == "not-satisfied"


def test_observation_collected_serializes_as_rfc3339_utc_z() -> None:
    """Observation.collected serializes as RFC 3339 with a 'T' separator (not a space)."""
    # arrange
    obs = Observation(
        uuid="obs-uuid",
        title="test_thing",
        description="A test",
        collected=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
    )

    # act
    result = obs.model_dump(by_alias=True, exclude_none=True)

    # assert
    expected_collected = "2026-01-02T03:04:05.000000Z"
    assert result["collected"] == expected_collected


def test_result_start_serializes_as_rfc3339_utc_z() -> None:
    """Result.start serializes as RFC 3339 with a 'T' separator."""
    # arrange
    result_model = Result(
        uuid="r-uuid",
        start=datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC),
        local_definitions=LocalDefinitions(),
        reviewed_controls=ReviewedControls(control_selections=[]),
    )

    # act
    dumped = result_model.model_dump(by_alias=True, exclude_none=True)

    # assert
    expected_start = "2026-03-04T05:06:07.000000Z"
    assert dumped["start"] == expected_start


def test_metadata_last_modified_serializes_as_rfc3339_utc_z() -> None:
    """Metadata.last-modified serializes as RFC 3339 with a 'T' separator."""
    # arrange
    meta = Metadata(last_modified=datetime(2026, 5, 6, 7, 8, 9, tzinfo=UTC))

    # act
    dumped = meta.model_dump(by_alias=True, exclude_none=True)

    # assert
    expected_last_modified = "2026-05-06T07:08:09.000000Z"
    assert dumped["last-modified"] == expected_last_modified


def test_observation_remarks_optional_defaults_to_none() -> None:
    """Observation.remarks is None by default and excluded from the dump."""
    # arrange
    obs = Observation(title="test_thing", description="A test")

    # act
    dumped = obs.model_dump(by_alias=True, exclude_none=True)

    # assert
    assert "remarks" not in dumped


def test_observation_remarks_renders_when_set() -> None:
    """Observation.remarks renders as a plain string when populated."""
    # arrange
    obs = Observation(title="test_thing", description="A test", remarks="SC-7 AC-3")

    # act
    dumped = obs.model_dump(by_alias=True, exclude_none=True)

    # assert
    expected_remarks = "SC-7 AC-3"
    assert dumped["remarks"] == expected_remarks
