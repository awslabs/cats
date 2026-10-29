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

import json
import warnings
from collections import defaultdict
from typing import Any, Literal, TypedDict

import pytest

from cats.config import Settings
from cats.exceptions import CatsUsageError
from cats.resources.account_under_test import AccountUnderTest

# A Literal, not a StrEnum: entries are JSON-round-tripped when recorded (pytest_plugin._json_safe), so an
# enum member would be its plain text by the time any reader saw it, and the declared type would lie.
ResultType = Literal["passed", "failed", "skipped", "xpassed", "xfailed", "error", "not_run"]


def get_custom_marker_names() -> set[str]:
    """Get custom marker names from config."""
    return set(Settings().reporting.custom_markers)


class CatsReporter:
    """
    The evidence a test adds to its own report entry.

    One instance per test, provided by the function-scoped ``cats_report`` fixture (and by
    ``get_account_fixture``, which records the accounts it resolves). ``add_account`` puts an
    account into ``accounts_in_test`` when the test reached it some way other than a fixture
    argument; ``add_report_metadata`` attaches JSON values under ``additional_metadata``. Both
    are read when the test's phase report is recorded, so calls made during the test body land
    in the same entry.
    """

    def __init__(self) -> None:
        self.additional_accounts: list[AccountUnderTest] = []
        self.additional_metadata: dict[str, Any] = {}

    def add_report_metadata(self, report_metadata: dict[str, Any]) -> None:
        """
        Adds any provided json-friendly metadata to a test's report.

        Raises:
            CatsUsageError: A value is not JSON-serializable. Evidence is written verbatim or not
                at all, so convert values (for example ``value.isoformat()`` for datetimes)
                before adding them.
        """
        for key, value in report_metadata.items():
            try:
                json.dumps(value)
            except (TypeError, ValueError) as e:
                raise CatsUsageError(
                    f"additional_metadata value for key '{key}' is not JSON-serializable (type {type(value).__name__}). "
                    + "Convert it to JSON types (str/int/float/bool/None/list/dict) first - "
                    + "for example str(value), or value.isoformat() for datetimes."
                ) from e
        self.additional_metadata.update(report_metadata)
        return None

    def add_account(self, account: AccountUnderTest) -> None:
        """
        Adds an account to the test report manually.
        Useful when AccountUnderTest use are not automatically detected in function arguments.
        """
        if account not in self.additional_accounts:
            self.additional_accounts.append(account)
        return None


def get_cats_reporter(item: pytest.Function) -> CatsReporter | None:
    # funcargs may be missing or partial when fixture setup itself errored.
    reporter_arg = [arg for arg in getattr(item, "funcargs", {}).values() if isinstance(arg, CatsReporter)]
    if reporter_arg:
        reporter = reporter_arg.pop()
        if reporter_arg:
            raise CatsUsageError("Test has more than one CatsReporter fixture argument: only one may be passed.")
        return reporter
    return None


class AccountReportDetail(TypedDict):
    account_name: str
    account_id: str
    tags: dict[str, str | None]


def get_account_details(item: pytest.Function, cats_reporter: CatsReporter | None) -> list[AccountReportDetail]:
    """The accounts a test used, from its fixtures, plus any the test registered with cats_report.add_account.

    A test that never ran its setup (a skip or a setup error) has no fixture values, so the
    accounts are read from the fixture names it requested and the config: the evidence then
    still names the account, which is what lets a skip retire a stale Security Hub finding and
    keeps the OSCAL subject filled.
    """
    accounts_in_test = [acc for acc in getattr(item, "funcargs", {}).values() if isinstance(acc, AccountUnderTest)]
    details = [AccountReportDetail(account_name=acc.name, account_id=acc.account_id, tags=acc.tags) for acc in accounts_in_test]
    if len(details) == 0:
        details = _account_details_from_fixture_names(item)
    if cats_reporter is not None:
        details.extend(
            AccountReportDetail(account_name=acc.name, account_id=acc.account_id, tags=acc.tags) for acc in cats_reporter.additional_accounts
        )
    return details


def _account_details_from_fixture_names(item: pytest.Function) -> list[AccountReportDetail]:
    """Every ``<keyname>_account`` fixture the item requested, described from cats.yaml alone (no AWS call)."""
    accounts = Settings().accounts_under_test
    details: list[AccountReportDetail] = []
    for fixture_name in getattr(item, "fixturenames", []):
        keyname = fixture_name.removesuffix("_account")
        if fixture_name.endswith("_account") and keyname in accounts:
            details.append(AccountReportDetail(account_name=keyname, account_id=accounts[keyname].account_id, tags=accounts[keyname].tags))
    return details


def get_standard_markers(item: pytest.Function) -> list[str]:
    """Collect standard names from ``cats_standard`` markers at every scope.

    ``iter_markers`` (not ``own_markers``) so class- and module-level ``@cats``
    apply too. Order is outermost-first (module, class, then function decorators
    in source order). Stacking the same name twice records it once.
    """
    standard_markers = list(item.iter_markers(name="cats_standard"))
    standard_markers.reverse()  # iter_markers yields closest-first; restore source order
    standards: list[str] = []

    for standard in standard_markers:
        standard_name = standard.kwargs.get("name", None)
        if standard_name is None:
            warnings.warn(stacklevel=1, message="a 'cats_standard' marker has been set without kwarg 'name' - ignoring")
        elif standard_name not in standards:
            standards.append(standard_name)

    return standards


def get_standard_metadata(item: pytest.Function) -> dict[str, dict[str, Any]]:
    """Collect per-standard metadata from stacked ``@cats()`` decorators.

    Returns a dict keyed by standard name, with values of the shape::

        {"control_ids": list[str], "control_group": str | None}

    Values are plain dicts (not ``StandardMetadata`` instances) so the
    output is JSON- and pickle-safe across xdist workers.

    Merge rules for a single standard name across stacked decorators:

    - ``control_ids``: union, preserving first-seen order.
    - ``control_group``: first non-``None`` wins (closest-to-``def`` first,
      matching pytest's decorator application order).
    """
    standard_markers = item.iter_markers(name="cats_standard")
    out: dict[str, dict[str, Any]] = {}

    for marker in standard_markers:
        name = marker.kwargs.get("name", None)
        if name is None:
            # get_standard_markers already warns; stay silent here.
            continue

        entry = out.setdefault(name, {"control_ids": [], "control_group": None})

        marker_control_ids = marker.kwargs.get("control_ids", []) or []
        for control_id in marker_control_ids:
            if control_id not in entry["control_ids"]:
                entry["control_ids"].append(control_id)

        marker_group = marker.kwargs.get("control_group", None)
        if marker_group is not None and entry["control_group"] is None:
            entry["control_group"] = marker_group

    return out


def get_custom_markers(item: pytest.Function) -> dict[str, Any]:
    """Collect values of the custom markers declared in cats.yaml (reporting.custom_markers).

    Both ``@pytest.mark.team("payments")`` and ``@pytest.mark.team(kwarg="payments")``
    record ``"payments"``. A bare ``@pytest.mark.team`` carries no value and is ignored
    with a warning.
    """
    declared = get_custom_marker_names()
    # One pass over the item's marks, so the key order is the decorators' order and report.json is diffable across runs.
    custom_markers = [mark for mark in item.iter_markers() if mark.name in declared]
    custom_markers.reverse()  # iter_markers yields closest-first; restore source order
    custom_marker_output = defaultdict(list)

    for custom_mark in custom_markers:
        if "kwarg" in custom_mark.kwargs:
            custom_marker_output[custom_mark.name].append(custom_mark.kwargs["kwarg"])
        elif custom_mark.args:
            custom_marker_output[custom_mark.name].extend(custom_mark.args)
        else:
            warnings.warn(stacklevel=1, message=f"custom marker '{custom_mark.name}' applied without a value - ignoring")

    return custom_marker_output


class ReportEntry(TypedDict):
    """One test's evidence: an element of ``tests`` in report.json.

    Every key is always present, so every entry has the same columns: a value that
    does not apply is ``null`` (or an empty object), never an absent key.

    Entries are sorted by ``nodeid`` before writing, so two runs over the same
    tests are diffable regardless of xdist scheduling. ``nodeid`` is the pytest
    node id (unique per test, includes parametrize ids); ``test_name`` alone can
    collide across files.

    ``result: "error"`` records a test whose setup or teardown raised: a fixture
    error before the test body ran, or a teardown failure after it passed (the
    entry recorded at call time is upgraded, so a broken teardown can never
    stand as passing evidence).

    ``result: "not_run"`` records a test ``test_selection`` in cats.yaml held back: it
    requires something the deployment does not provide, or it matches an ``excluded``
    entry. The entry is built at collection, so ``accounts_in_test`` comes from the
    account fixtures the test asks for, ``reason`` is the exclusion's reason (or the
    gap, ``requires {a, b}; not provided {b}``), ``duration_s`` is ``0.0`` and
    ``recorded_at`` is the moment of collection. It is not a pytest outcome;
    ``run.selection.config_excluded`` carries the structured record behind it.

    - ``error_message``: the failure or error message when ``result`` is
      ``"failed"`` or ``"error"``, else ``null``.
    - ``reason``: the skip or xfail reason when ``result`` is ``"skipped"`` or
      ``"xfailed"``, the exclusion reason when it is ``"not_run"``, else ``null``.
    - ``standard_metadata``: per-standard decorator metadata keyed by standard
      name, values of the shape ``{"control_ids": list[str], "control_group":
      str | None}``; ``{}`` when the test carries no standard.
    - ``additional_metadata``: what the test added through
      ``cats_report.add_report_metadata``; ``{}`` when nothing was added.
    - ``duration_s``: wall-clock seconds of the phase the entry records (the
      test call, or the setup phase for a setup skip or error).
    - ``recorded_at``: the UTC time the entry was recorded, RFC 3339.
    """

    result: ResultType
    accounts_in_test: list[AccountReportDetail]
    test_name: str
    nodeid: str
    test_docstring: str | None
    standards: list[str]
    standard_metadata: dict[str, dict[str, Any]]
    custom_markers: dict[str, list[Any]]
    additional_metadata: dict[str, Any]
    xfail: bool
    error_message: str | None
    reason: str | None
    duration_s: float
    recorded_at: str
