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

"""Report writer protocol and registry."""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from cats.config import ReportFormat
from cats.hookspecs import StandardMetadata
from cats.report_run import ReportFile, RunManifest
from cats.report_writers.cats_json import CatsJsonWriter
from cats.report_writers.oscal_ar import OscalArWriter
from cats.report_writers.securityhub import SecurityHubWriter
from cats.reporting import ReportEntry


class ReportWriter(Protocol):
    """Protocol for report format writers.

    Internal. The writer protocol and the registry below are not a plugin interface: there is
    no supported way to add a report format in 1.x, and ``report_formats`` is validated against
    the closed :class:`ReportFormat` enum. Consume ``report.json`` (the covered document) from
    your own tooling instead.

    A writer declares the kind of file it produces (``file``) and writes to whatever
    path the plugin resolves for it: the plugin owns naming (fixed or timestamped),
    the writer owns content. ``reports`` is a read-only view of the session's entries,
    shared by every writer.
    """

    @property
    def file(self) -> ReportFile:
        """The stem and suffix of the file this writer produces."""
        ...

    def write(
        self,
        reports: Sequence[ReportEntry],
        output_path: Path | None,
        run: RunManifest,
        standards_registry: Mapping[str, StandardMetadata] | None,
    ) -> list[str] | None:
        """Write the report for this session to ``output_path``.

        ``output_path`` is None when report files are disabled (``latest_files`` and
        ``timestamped_files`` both false). A writer whose only product is its file
        returns without doing anything; a writer that also delivers elsewhere (Security
        Hub) still delivers.

        ``run`` is the session's manifest; at minimum its ``run_id`` is recorded inside
        the document wherever the format has a place for it, so the files of one run can
        be matched. ``standards_registry`` is the resolved output of the
        ``pytest_cats_standards`` hook, or ``None`` if no consumer implemented it.
        Writers that don't need it should ignore it.

        The returned lines, if any, are printed in the CATS section of pytest's terminal
        summary: the place for an outcome the file alone does not show the user.
        """
        ...


_WRITERS: dict[ReportFormat, ReportWriter] = {
    ReportFormat.CATS_JSON: CatsJsonWriter(),
    ReportFormat.OSCAL_AR: OscalArWriter(),
    ReportFormat.SECURITYHUB: SecurityHubWriter(),
}


def get_writer(fmt: ReportFormat) -> ReportWriter:
    """Return the writer instance for a given report format.

    ``junit`` has none: pytest's own junitxml plugin writes junit.xml during the run,
    switched on by the CATS pytest plugin at configure time.
    """
    return _WRITERS[fmt]
