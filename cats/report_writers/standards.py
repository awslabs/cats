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

"""Resolving a test's standard names to control ids, shared by every writer that maps them.

A standard's metadata comes from two places: the ``@cats(control_ids=..., control_group=...)``
decorator on the test (recorded per entry in ``standard_metadata``) and the consumer's
``pytest_cats_standards`` registry. The rules for merging them live here once, so the OSCAL
and Security Hub writers cannot resolve the same standard two different ways.
"""

from collections.abc import Mapping

from cats.hookspecs import StandardMetadata
from cats.reporting import ReportEntry


def resolve_standard(
    standard_name: str,
    decorator_meta: StandardMetadata,
    registry: Mapping[str, StandardMetadata] | None,
) -> StandardMetadata:
    """Resolve a standard's metadata by merging decorator, registry, and fallback.

    Decorator wins per-field. Registry fills gaps. If both are empty for
    ``control_ids``, fall back to ``[standard_name]`` so the writer always
    produces at least one finding.
    """
    registry_entry = registry.get(standard_name) if registry is not None else None

    if decorator_meta.control_ids:
        control_ids = list(decorator_meta.control_ids)
    elif registry_entry is not None and registry_entry.control_ids:
        control_ids = list(registry_entry.control_ids)
    else:
        control_ids = [standard_name]

    if decorator_meta.control_group is not None:
        control_group: str | None = decorator_meta.control_group
    elif registry_entry is not None:
        control_group = registry_entry.control_group
    else:
        control_group = None

    return StandardMetadata(control_ids=control_ids, control_group=control_group)


def decorator_metadata_for(entry: ReportEntry, standard_name: str) -> StandardMetadata:
    """Extract a ``StandardMetadata`` from the per-test decorator metadata."""
    raw = entry["standard_metadata"].get(standard_name, {})
    return StandardMetadata(
        control_ids=list(raw.get("control_ids") or []),
        control_group=raw.get("control_group"),
    )
