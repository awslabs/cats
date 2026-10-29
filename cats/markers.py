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

from collections.abc import Callable
from typing import TypeVar

import pytest

from cats.config import CATS_DECORATOR_KEYWORDS, ReportFormat, Settings
from cats.exceptions import CatsUsageError
from cats.filepath import get_cats_config_filepath
from cats.hookspecs import control_id_problem

# What a mark decorates: a test function or a test class. A pytest mark returns the very object it
# was given (it attaches ``pytestmark`` and hands the same function back), so every decorator here
# is typed as identity on it, the way pytest types ``MarkDecorator.__call__``: the decorated test
# keeps its exact signature for type checkers, rather than collapsing to ``Callable[..., R]``.
Marked = TypeVar("Marked", bound=Callable[..., object])


def _validate_control_ids(control_ids: list[str], *, oscal: bool) -> list[str]:
    """Reject at decoration time a control id no report format can carry, or one OSCAL output would fail on; a repeat is a mistake."""
    seen: set[str] = set()
    for control_id in control_ids:
        problem = control_id_problem(control_id, oscal=oscal)
        if problem is not None:
            raise CatsUsageError(f"@cats control_ids entry {control_id!r} {problem}")
        if control_id in seen:
            raise CatsUsageError(f"@cats control_ids contains duplicate entry '{control_id}'")
        seen.add(control_id)
    return list(control_ids)


def compose_decos(decos: list[pytest.MarkDecorator]) -> Callable[[Marked], Marked]:
    def composition(func: Marked) -> Marked:
        for deco in reversed(decos):
            func = deco(func)
        return func

    return composition


def cats(
    *,
    standard_name: str | list[str] | None = None,
    control_ids: list[str] | None = None,
    control_group: str | None = None,
    **kwargs: str | bool | list[str],
) -> Callable[[Marked], Marked]:
    """Attach compliance metadata and custom markers to a test.

    ``@cats`` is optional metadata: CATS records every test in ``report.json`` and
    ``junit.xml`` whether or not it is decorated, and the compliance writers (OSCAL
    findings, Security Hub findings) act only on tests that carry a standard. A test that
    attests to no standard but carries a custom marker, ``@cats(team="platform")``, is a
    first-class case.

    The keywords are ``standard_name``, ``control_ids`` and ``control_group``. Every other
    keyword is a custom marker and must be declared under ``reporting.custom_markers`` in
    cats.yaml; ``@cats`` never gains a keyword, so a declared custom marker is never shadowed
    by a later release.

    Markers applied:

    - ``cats_standard(name=..., control_ids=..., control_group=...)`` — one per
      standard name. ``control_ids`` / ``control_group`` are attached to
      the same marker so ``pytest -m "cats_standard(name='...')"`` selects
      by standard.
    - Each custom marker as ``marker(kwarg=value)``, one marker per list element
      for a list value.

    Args:
        standard_name: One or more standard identifiers. Stack multiple
            ``@cats()`` decorators to attach different ``control_ids`` or
            ``control_group`` per standard.
        control_ids: Framework control IDs this standard attests to
            (e.g. ``["AC-3", "SC-7"]``). Per-test override of the
            ``pytest_cats_standards`` registry. If both empty, the writer falls
            back to ``[standard_name]`` as a single control ID.
        control_group: Free-form group label (e.g. ``"Governance"``) for
            grouping observations in OSCAL output. Emitted as
            ``observation.props[name="control-group"]``.
        **kwargs: Custom markers declared in ``reporting.custom_markers``; a value
            is a ``str``, a ``bool`` or a list of ``str``.

    Raises:
        CatsUsageError: A keyword is not a declared custom marker (a misspelled
            CATS keyword would otherwise silently drop a standard from the evidence);
            ``control_ids`` or ``control_group`` is given without ``standard_name``;
            ``standard_name`` is not a ``str`` or a non-empty list of ``str``;
            a custom marker value has another type; a control id is not a non-empty
            string, or is not an OSCAL token while ``oscal_ar`` is a configured format.
    """
    marks: list[pytest.MarkDecorator] = []
    reporting = Settings().reporting

    standard_kwargs: dict[str, object] = {}
    if control_ids is not None:
        standard_kwargs["control_ids"] = _validate_control_ids(control_ids, oscal=ReportFormat.OSCAL_AR in reporting.report_formats)
    if control_group is not None:
        standard_kwargs["control_group"] = control_group

    if isinstance(standard_name, str):
        if standard_name.strip() == "":
            raise CatsUsageError("@cats standard_name is empty; name a standard or omit standard_name.")
        marks.append(pytest.mark.cats_standard(name=standard_name, **standard_kwargs))
    elif isinstance(standard_name, list):
        # An empty list would attach no mark; an empty or non-string name would attach one that no report can key on.
        if len(standard_name) == 0:
            raise CatsUsageError("@cats standard_name is an empty list; name at least one standard or omit standard_name.")
        for single_name in standard_name:
            if isinstance(single_name, str) is False:
                raise CatsUsageError(
                    f"@cats standard_name must be a str or a list of str; list element {single_name!r} is {type(single_name).__name__}."
                )
            if single_name.strip() == "":
                raise CatsUsageError("@cats standard_name contains an empty name; every element must name a standard.")
            marks.append(pytest.mark.cats_standard(name=single_name, **standard_kwargs))
    elif standard_name is not None:
        raise CatsUsageError(f"@cats standard_name must be a str or a list of str, got {type(standard_name).__name__}.")
    elif standard_kwargs:
        raise CatsUsageError("@cats was passed control_ids or control_group without standard_name; they describe a standard, so name one.")

    custom_markers = reporting.custom_markers

    for marker_name, marker_value in kwargs.items():
        if marker_name not in custom_markers:
            declared = ", ".join(custom_markers) or "(none)"
            raise CatsUsageError(
                f"@cats keyword '{marker_name}' is not a CATS keyword ({', '.join(sorted(CATS_DECORATOR_KEYWORDS))}) "
                + f"and is not declared in reporting.custom_markers of {get_cats_config_filepath()} (declared: {declared})."
            )

        if isinstance(marker_value, str | bool):
            marks.append(getattr(pytest.mark, marker_name)(kwarg=marker_value))
        elif isinstance(marker_value, list):
            for list_value in marker_value:
                if isinstance(list_value, str) is False:
                    raise CatsUsageError(
                        f"Custom marker was passed by keyword {marker_name} as a list - must contain strings only - got {list_value}"
                    )
                marks.append(getattr(pytest.mark, marker_name)(kwarg=list_value))
        else:
            raise CatsUsageError(f"@cats custom marker '{marker_name}' must be a str, a bool or a list of str, got {type(marker_value).__name__}.")

    all_marks = compose_decos(marks)
    return all_marks


def cats_requires(*requirements: str) -> Callable[[Marked], Marked]:
    """Declare what the deployment must provide for this test to apply.

    A test runs only when every requirement it declares is provided, by
    ``test_selection.provides`` in cats.yaml or by a ``pytest_cats_provides`` hook. Otherwise
    it is deselected and recorded as ``not_run``, with the gap named
    (``requires {a, b}; not provided {b}``), where a test ``-k`` or ``-m`` removed leaves no
    record at all. A test with no requirements always runs.

    Applies one ``cats_requires(name=...)`` mark per distinct requirement, sorted, so
    ``@cats_requires("b", "a")`` and ``@cats_requires("a", "b")`` attach identical marks, and
    stacked decorators union their requirements. ``pytest -m "cats_requires(name='x')"`` selects
    on it like any mark (and is recorded in ``run.selection`` like any ``-m``). Requirement names are
    free-form strings: a new deployment variant is a new name, and CATS keeps no registry of
    them. The semantics are AND only: mutually exclusive variants are two positive requirements
    (``standard-deployment`` / ``container-deployment``), and a control shared across variants
    is two tests, or one requirement every variant provides.

    ``cats_requires`` is a precondition on the deployment, declared on the test, not a feature
    flag: tests cannot read the provided set, and providing a requirement only ever runs more
    tests, never changes what a test asserts. Applies to classes like any pytest mark.

    Raises:
        CatsUsageError: No requirement was given, or one is not a non-empty string.
    """
    if len(requirements) == 0:
        raise CatsUsageError("@cats_requires needs at least one requirement, e.g. @cats_requires('container-deployment').")
    names: set[str] = set()
    for requirement in requirements:
        if not isinstance(requirement, str) or requirement.strip() == "":
            raise CatsUsageError(f"@cats_requires requirement {requirement!r} must be a non-empty string.")
        names.add(requirement.strip())
    return compose_decos([pytest.mark.cats_requires(name=name) for name in sorted(names)])
