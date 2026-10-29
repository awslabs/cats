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

"""OSCAL Assessment Results writer — produces assessment-results.json.

Emits a document the OSCAL 1.2.1 assessment-results JSON schema accepts (pinned
by test_oscal_schema_validation.py against a vendored copy). In addition to the fields required
by the schema, the writer adds a few observation-level conveniences that
are common across OSCAL consumers:

- ``observation.props[name="result"]`` — a human-readable pytest outcome
  (``Pass`` / ``Fail`` / ``Error`` / ``N/A``), useful for summary filtering.
- ``observation.props[name="control-group"]`` — the resolved
  ``control_group``, so consumers can group observations by domain.
- ``observation.remarks`` — the resolved control IDs space-joined, so
  consumers scanning remarks free-text for catalog IDs can pick them up.
"""

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from cats.config import Settings
from cats.exceptions import CatsError
from cats.hookspecs import OSCAL_TOKEN, StandardMetadata
from cats.oscal_models import (
    AssessmentResults,
    ControlSelection,
    Finding,
    FindingTarget,
    InventoryItem,
    LocalDefinitions,
    ObjectiveStatus,
    Observation,
    Property,
    RelatedObservation,
    Result,
    ReviewedControls,
    SelectControlById,
    State,
    StatusReason,
    SubjectReference,
    stable_uuid,
)
from cats.report_run import ReportFile, RunManifest, run_properties
from cats.report_writers.standards import decorator_metadata_for, resolve_standard
from cats.reporting import AccountReportDetail, ReportEntry
from cats.version import __version__

# OSCAL TokenDatatype: a letter or underscore, then letters/digits/./-/_ only.
# `[^\W\d]` is the `re` spelling of "\p{L} or _".

# pytest outcome → OSCAL finding state (binary).
_RESULT_TO_STATE: dict[str, State] = {
    "passed": State.SATISFIED,
    "xpassed": State.SATISFIED,
    "failed": State.NOT_SATISFIED,
    "error": State.NOT_SATISFIED,
    "skipped": State.NOT_SATISFIED,
    "xfailed": State.NOT_SATISFIED,
}

# pytest outcome → objective-status reason, so an auditor can distinguish "the test
# failed" from "the test never ran" (skipped) or "passed unexpectedly" (xpassed)
# without leaving the finding.
_RESULT_TO_REASON: dict[str, StatusReason] = {
    "passed": StatusReason.PASS,
    "xpassed": StatusReason.OTHER,
    "failed": StatusReason.FAIL,
    "error": StatusReason.FAIL,
    "skipped": StatusReason.OTHER,
    "xfailed": StatusReason.OTHER,
}

# pytest outcome → human-readable "result" prop value. Exposed as
# ``Pass`` / ``Fail`` / ``Error`` / ``N/A`` so OSCAL consumers can filter
# or summarize by outcome without reinterpreting the binary finding state.
# ``Not run`` is a test test_selection held back; it has an observation and no finding.
_RESULT_TO_PROP: dict[str, str] = {
    "passed": "Pass",
    "xpassed": "Pass",
    "failed": "Fail",
    "xfailed": "Fail",
    "error": "Error",
    "skipped": "N/A",
    "not_run": "Not run",
}


def _require_token(control_id: str, source: str) -> str:
    """Reject a control id the OSCAL schema would reject as a finding target-id."""
    if OSCAL_TOKEN.match(control_id) is None:
        raise CatsError(
            f"control id '{control_id}' ({source}) is not a valid OSCAL token: it must start with "
            + "a letter or '_' and contain only letters, digits, '.', '-', '_'. Spaces and parentheses "
            + "are not allowed - use the catalog convention (e.g. 'ac-2.1' for AC-2(1))."
        )
    return control_id


def _prop_name(raw: str) -> str:
    """Map a marker/metadata/tag key to a valid OSCAL prop name (token).

    '_' maps to '-' (established output convention); any other illegal character
    (spaces, ':', ...) also becomes '-'; a name that still cannot start a token
    gets a 'cats-' prefix.
    """
    name = re.sub(r"[^\w.\-]", "-", raw.replace("_", "-"))
    if re.match(r"^[^\W\d]", name) is None:
        name = f"cats-{name}"
    return name


def _account_key(account: AccountReportDetail) -> str:
    return account["account_id"]


def _build_inventory_item(account: AccountReportDetail) -> InventoryItem:
    """Build an OSCAL InventoryItem from an account report detail."""
    props = [Property(name="account-id", value=account["account_id"])]
    for tag_name, tag_value in account["tags"].items():
        props.append(Property(name=_prop_name(tag_name), value=tag_value if tag_value is not None else "true"))
    description = f"{account['account_name']} ({account['account_id']})"
    return InventoryItem(uuid=stable_uuid("inventory", account["account_id"]), description=description, props=props)


def _build_observation_props(
    entry: ReportEntry,
    result_value: str,
    control_group: str | None,
) -> list[Property]:
    """Build OSCAL properties from the resolved result, markers, and metadata."""
    props: list[Property] = []

    # Human-readable pytest outcome summary.
    props.append(Property(name="result", value=result_value))

    # Group label, when resolved.
    if control_group is not None:
        props.append(Property(name="control-group", value=control_group))

    for standard in entry["standards"]:
        props.append(Property(name="cats-standard", value=standard))
    for marker_name, marker_values in entry["custom_markers"].items():
        for val in marker_values:
            props.append(Property(name=_prop_name(marker_name), value=str(val)))
    if entry["xfail"] is True:
        props.append(Property(name="xfail", value="true"))
    for key, val in entry["additional_metadata"].items():
        props.append(Property(name=_prop_name(key), value=str(val)))
    return props


def _first_non_none_group(groups: list[str | None]) -> str | None:
    """Pick the first non-``None`` group, preserving marker order."""
    for group in groups:
        if group is not None:
            return group
    return None


def build_assessment_results(
    reports: list[ReportEntry],
    standards_registry: Mapping[str, StandardMetadata] | None,
    run: RunManifest,
) -> AssessmentResults:
    """Transform CATS ReportEntry list into an OSCAL AssessmentResults document.

    The result's ``start`` and ``end`` are the run's: the session's ``started_at`` and
    ``finished_at``, not the moment the document was written. Each observation's
    ``collected`` is the moment its test was recorded.
    """
    inventory_map: dict[str, InventoryItem] = {}
    observations: list[Observation] = []
    findings: list[Finding] = []
    all_control_ids: set[str] = set()

    for entry in reports:
        # Deduplicate inventory items by account_id
        subject_refs: list[SubjectReference] = []
        for account in entry["accounts_in_test"]:
            key = _account_key(account)
            if key not in inventory_map:
                inventory_map[key] = _build_inventory_item(account)
            subject_refs.append(SubjectReference(subject_uuid=inventory_map[key].uuid))

        if entry["result"] == "not_run":
            # A test test_selection held back: observed, with the reason, and no finding. A finding
            # would claim a control state (not-satisfied), and no control was assessed; so its
            # control ids are not resolved and do not enter reviewed-controls.
            observations.append(
                Observation(
                    uuid=stable_uuid("observation", entry["nodeid"]),
                    title=entry["test_name"],
                    description=entry["test_docstring"] or entry["test_name"],
                    props=_build_observation_props(entry, _RESULT_TO_PROP["not_run"], None),
                    subjects=subject_refs,
                    collected=datetime.fromisoformat(entry["recorded_at"]),
                    remarks=entry["reason"],
                )
            )
            continue

        # Resolve per-standard metadata for this entry, unioning control IDs
        # and picking the first non-None control_group across standards.
        resolved_control_ids: list[str] = []
        resolved_groups: list[str | None] = []
        for standard in entry["standards"]:
            resolved = resolve_standard(
                standard,
                decorator_metadata_for(entry, standard),
                standards_registry,
            )
            for control_id in resolved.control_ids:
                _require_token(control_id, f"standard '{standard}' on test '{entry['test_name']}'")
                if control_id not in resolved_control_ids:
                    resolved_control_ids.append(control_id)
            resolved_groups.append(resolved.control_group)

        observation_group = _first_non_none_group(resolved_groups)
        result_value = _RESULT_TO_PROP.get(entry["result"], "Error - Test results missing")

        # Space-joined control IDs in remarks so OSCAL consumers that scan
        # free-text for catalog IDs can extract them.
        observation_remarks = " ".join(resolved_control_ids) if resolved_control_ids else None

        obs = Observation(
            uuid=stable_uuid("observation", entry["nodeid"]),
            title=entry["test_name"],
            description=entry["test_docstring"] or entry["test_name"],
            props=_build_observation_props(entry, result_value, observation_group),
            subjects=subject_refs,
            collected=datetime.fromisoformat(entry["recorded_at"]),
            remarks=observation_remarks,
        )
        observations.append(obs)

        # One finding per resolved control ID, linked back to the observation
        all_control_ids.update(resolved_control_ids)
        state = _RESULT_TO_STATE.get(entry["result"], State.NOT_SATISFIED)
        reason = _RESULT_TO_REASON.get(entry["result"])
        finding_remarks = entry["error_message"] or entry["reason"]

        for control_id in resolved_control_ids:
            status = ObjectiveStatus(state=state, reason=reason, remarks=finding_remarks)
            finding = Finding(
                uuid=stable_uuid("finding", entry["nodeid"], control_id),
                title=f"{control_id} assessment",
                description=f"Assessed by {entry['test_name']}",
                target=FindingTarget(target_id=control_id, status=status),
                related_observations=[RelatedObservation(observation_uuid=obs.uuid)],
            )
            findings.append(finding)

    # Assemble result. A run that assessed no specific control ids selects
    # include-all: {} - the schema forbids an empty include-controls array.
    if all_control_ids:
        selection = ControlSelection(include_controls=[SelectControlById(control_id=cid) for cid in sorted(all_control_ids)])
    else:
        selection = ControlSelection(include_all={})
    reviewed = ReviewedControls(control_selections=[selection])
    result = Result(
        start=datetime.fromisoformat(run["started_at"]),
        end=datetime.fromisoformat(run["finished_at"]),
        local_definitions=LocalDefinitions(inventory_items=list(inventory_map.values())) if inventory_map else None,
        reviewed_controls=reviewed,
        observations=observations,
        findings=findings,
    )
    return AssessmentResults(results=[result])


def _strip_empty_arrays(value: Any) -> Any:  # noqa: ANN401 - recursive JSON transform
    """Drop empty-array keys at dump time: OSCAL puts minItems: 1 on every array.

    Applied to the dumped dict (not the models) so tests and consumers of the
    model objects keep list-typed attributes.
    """
    if isinstance(value, dict):
        return {k: _strip_empty_arrays(v) for k, v in value.items() if v != []}
    if isinstance(value, list):
        return [_strip_empty_arrays(v) for v in value]
    return value


class OscalArWriter:
    """Write OSCAL Assessment Results JSON."""

    file = ReportFile(stem="assessment-results", suffix=".json")

    def write(
        self,
        reports: Sequence[ReportEntry],
        output_path: Path | None,
        run: RunManifest,
        standards_registry: Mapping[str, StandardMetadata] | None,
    ) -> None:
        if output_path is None:
            return  # report files are off and the file is this writer's only product
        ar = build_assessment_results(list(reports), standards_registry, run)
        # tool-version, then the run facts shared with junit (cats-run-id, cats-role-session-name, ...).
        ar.metadata.props = [
            Property(name="tool-version", value=__version__),
            *(Property(name=f"cats-{name.replace('_', '-')}", value=value) for name, value in run_properties(run)),
        ]
        ar.import_ap.href = Settings().reporting.oscal_import_ap_href
        dumped = _strip_empty_arrays(ar.model_dump(by_alias=True, exclude_none=True))
        wrapper: dict[str, Any] = {"assessment-results": dumped}
        with output_path.open("w", encoding="utf-8") as f:
            json.dump(wrapper, f, indent=2, default=str)
