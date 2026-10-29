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

"""CATS JSON report writer — produces report.json."""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TypedDict

from cats.hookspecs import StandardMetadata
from cats.report_run import REPORT_SCHEMA_VERSION, ReportFile, RunManifest
from cats.reporting import ReportEntry


class ReportDocument(TypedDict):
    """The whole of report.json: the document's shape version, the run's provenance, then one entry per test.

    ``schema_version`` is the shape of this document and moves only with a breaking change to
    it (additive fields do not move it), so a consumer can tell whether its reader still applies
    without parsing CATS's own version. ``run`` says who produced the evidence, when, from which
    config and command line, and what narrowed the run; ``tests`` is the evidence, sorted
    by ``nodeid``. Query it with ``jq '.tests[]'``; gate CI on ``jq -e '.run.counts.failed == 0'``.
    """

    schema_version: int
    run: RunManifest
    tests: list[ReportEntry]


class CatsJsonWriter:
    """Write the standard CATS report.json format."""

    file = ReportFile(stem="report", suffix=".json")

    def write(
        self,
        reports: Sequence[ReportEntry],
        output_path: Path | None,
        run: RunManifest,
        standards_registry: Mapping[str, StandardMetadata] | None,
    ) -> None:
        """Emit the report document. The standards registry is not used here."""
        _ = standards_registry  # present for Protocol conformance
        if output_path is None:
            return  # report files are off and the file is this writer's only product
        document = ReportDocument(schema_version=REPORT_SCHEMA_VERSION, run=run, tests=list(reports))
        with output_path.open("w", encoding="utf-8") as f:
            json.dump(document, f, indent=4)
