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

"""The written assessment-results.json must validate against the vendored OSCAL 1.2.1 schema.

OSCAL puts minItems: 1 on every array, target-id/prop names are tokens, and metadata
fields are required - the schema is the arbiter, not our models. These tests run the
real writer end to end for the shapes that once produced invalid documents: an empty
run, a run with no control ids, empty subjects, and colon-carrying prop keys.
"""

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from cats.report_writers.oscal_ar import OscalArWriter
from cats.reporting import ReportEntry
from tests.utils import make_run_manifest

_SCHEMA_PATH = Path(__file__).parent / "schemas" / "oscal_assessment-results_schema_1.2.1.json"


def _pythonize_patterns(value: Any) -> Any:  # noqa: ANN401 - recursive JSON transform
    """NIST's schema uses ECMA `\\p{L}`/`\\p{N}` classes that Python's `re` cannot compile."""
    if isinstance(value, dict):
        return {
            key: (
                val.replace(r"\p{L}", r"[^\W\d_]").replace(r"\p{N}", r"\d") if key == "pattern" and isinstance(val, str) else _pythonize_patterns(val)
            )
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [_pythonize_patterns(item) for item in value]
    return value


@pytest.fixture(scope="module")
def oscal_schema() -> dict[str, Any]:
    return _pythonize_patterns(json.load(_SCHEMA_PATH.open()))  # type: ignore[no-any-return]


def _entry(
    result: str = "passed",
    test_name: str = "test_something",
    accounts: list[dict[str, Any]] | None = None,
    standards: list[str] | None = None,
    standard_metadata: dict[str, dict[str, Any]] | None = None,
    custom_markers: dict[str, list[str]] | None = None,
    additional_metadata: dict[str, Any] | None = None,
    error_message: str | None = None,
    reason: str | None = None,
    xfail: bool = False,
) -> ReportEntry:
    entry: ReportEntry = {
        "result": result,  # type: ignore[typeddict-item]
        "accounts_in_test": accounts if accounts is not None else [],  # type: ignore[typeddict-item]
        "test_name": test_name,
        "nodeid": f"tests/test_module.py::{test_name}",
        "test_docstring": None,
        "standards": standards or [],
        "custom_markers": custom_markers or {},
        "standard_metadata": standard_metadata or {},
        "additional_metadata": additional_metadata or {},
        "xfail": xfail,
        "error_message": error_message,
        "reason": reason,
        "duration_s": 0.5,
        "recorded_at": "2026-09-24T10:53:17+00:00",
    }
    return entry


def _write_and_load(tmp_path: Path, entries: list[ReportEntry]) -> dict[str, Any]:
    OscalArWriter().write(entries, tmp_path / "assessment-results.json", make_run_manifest(), None)
    return json.load((tmp_path / "assessment-results.json").open())  # type: ignore[no-any-return]


def test_empty_run_validates(tmp_path: Path, oscal_schema: dict[str, Any]) -> None:
    """A run with zero recorded tests once emitted four schema violations."""
    # act
    document = _write_and_load(tmp_path, [])

    # assert
    jsonschema.validate(document, oscal_schema)


def test_run_without_cats_markers_validates(tmp_path: Path, oscal_schema: dict[str, Any]) -> None:
    """No @cats test at all: no control ids resolved, selection must be include-all."""
    # act
    document = _write_and_load(tmp_path, [_entry()])

    # assert
    jsonschema.validate(document, oscal_schema)
    expected_selection: list[dict[str, Any]] = [{"include-all": {}}]
    assert document["assessment-results"]["results"][0]["reviewed-controls"]["control-selections"] == expected_selection


def test_entry_without_accounts_validates(tmp_path: Path, oscal_schema: dict[str, Any]) -> None:
    """A test with no account fixture once emitted observation.subjects: []."""
    # act
    document = _write_and_load(tmp_path, [_entry(standards=["SOC2-CC6.6"])])

    # assert
    jsonschema.validate(document, oscal_schema)


def test_not_run_entry_validates_as_an_observation_without_a_finding(tmp_path: Path, oscal_schema: dict[str, Any]) -> None:
    """A held-back test is an observation and no finding; a run with only such tests reviewed no control and selects include-all."""
    # arrange
    held_back = _entry(result="not_run", standards=["ACME-G-SCP-34"], reason="RDS not in use; accepted by CISO 2026-09-01")

    # act
    document = _write_and_load(tmp_path, [held_back])

    # assert
    jsonschema.validate(document, oscal_schema)
    result = document["assessment-results"]["results"][0]
    expected: tuple[str, bool, list[dict[str, Any]]] = ("RDS not in use; accepted by CISO 2026-09-01", False, [{"include-all": {}}])
    assert expected == (result["observations"][0]["remarks"], "findings" in result, result["reviewed-controls"]["control-selections"])


def test_representative_run_validates(tmp_path: Path, oscal_schema: dict[str, Any]) -> None:
    """Accounts with colon-carrying tag keys, failures, skips, xfails, metadata: all in one document."""
    # arrange
    accounts = [
        {
            "account_name": "prod",
            "account_id": "111111111111",
            "tags": {"aws:cloudformation:stack-name": "CATS-prod", "env": None},
        }
    ]
    entries = [
        _entry(
            result="passed",
            test_name="test_pass",
            accounts=accounts,
            standards=["SOC2-CC6.6"],
            standard_metadata={"SOC2-CC6.6": {"control_ids": ["AC-3", "sc-7.5"], "control_group": "Network"}},
            custom_markers={"team": ["payments"]},
            additional_metadata={"run:context": "ci"},
        ),
        _entry(result="failed", test_name="test_fail", accounts=accounts, standards=["SOC2-CC6.6"], error_message="assert False"),
        _entry(result="skipped", test_name="test_skip", reason="not deployed"),
        _entry(result="xfailed", test_name="test_xfail", reason="known broken", xfail=True),
        _entry(result="error", test_name="test_error", error_message="fixture exploded"),
    ]

    # act
    document = _write_and_load(tmp_path, entries)

    # assert
    jsonschema.validate(document, oscal_schema)


def test_observation_uuids_are_stable_across_runs(tmp_path: Path) -> None:
    """Two runs over the same tests produce the same observation uuids (diffable evidence)."""
    # arrange
    entries = [_entry(standards=["SOC2-CC6.6"])]
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()

    # act
    doc_a = _write_and_load(dir_a, entries)
    doc_b = _write_and_load(dir_b, entries)

    # assert
    obs_a = doc_a["assessment-results"]["results"][0]["observations"][0]["uuid"]
    obs_b = doc_b["assessment-results"]["results"][0]["observations"][0]["uuid"]
    doc_uuid_a = doc_a["assessment-results"]["uuid"]
    doc_uuid_b = doc_b["assessment-results"]["uuid"]
    assert (obs_a == obs_b, doc_uuid_a == doc_uuid_b) == (True, False)
