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

"""Minimal OSCAL Assessment Results models for CATS output.

Pydantic v2 models covering the ~15 types needed to emit valid
OSCAL assessment-results JSON. No trestle dependency.

Target OSCAL version: 1.2.1 (what compliance-trestle 5.x targets).
The vendored schema in tests/unit_tests/schemas/ pins output validity.
"""

from datetime import UTC, datetime
from enum import StrEnum
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from cats.report_run import rfc3339_timestamp

OSCAL_VERSION = "1.2.1"

# Namespace for deterministic (uuid5) observation/inventory/finding UUIDs, so two
# runs over the same tests produce diffable documents. Document and result UUIDs
# stay uuid4: OSCAL expects a fresh instance identity per run.
_CATS_OSCAL_NAMESPACE = uuid5(NAMESPACE_URL, "https://github.com/awslabs/cats/oscal")


def stable_uuid(*parts: str) -> str:
    """Deterministic UUID from the given parts (e.g. nodeid, account id, control id)."""
    return str(uuid5(_CATS_OSCAL_NAMESPACE, "|".join(parts)))


def _uuid() -> str:
    return str(uuid4())


def _now() -> datetime:
    return datetime.now(tz=UTC)


class OscalBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)


class Property(OscalBase):
    name: str
    value: str


class SubjectReference(OscalBase):
    subject_uuid: str = Field(alias="subject-uuid")
    type: str = "inventory-item"


class InventoryItem(OscalBase):
    uuid: str = Field(default_factory=_uuid)
    description: str
    props: list[Property] = []


class Observation(OscalBase):
    uuid: str = Field(default_factory=_uuid)
    title: str
    description: str
    props: list[Property] = []
    methods: list[str] = Field(default_factory=lambda: ["TEST"])
    subjects: list[SubjectReference] = []
    collected: datetime = Field(default_factory=_now)
    remarks: str | None = None

    @field_serializer("collected")
    def _serialize_collected(self, value: datetime) -> str:
        return rfc3339_timestamp(value)


class State(StrEnum):
    SATISFIED = "satisfied"
    NOT_SATISFIED = "not-satisfied"


class StatusReason(StrEnum):
    """Schema-suggested reasons for an objective status (free tokens are also legal)."""

    PASS = "pass"  # noqa: S105 - OSCAL status reason token, not a password
    FAIL = "fail"
    OTHER = "other"


class ObjectiveStatus(OscalBase):
    state: State
    reason: StatusReason | None = None
    remarks: str | None = None


class FindingTarget(OscalBase):
    type: str = "objective-id"
    target_id: str = Field(alias="target-id")
    status: ObjectiveStatus


class RelatedObservation(OscalBase):
    observation_uuid: str = Field(alias="observation-uuid")


class Finding(OscalBase):
    uuid: str = Field(default_factory=_uuid)
    title: str
    description: str
    target: FindingTarget
    related_observations: list[RelatedObservation] = Field(default=[], alias="related-observations")


class SelectControlById(OscalBase):
    control_id: str = Field(alias="control-id")


class ControlSelection(OscalBase):
    include_controls: list[SelectControlById] = Field(default=[], alias="include-controls")
    # {} means "all controls": the schema-legal shape for a run that assessed no
    # specific control ids (include-controls forbids an empty array).
    include_all: dict[str, str] | None = Field(default=None, alias="include-all")


class ReviewedControls(OscalBase):
    control_selections: list[ControlSelection] = Field(alias="control-selections")


class LocalDefinitions(OscalBase):
    inventory_items: list[InventoryItem] = Field(default=[], alias="inventory-items")


class Result(OscalBase):
    uuid: str = Field(default_factory=_uuid)
    title: str = "CATS Test Run"
    description: str = "Automated assessment by CATS"
    start: datetime = Field(default_factory=_now)
    end: datetime | None = None
    local_definitions: LocalDefinitions | None = Field(default=None, alias="local-definitions")
    reviewed_controls: ReviewedControls = Field(alias="reviewed-controls")
    observations: list[Observation] = []
    findings: list[Finding] = []

    @field_serializer("start", "end")
    def _serialize_timestamps(self, value: datetime | None) -> str | None:
        return rfc3339_timestamp(value) if value is not None else None


class ImportAp(OscalBase):
    href: str = ""


class Metadata(OscalBase):
    title: str = "CATS Assessment Results"
    last_modified: datetime = Field(default_factory=_now, alias="last-modified")
    # OSCAL's `version` is the *document* version, not the tool's: each run is a new
    # document, so the default is a per-run timestamp. The producing CATS release
    # travels in props[name="tool-version"], which the writer sets.
    version: str = Field(default_factory=lambda: rfc3339_timestamp(_now()))
    oscal_version: str = Field(default=OSCAL_VERSION, alias="oscal-version")
    props: list[Property] = []

    @field_serializer("last_modified")
    def _serialize_last_modified(self, value: datetime) -> str:
        return rfc3339_timestamp(value)


class AssessmentResults(OscalBase):
    uuid: str = Field(default_factory=_uuid)
    metadata: Metadata = Field(default_factory=Metadata)
    import_ap: ImportAp = Field(default_factory=ImportAp, alias="import-ap")
    results: list[Result] = []
