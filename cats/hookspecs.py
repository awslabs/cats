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

"""CATS pytest hook specifications.

Consumers implement these hooks in their conftest.py.
Registration happens via pytest_addhooks in cats/pytest_plugin.py.
"""

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field

import pytest

from cats.config import CatsConfig
from cats.user_agent import CatsTestPack

# An OSCAL token: what a finding target-id must be. The decorator, the standards registry and
# the OSCAL writer all check control ids against this one rule.
OSCAL_TOKEN = re.compile(r"^[^\W\d][\w.\-]*$")


def control_id_problem(control_id: object, *, oscal: bool) -> str | None:
    """Why ``control_id`` cannot be a control id, or None when it can.

    Every report format carries control ids as the strings the user wrote, so the only rule
    they all share is a non-empty string. The OSCAL writer alone has a grammar: a finding
    ``target-id`` is a token (letters, digits, ``.``, ``-``, ``_``, starting with a letter or
    ``_``), so with ``oscal_ar`` configured every id must be one, and ``AC-2(1)`` is written
    in the catalog convention ``ac-2.1``.
    """
    if not isinstance(control_id, str) or control_id.strip() == "":
        return "must be a non-empty string"
    if oscal is True and OSCAL_TOKEN.match(control_id) is None:
        return (
            "is not an OSCAL token, which reporting.report_formats oscal_ar needs for a finding target-id: it must start with "
            + "a letter or '_' and contain only letters, digits, '.', '-', '_'. Spaces and parentheses are not allowed - "
            + "use the catalog convention (e.g. 'ac-2.1' for AC-2(1))"
        )
    return None


@dataclass(frozen=True)
class StandardMetadata:
    """Per-standard metadata: the controls a standard attests to, for every report format that names them.

    Attributes:
        control_ids: Framework control IDs that this standard attests to
            (e.g. ``["AC-3", "SC-7", "1.1.1"]``), carried as written into
            ``report.json``, JUnit and Security Hub ``RelatedRequirements``.
            With ``oscal_ar`` in ``reporting.report_formats`` each ID becomes
            the ``target-id`` on an OSCAL finding, which the schema constrains
            to a token: letters, digits, ``.``, ``-``, ``_``, starting with
            a letter or ``_``; write ``SC-7(5)`` in the catalog convention
            ``sc-7.5`` there. The list is space-joined into
            ``observation.remarks`` so OSCAL consumers that scan free-text
            for catalog IDs can extract them.
        control_group: Optional free-form grouping label (e.g. ``"Governance"``)
            emitted as ``observation.props[name="control-group"]``. OSCAL
            consumers can use this to group observations by domain.
    """

    control_ids: list[str] = field(default_factory=list)
    control_group: str | None = None


class CatsHookSpec:
    """Hook specifications for CATS pytest plugin extensions."""

    @pytest.hookspec
    def pytest_cats_standards(self) -> Mapping[str, StandardMetadata] | None:
        """Return a registry mapping standard_name to StandardMetadata.

        Called once per session on the main process, after collection and
        before the first test runs. Values here are used when a ``@cats(...)``
        decorator does not supply ``control_ids`` or ``control_group`` directly.

        Every implementer's registry counts: a shared organization conftest, an
        installed plugin and a project conftest are merged. Two implementers may
        declare the same standard only with the same value; different values are
        a usage error naming both, and the session stops before any test runs.
        Every control id must be a non-empty string; with ``oscal_ar`` in
        ``reporting.report_formats`` it must also be an OSCAL token (see
        :class:`StandardMetadata`). Either failure is reported the same way.

        Implement it in a conftest pytest loads before collection (the root
        ``conftest.py``, a ``test*/`` directory's, or a directory passed to
        pytest). Under pytest-xdist the main process does not collect, so a
        conftest first found during collection is warned about and its registry
        is not in the reports.

        Return ``None`` to opt out entirely — decorator values and the
        ``standard_name`` fallback still apply.
        """

    @pytest.hookspec
    def pytest_cats_test_pack(self) -> CatsTestPack | None:
        """Declare the test pack this conftest belongs to.

        CATS identifies itself in the User-Agent of its AWS API calls as
        ``AwsCats/<version>`` (see "Usage attribution" in the README). Return a
        :class:`~cats.user_agent.CatsTestPack` and CATS appends the pack's name and
        version the same way, as ``AwsCatsPack/<name>#<version>``, with any ``metadata``
        as further ``#key#value`` pairs, and records the pack in ``run.test_packs`` of
        report.json (the same token is the OSCAL ``cats-test-pack`` prop and the JUnit
        ``cats:test_pack`` property)::

            from cats import CatsTestPack


            def pytest_cats_test_pack() -> CatsTestPack:
                return CatsTestPack(name="acme-controls", version="0.99.4")

        Read once per process in CATS's ``pytest_configure``, so the implementing conftest
        must be one pytest loads before configure: the root ``conftest.py``, a ``test*/``
        directory's conftest, or one under a path given to pytest. CATS warns about one
        loaded later. Every implementer's result counts; a repeated name keeps its first
        declaration. Values must identify a release, never a run. An invalid result is
        skipped with a warning.
        """

    @pytest.hookspec
    def pytest_cats_provides(self, cats_config: CatsConfig) -> Collection[str] | None:
        """Return the requirements this deployment provides, so a test pack can supply its defaults.

        A test decorated ``@cats_requires(...)`` runs only when every requirement it names is
        provided. ``test_selection.provides`` in cats.yaml is the user's half; this hook is the
        pack's: return what the deployment satisfies by default, or infer it. Every implementer's
        result is unioned with ``provides`` before selection, and ``None`` contributes nothing.
        ``cats_config`` is the loaded configuration, so a default can depend on what the user
        declared::

            from cats import CatsConfig


            def pytest_cats_provides(cats_config: CatsConfig) -> set[str]:
                if "container-deployment" in cats_config.test_selection.provides:
                    return set()
                return {"standard-deployment"}

        Called once per collecting process, during collection and after every conftest under the
        collected paths is loaded, so any of them may implement it; under pytest-xdist each worker
        collects and calls it. An implementation that raises, or returns anything other than a
        set, list or tuple of non-empty strings, is warned about and provides nothing: the tests
        that needed what it would have provided are then recorded ``not_run`` with the gap named,
        which is visible in the terminal summary and as ``counts.not_run`` in report.json.
        """
