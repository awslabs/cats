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
import os
import platform
import shutil
import warnings
import xml.etree.ElementTree as ET
from collections.abc import Generator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import pytest

# Private pytest API with no public equivalent; the pytest<10 pin bounds it.
from _pytest.junitxml import mangle_test_address, xml_key
from botocore.exceptions import BotoCoreError, ClientError

from cats import GetAccountFixture
from cats.config import CatsConfig, ReportFormat, Settings, reset_settings
from cats.exceptions import CatsError, CatsUsageError
from cats.filepath import DEFAULT_FILEPATH, get_cats_config_filepath, get_cats_config_override, set_cats_config_filepath
from cats.hookspecs import CatsHookSpec, StandardMetadata, control_id_problem
from cats.report_run import (
    JUNIT_FILE,
    RESULT_ORDER,
    CallerIdentity,
    ConfiguredExclusionRecord,
    ReportFile,
    ReportRun,
    RunManifest,
    RunProvenance,
    RunSelection,
    WriterOutcome,
    build_run_manifest,
    count_results,
    resolve_report_run,
    rfc3339_timestamp,
    run_properties,
    selection_from_session,
)
from cats.report_writers import ReportWriter, get_writer
from cats.reporting import (
    CatsReporter,
    ReportEntry,
    ResultType,
    get_account_details,
    get_cats_reporter,
    get_custom_marker_names,
    get_custom_markers,
    get_standard_markers,
    get_standard_metadata,
)
from cats.resources.account_under_test import AccountUnderTest
from cats.resources.session_interact import DEFAULT_ROLE_SESSION_NAME, set_role_session_name
from cats.run_provenance import gather_provenance
from cats.selection import ConfigExclusionRecord, select_items
from cats.user_agent import CatsTestPack, attributed_session, register_test_pack
from cats.version import __version__

# Resolved once per session in pytest_configure (controller only); read everywhere after.
_REPORT_RUN_KEY = pytest.StashKey[ReportRun]()
# What code and configuration the run tested with, gathered on the controller in pytest_sessionstart.
_PROVENANCE_KEY = pytest.StashKey[RunProvenance]()
# What narrowed the run and what test_selection decided, set in pytest_sessionfinish for the terminal summary that follows it.
_RUN_SELECTION_KEY = pytest.StashKey[RunSelection]()
# The writers' own summary lines (ReportWriter.write return values), same lifecycle.
_WRITER_NOTES_KEY = pytest.StashKey[list[str]]()
# Set once pytest_configure has read pytest_cats_test_pack; later implementers are too late.
# A conftest registered after it was loaded during collection (see pytest_plugin_registered).
_TEST_PACKS_REGISTERED_KEY = pytest.StashKey[bool]()
# Where a conftest must live for pytest to load it before pytest_configure, on every process.
_EARLY_CONFTEST_LOCATIONS = "Move it to the root conftest.py, a test*/ directory's conftest.py, or a directory passed to pytest."
# The merged pytest_cats_standards registry, resolved on the controller once collection has
# loaded every conftest and before the first test runs; None when no implementer returned one.
_STANDARDS_REGISTRY_KEY = pytest.StashKey[Mapping[str, StandardMetadata] | None]()

# A writer failure is the last line of the CATS section, in red, and fails the run.
WRITER_FAILURE_PREFIX = "report writing failed: "
# CATS's own junit properties live under this prefix; user markers and metadata keys keep their own names.
JUNIT_PROPERTY_PREFIX = "cats:"
# The key the controller hands its RoleSessionName to each xdist worker under (see pytest_configure_node).
_WORKERINPUT_ROLE_SESSION_NAME = "cats_role_session_name"


class _WorkerNode(Protocol):
    """The two attributes of an xdist WorkerController this plugin reads; xdist is not imported."""

    config: pytest.Config
    workerinput: dict[str, Any]


def pytest_addhooks(pluginmanager: pytest.PytestPluginManager) -> None:
    """Register CATS hook specifications so consumers can implement them."""
    pluginmanager.add_hookspecs(CatsHookSpec)


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add CATS-specific command line options."""
    parser.addoption(
        "--cats-report-dir",
        action="store",
        default=None,
        help="Directory for report.json, assessment-results.json, junit.xml and securityhub-findings.json. "
        + "Overrides reporting.report_dir in cats.yaml; both default to the current working directory.",
    )


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest with custom markers, this session's reporting run, JUnit XML, and per-account fixtures."""
    _fall_back_to_rootdir_config(config.rootpath)
    _register_markers(config)
    _register_test_packs(config)
    if _is_xdist_worker(config) is False and _reporting_active(config) is True:
        config.stash[_REPORT_RUN_KEY] = _resolve_report_run(config)
        if ReportFormat.JUNIT in Settings().reporting.report_formats:
            _configure_junit_xml(config)
    _configure_role_session_name(config)
    _register_account_fixtures()


def _configure_role_session_name(config: pytest.Config) -> None:
    """Name this process's test-role sessions after the run, so every AssumeRole of the run is one principal.

    The controller derives ``cats-<run_id>`` from its report run; a worker reads the value the
    controller handed it (see pytest_configure_node). A session with no report run (no cats.yaml,
    ``--collect-only``, ``reporting.enabled: false``) has no run id to be found by and uses the
    default, which is also what a second pytest.main() in one interpreter must fall back to.
    """
    run = config.stash.get(_REPORT_RUN_KEY, None)
    if run is not None:
        set_role_session_name(run.role_session_name)
        return
    workerinput: Mapping[str, object] = getattr(config, "workerinput", {})
    handed_down = workerinput.get(_WORKERINPUT_ROLE_SESSION_NAME)
    set_role_session_name(handed_down if isinstance(handed_down, str) else DEFAULT_ROLE_SESSION_NAME)


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node: _WorkerNode) -> None:
    """Hand this run's RoleSessionName to an xdist worker before it starts (runs on the controller).

    ``workerinput`` is the dict xdist sends to the worker, which reads it back in its own
    pytest_configure. The run id exists only on the controller, so this is how a worker's
    AssumeRole calls come to carry it. ``optionalhook``: the hook belongs to pytest-xdist,
    which is not a CATS dependency, and pytest refuses an unknown hook name otherwise.
    """
    run = node.config.stash.get(_REPORT_RUN_KEY, None)
    if run is not None:
        node.workerinput[_WORKERINPUT_ROLE_SESSION_NAME] = run.role_session_name


def _fall_back_to_rootdir_config(rootpath: Path) -> None:
    """When nothing names a config and ./cats.yaml is absent, read the one at pytest's rootdir.

    `cd tests && pytest` runs from a directory without cats.yaml while pytest's rootdir (pinned by
    the ini file `cats init` writes) holds it. Applies only where the existing chain finds nothing,
    so a project that names its config keeps it.
    """
    if get_cats_config_override() is not None or "CATS_CONFIG" in os.environ or DEFAULT_FILEPATH.is_file() is True:
        return
    candidate = rootpath / "cats.yaml"
    if candidate.is_file() is True:
        set_cats_config_filepath(candidate)
        reset_settings()  # a Settings() cached before configure read the absent default


def _reporting_active(config: pytest.Config) -> bool:
    """Whether this session records and writes evidence.

    A pytest session with no cats.yaml is not a CATS run: an unrelated project that merely has
    aws-cats installed must never find a report.json in its tree. ``--collect-only`` runs no
    test, so there is no evidence to write. ``reporting.enabled: false`` is the user's own switch.
    """
    if get_cats_config_filepath().is_file() is False:
        return False
    if config.getoption("collectonly", default=False) is True:
        return False
    return Settings().reporting.enabled is True


def _register_test_packs(config: pytest.Config) -> None:
    """Invoke ``pytest_cats_test_pack`` once per process and register every valid result.

    Runs on the controller and on every xdist worker, since each is its own process with
    its own sessions. An implementation that raises or returns the wrong type is warned
    about and skipped; the CATS token is emitted regardless.
    """
    config.stash[_TEST_PACKS_REGISTERED_KEY] = True
    try:
        results: list[object] = config.hook.pytest_cats_test_pack()
    except Exception as e:
        warnings.warn(f"pytest_cats_test_pack hook raised {e!r} - no test pack recorded in the User-Agent", stacklevel=2)
        return
    for result in results:  # pluggy has already dropped the None returns of a non-firstresult hook
        if not isinstance(result, CatsTestPack):
            warnings.warn(
                f"pytest_cats_test_pack returned {type(result).__name__}, expected CatsTestPack - ignoring it",
                stacklevel=2,
            )
            continue
        register_test_pack(result)


def pytest_plugin_registered(plugin: object, manager: pytest.PytestPluginManager) -> None:
    """Warn when a conftest implementing a CATS hook arrives after configure, too late for it to count.

    - ``pytest_cats_test_pack`` is read once in ``pytest_configure``, in every process. A
      conftest pytest discovers later, during collection, implements it to no effect.
    - ``pytest_cats_standards`` is read after collection on the process that writes the
      reports. Serially that process collects, so a collection-time conftest counts. Under
      pytest-xdist the controller never collects: only the workers load that conftest, and
      its registry never reaches the reports, whose OSCAL target-ids fall back to the
      standard names. The worker says so, and xdist relays the warning to the controller.

    Either way, say so rather than silently dropping what the hook returns.
    """
    config = manager.get_plugin("pytestconfig")
    if config is None or config.stash.get(_TEST_PACKS_REGISTERED_KEY, False) is False:
        return
    location = getattr(plugin, "__file__", plugin)
    if hasattr(plugin, "pytest_cats_test_pack"):
        _warn_at_hook(
            plugin.pytest_cats_test_pack,
            f"{location!s} implements pytest_cats_test_pack but was loaded after pytest_configure, "
            + f"so its test pack is not in the User-Agent. {_EARLY_CONFTEST_LOCATIONS}",
        )
    if hasattr(plugin, "pytest_cats_standards") and _is_xdist_worker(config) is True:
        _warn_at_hook(
            plugin.pytest_cats_standards,
            f"{location!s} implements pytest_cats_standards but was loaded during collection, which the pytest-xdist "
            + f"controller does not run, so its registry is not in the reports. {_EARLY_CONFTEST_LOCATIONS}",
        )


def _warn_at_hook(hook_function: object, message: str) -> None:
    """Warn with the hook's own definition as the location, not the pluggy frame that registered it."""
    code = getattr(hook_function, "__code__", None)
    if code is None:
        warnings.warn(message, stacklevel=3)
        return
    warnings.warn_explicit(message, UserWarning, filename=code.co_filename, lineno=code.co_firstlineno)


def _is_xdist_worker(config: pytest.Config) -> bool:
    """Under pytest-xdist only the controller resolves paths, writes files and prints the summary."""
    return hasattr(config, "workerinput")


def _report_files(formats: list[ReportFormat]) -> list[ReportFile]:
    """Every kind of file this session may write, in report_formats order.

    ``junit`` has no writer: pytest's own junitxml plugin writes junit.xml during the
    run (see _configure_junit_xml), so its file is named here directly.
    """
    return [JUNIT_FILE if fmt is ReportFormat.JUNIT else get_writer(fmt).file for fmt in formats]


def _resolve_report_run(config: pytest.Config) -> ReportRun:
    """Resolve the report directory, run id and naming flags once for the whole session."""
    reporting = Settings().reporting
    run = resolve_report_run(
        cli_report_dir=config.getoption("--cats-report-dir"),
        settings=reporting,
        config_path=get_cats_config_filepath(),
        files=_report_files(reporting.report_formats),
        now=datetime.now(tz=UTC),
    )
    if reporting.enabled is True and run.writes_files is True:
        run.report_dir.mkdir(parents=True, exist_ok=True)
    return run


def _register_markers(config: pytest.Config) -> None:
    """Declare the CATS markers, including the custom markers named in cats.yaml."""
    config.addinivalue_line(
        "markers",
        "cats_standard(name, control_ids=None, control_group=None): the standard a test attests to; applied by @cats(standard_name=...)",
    )
    config.addinivalue_line(
        "markers",
        "cats_requires(name): a requirement of the deployment this test applies to; the test runs only when test_selection.provides "
        + "(or a pytest_cats_provides hook) provides every one it declares, and is recorded not_run otherwise; applied by @cats_requires(...)",
    )
    for custom_marker in get_custom_marker_names():
        config.addinivalue_line("markers", f"{custom_marker}(kwarg): Defined in cats.yaml")


def _configure_junit_xml(config: pytest.Config) -> None:
    """Turn on pytest's JUnit XML output at this run's junit path.

    Must run inside pytest_configure: the built-in junitxml plugin decides whether
    to activate by reading ``config.option.xmlpath`` at configure time, so setting
    it any later has no effect. An explicit ``--junit-xml`` on the command line wins.
    With ``latest_files`` and ``timestamped_files`` both off there is no junit path
    and nothing is written.
    """
    junit_path = config.stash[_REPORT_RUN_KEY].primary_path(JUNIT_FILE)
    if junit_path is not None and not getattr(config.option, "xmlpath", None):
        config.option.xmlpath = str(junit_path)
        # xunit1 is the family whose schema allows <properties> on a testcase and carries file/line.
        # Only for the file CATS named: a user who passed --junit-xml keeps their own junit_family.
        # Private pytest API (Config._inicache); the pytest<10 pin bounds it.
        config._inicache["junit_family"] = "xunit1"

    # Ensure traceback output is still shown to stdout even with JUnit XML enabled
    if not hasattr(config.option, "tbstyle") or not config.option.tbstyle:
        config.option.tbstyle = "short"


def pytest_sessionstart(session: pytest.Session) -> None:
    """Start this session's evidence from nothing, and record what code and configuration it is about to test with.

    `entries` is module state: a second pytest.main() in one interpreter would otherwise report the
    first run's entries again. Cleared on the controller and on every worker, as is the selection
    outcome that collection fills next.

    The provenance is read here, on the controller, rather than at session end: no writer has
    touched the report directory yet, so a tracked fixed-name report file the run is about to
    overwrite cannot make the run that overwrites it dirty. The checkout described is pytest's
    rootdir, the one the test files were collected from.
    """
    entries.clear()
    _entries_by_nodeid.clear()
    _selection.clear()
    if _is_xdist_worker(session.config) is True or session.config.stash.get(_REPORT_RUN_KEY, None) is None:
        return
    session.config.stash[_PROVENANCE_KEY] = gather_provenance(session.config.rootpath, get_cats_config_filepath())


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(session: pytest.Session, config: pytest.Config, items: list[pytest.Item]) -> None:
    """Warn about account fixtures no account provides, then apply ``test_selection`` to what the command line selected.

    ``trylast``: pytest's own ``-k``/``-m`` deselection runs ``tryfirst`` and ``--deselect`` as a
    plain implementation, so this sees only their survivors. A test held back here was in scope by
    the command line and out of scope by configuration, which is what ``not_run`` means; whatever
    ``-k``, ``-m`` or ``--deselect`` removed leaves no entry and is recorded only as the selector in
    ``run.selection``, as before. ``--lf`` filters after every plain implementation and never sees
    a held-back test, so the two cannot disagree.
    """
    _ = session
    _warn_missing_account_fixtures(config, items)
    _apply_test_selection(config, items)


def _warn_missing_account_fixtures(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Say where CATS looked when a test asks for an account fixture no account provides.

    pytest's own 'fixture not found' lists every unrelated fixture and nothing about cats.yaml.
    This runs during collection, so the warning lands in pytest's warnings summary.
    """
    missing = sorted(
        {
            name
            for item in items
            if isinstance(item, pytest.Function)
            for name in item.fixturenames
            # Private pytest API (Item._fixtureinfo); the pytest<10 pin bounds it.
            if name.endswith("_account") and name not in item._fixtureinfo.name2fixturedefs
        }
    )
    if len(missing) == 0:
        return
    config_path = get_cats_config_filepath().resolve()
    if config_path.is_file() is False:
        where = f"{config_path} does not exist"
    elif len(Settings().accounts_under_test) == 0:
        where = f"{config_path} declares no accounts_under_test"
    else:
        where = f"{config_path} declares the accounts {', '.join(Settings().accounts_under_test)}"
    requested = ", ".join(repr(name) for name in missing)
    config.issue_config_time_warning(
        pytest.PytestWarning(
            f"CATS: {requested} was requested but no such account fixture exists: {where}. "
            + "Account fixtures are named <keyname>_account; set CATS_CONFIG or run pytest from the directory holding cats.yaml."
        ),
        stacklevel=2,
    )


@dataclass
class SelectionOutcome:
    """What this process's collection decided under ``test_selection``, kept for the session's reports.

    Filled by ``pytest_collection_modifyitems`` on the process that collects (serially, the one
    process; under pytest-xdist, every worker), shipped to the controller in ``workeroutput`` and
    merged there by nodeid, so N workers collecting the same suite yield one record per test.
    ``not_run`` holds the evidence entry built for each held-back test at collection time.
    """

    provided: set[str] = field(default_factory=set)
    config_excluded: dict[str, ConfigExclusionRecord] = field(default_factory=dict)
    not_run: dict[str, ReportEntry] = field(default_factory=dict)

    def clear(self) -> None:
        self.provided.clear()
        self.config_excluded.clear()
        self.not_run.clear()

    def as_workeroutput(self) -> dict[str, Any]:
        """The JSON-safe shape a worker sends; records and entries are built-in types already."""
        return {"provided": sorted(self.provided), "config_excluded": list(self.config_excluded.values()), "not_run": list(self.not_run.values())}

    def merge_workeroutput(self, shipped: Mapping[str, Any]) -> None:
        self.provided.update(shipped["provided"])
        for record in shipped["config_excluded"]:
            self.config_excluded.setdefault(record["nodeid"], record)
        for entry in shipped["not_run"]:
            self.not_run.setdefault(entry["nodeid"], entry)


_selection = SelectionOutcome()
# The key a worker ships its SelectionOutcome under, beside "cats_reports".
_WORKEROUTPUT_SELECTION = "cats_selection"


def _apply_test_selection(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Hold back the tests ``test_selection`` says do not apply here, and record each as ``not_run``.

    A project without cats.yaml has an empty ``test_selection`` and nothing happens. Held-back
    items go through ``pytest_deselected`` so pytest's own "N deselected" count stays honest; their
    evidence entries are built now, from their marks and the fixtures they ask for, because they
    will never run.
    """
    cats_config = Settings()
    provided = _provided_requirements(config, cats_config)
    selection = select_items(items, provided, cats_config.test_selection.excluded)
    _selection.provided.update(provided)
    if len(selection.deselected) == 0:
        return
    items[:] = selection.kept
    config.hook.pytest_deselected(items=[item for item, _ in selection.deselected])
    for item, record in selection.deselected:
        _selection.config_excluded[record["nodeid"]] = record
        if isinstance(item, pytest.Function):
            _selection.not_run[record["nodeid"]] = _not_run_entry(item, record)


def _provided_requirements(config: pytest.Config, cats_config: CatsConfig) -> set[str]:
    """``test_selection.provides`` unioned with every ``pytest_cats_provides`` result.

    Each implementer is called on its own so a broken one can be named and skipped while the
    others count, the way the standards registry is resolved. One that raises, or returns anything
    but a set, list or tuple of non-empty strings, is warned about and provides nothing.
    """
    provided = set(cats_config.test_selection.provides)
    hook = getattr(config.hook, "pytest_cats_provides", None)
    if hook is None:
        return provided
    for hookimpl in hook.get_hookimpls():
        source = _plugin_label(config, hookimpl.plugin_name)
        kwargs = {"cats_config": cats_config} if "cats_config" in hookimpl.argnames else {}
        try:
            result: object = hookimpl.function(**kwargs)
        except Exception as e:
            config.issue_config_time_warning(
                pytest.PytestWarning(f"pytest_cats_provides in {source} raised {e!r} - it provides nothing"), stacklevel=2
            )
            continue
        if result is None:
            continue
        if not isinstance(result, set | frozenset | list | tuple) or any(not isinstance(name, str) or name.strip() == "" for name in result):
            config.issue_config_time_warning(
                pytest.PytestWarning(
                    f"pytest_cats_provides in {source} returned {result!r}, expected a set, list or tuple of non-empty strings - it provides nothing"
                ),
                stacklevel=2,
            )
            continue
        provided.update(name.strip() for name in result)
    return provided


def _not_run_entry(item: pytest.Function, record: ConfigExclusionRecord) -> ReportEntry:
    """The evidence entry for a test ``test_selection`` held back, built at collection since it will never run.

    The same columns as a recorded test: the accounts come from the account fixtures the test asks
    for (as for a setup skip), the standards and markers from its marks, the reason from the
    record. ``duration_s`` is 0 and ``recorded_at`` is now.
    """
    entry: ReportEntry = {
        "result": "not_run",
        "accounts_in_test": get_account_details(item, None),
        "test_name": item.name,
        "nodeid": item.nodeid,
        "test_docstring": item.function.__doc__,
        "standards": get_standard_markers(item),
        "standard_metadata": get_standard_metadata(item),
        "custom_markers": get_custom_markers(item),
        "additional_metadata": {},
        "xfail": any(item.iter_markers(name="xfail")),
        "error_message": None,
        "reason": record["reason"],
        "duration_s": 0.0,
        "recorded_at": rfc3339_timestamp(datetime.now(tz=UTC)),
    }
    return _json_safe(entry)


def _register_account_fixtures() -> None:
    """Create a ``<keyname>_account`` fixture for each account in cats.yaml."""
    config_path = get_cats_config_filepath().resolve()
    for account_name, account_config in Settings().accounts_under_test.items():
        scope = account_config.account_settings.fixture_scope
        fixture_name = f"{account_name}_account"

        def make_account_fixture(_name: str = account_name) -> AccountUnderTest:
            return AccountUnderTest(_name)

        # What `pytest --fixtures` shows: the account, its id and the file that declared it.
        make_account_fixture.__doc__ = f"AccountUnderTest for account '{account_name}' ({account_config.account_id}), from {config_path}."
        fixture_func: Any = pytest.fixture(scope=scope, name=fixture_name)(make_account_fixture)
        globals()[fixture_name] = fixture_func


@pytest.fixture
def get_account_fixture(request: pytest.FixtureRequest, cats_report: CatsReporter) -> GetAccountFixture:
    """Resolve an account fixture by name (get_account_fixture('prod_account')) and record the account in the test's evidence."""

    def inner_func(acc_name: str) -> AccountUnderTest:
        account_fixture: AccountUnderTest = request.getfixturevalue(acc_name)
        cats_report.add_account(account_fixture)
        return account_fixture

    return inner_func


# Every evidence entry recorded this session (single-process, or one xdist worker).
entries: list[ReportEntry] = []

# Index into `entries` by test nodeid. A teardown failure arrives after that test's
# call phase has already appended an entry; this is how it finds that entry to update.
_entries_by_nodeid: dict[str, ReportEntry] = {}


@pytest.fixture
def cats_report() -> CatsReporter:
    """The CatsReporter for the running test: add_account() and add_report_metadata() attach evidence to its report entry."""
    return CatsReporter()


def _entry_result(item: pytest.Function, phase_report: pytest.TestReport) -> ResultType:
    """Map one phase's TestReport to the result the evidence entry records."""
    if phase_report.when == "setup":
        return "error" if phase_report.failed is True else "skipped"
    if phase_report.when == "teardown":
        return "error"
    if phase_report.outcome == "passed" and any(item.iter_markers(name="xfail")):
        return "xpassed"  # Unexpected pass (X), non-strict
    if phase_report.outcome == "skipped" and getattr(phase_report, "wasxfail", None) is not None:
        return "xfailed"  # Expected failure (x), by marker or pytest.xfail()
    # "failed" stays "failed" even under an xfail marker: strict XPASS and a
    # raises= mismatch are failures in pytest's own summary.
    return phase_report.outcome


def _extract_reason(phase_report: pytest.TestReport, result: ResultType) -> str | None:
    """The skip or xfail reason for a non-run test, when one is available."""
    if result == "xfailed":
        wasxfail = getattr(phase_report, "wasxfail", None)
        return str(wasxfail) if wasxfail else None
    if result == "skipped":
        # A skip's longrepr is (file, line, "Skipped: reason").
        longrepr = phase_report.longrepr
        if isinstance(longrepr, tuple) and len(longrepr) == 3:
            return str(longrepr[2]).removeprefix("Skipped: ")
        return str(longrepr) if longrepr else None
    return None


def _extract_error_message(phase_report: pytest.TestReport) -> str | None:
    """Pull a human-readable message out of a failed phase's TestReport."""
    if not phase_report.longrepr:
        return None
    if isinstance(phase_report.longrepr, str):
        return phase_report.longrepr
    reprcrash = getattr(phase_report.longrepr, "reprcrash", None)
    if reprcrash is not None and hasattr(reprcrash, "message"):
        return getattr(reprcrash, "message", None)
    try:
        return str(phase_report.longrepr)
    except Exception as e:
        return f"Error message could not be extracted: {e!s}"


def _record_entry(item: pytest.Function, phase_report: pytest.TestReport) -> None:
    """Build the evidence entry for one test and store it in `entries`."""
    cats_reporter = get_cats_reporter(item)
    account_details = get_account_details(item, cats_reporter)
    standard_markers = get_standard_markers(item)
    standard_metadata = get_standard_metadata(item)
    custom_markers = get_custom_markers(item)

    result = _entry_result(item, phase_report)
    is_xfail = any(item.iter_markers(name="xfail")) or getattr(phase_report, "wasxfail", None) is not None
    test_docstring = item.function.__doc__
    error_message = _extract_error_message(phase_report) if result in ("failed", "error") else None
    reason = _extract_reason(phase_report, result)
    additional_metadata = cats_reporter.additional_metadata if cats_reporter is not None else {}

    # Every key is always present: a value that does not apply is None or empty, so every
    # entry has the same columns (see ReportEntry).
    entry: ReportEntry = {
        "result": result,
        "accounts_in_test": account_details,
        "test_name": item.name,
        "nodeid": item.nodeid,
        "test_docstring": test_docstring,
        "standards": standard_markers,
        "standard_metadata": standard_metadata,
        "custom_markers": custom_markers,
        "additional_metadata": additional_metadata,
        "xfail": is_xfail,
        "error_message": error_message,
        "reason": reason,
        "duration_s": phase_report.duration,
        "recorded_at": rfc3339_timestamp(datetime.now(tz=UTC)),
    }

    # Evidence is written verbatim or not at all; see _json_safe.
    entry = _json_safe(entry)

    entries.append(entry)
    _entries_by_nodeid[item.nodeid] = entry

    _record_junit_properties(item, entry)


def _json_safe(entry: ReportEntry) -> ReportEntry:
    """The entry after a JSON round trip, or CatsError naming the culprit.

    Evidence is written verbatim or not at all: a value json.dump cannot represent fails this
    test with the culprit named. Left until session end, the same value would truncate the whole
    evidence file. add_report_metadata already rejects bad metadata at the call site; this catches
    marker and decorator values.

    The round trip also leaves the entry holding built-in types only, so every writer sees the
    same values whether the run is serial or under xdist (whose workers ship entries through this
    same round trip). A StrEnum standard name, marker value or metadata value is its text here:
    never Cls.MEMBER in OSCAL or Security Hub, and never an enum in item.user_properties, which
    xdist sends through execnet, which serializes only built-in types and aborts the whole session
    on the first other type.
    """
    try:
        safe: ReportEntry = json.loads(json.dumps(entry))
    except (TypeError, ValueError) as e:
        raise CatsError(
            f"evidence entry for '{entry['nodeid']}' is not JSON-serializable: {e}. "
            + "Marker values, control_ids, and metadata must be JSON types (str/int/float/bool/None/list/dict)."
        ) from e
    return safe


def _record_teardown_error(item: pytest.Function, phase_report: pytest.TestReport) -> None:
    """A teardown failure is an ERROR in pytest's summary: the entry recorded at call
    time must not stand as passing evidence."""
    entry = _entries_by_nodeid.get(item.nodeid)
    if entry is None:
        _record_entry(item, phase_report)
        return
    if entry["result"] == "passed":
        entry["result"] = "error"
        error_message = _extract_error_message(phase_report)
        if error_message is not None:
            entry["error_message"] = error_message


def _record_junit_properties(item: pytest.Function, entry: ReportEntry) -> None:
    """Attach the entry's data to the JUnit XML report, when the junit format is enabled.

    Appends to ``item.user_properties`` directly (what the ``record_property``
    fixture does): no fixture lookup, so it works for setup-phase skips too and
    avoids the deprecated lookup path pytest 10 removes.
    """
    if ReportFormat.JUNIT not in Settings().reporting.report_formats:
        return
    item.user_properties.extend(_junit_properties(entry))


def _junit_properties(entry: ReportEntry) -> list[tuple[str, str]]:
    """The JUnit properties one entry carries, in order; the same list for a recorded test and a ``not_run`` one.

    CATS's own properties are namespaced ``cats:`` (``cats:standard``, ``cats:account``,
    ``cats:tag:<key>``, ``cats:xfail``, ``cats:description``); a user's custom markers and
    ``add_report_metadata`` keys keep their own names, so no future CATS property can collide
    with one of them.
    """
    properties: list[tuple[str, str]] = []

    def record_property(name: str, value: str) -> None:
        properties.append((name, value))

    for standard in entry["standards"]:
        record_property(f"{JUNIT_PROPERTY_PREFIX}standard", standard)

    for account in entry["accounts_in_test"]:
        record_property(f"{JUNIT_PROPERTY_PREFIX}account", f"{account['account_name']} ({account['account_id']})")

    for account in entry["accounts_in_test"]:
        for tag_key, tag_value in account["tags"].items():
            if tag_value is None:
                record_property(f"{JUNIT_PROPERTY_PREFIX}tag:{tag_key}", "true")
            else:
                record_property(f"{JUNIT_PROPERTY_PREFIX}tag:{tag_key}", tag_value)

    if entry["xfail"]:
        record_property(f"{JUNIT_PROPERTY_PREFIX}xfail", "true")

    if entry["test_docstring"]:
        # Clean up the docstring - remove extra whitespace and newlines
        clean_docstring = " ".join(line.strip() for line in entry["test_docstring"].strip().split("\n"))
        record_property(f"{JUNIT_PROPERTY_PREFIX}description", clean_docstring)

    # One property per value: a list is one property per element, a scalar is itself, and a
    # mapping is JSON, so no property ever carries a Python repr.
    for marker_name, marker_values in entry["custom_markers"].items():
        for marker_value in marker_values:
            record_property(marker_name, _junit_property_value(marker_value))

    for key, value in entry["additional_metadata"].items():
        if isinstance(value, list):
            for element in value:
                record_property(key, _junit_property_value(element))
        else:
            record_property(key, _junit_property_value(value))

    return properties


def _junit_property_value(value: object) -> str:
    """A junit property value is text: strings as they are, other scalars via JSON (``true``, ``42``), mappings as JSON."""
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(
    item: pytest.Function, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport | None, pytest.TestReport | None]:
    """
    Pytest hook that runs AFTER each test phase completes.

    Every phase that carries evidence is recorded: a setup skip or error, the call
    itself, and a teardown failure (which upgrades a passed entry to an error, since
    pytest reports that test as ERROR).

    CRITICAL: This is a wrapper hook. Code after 'yield' runs post-test.
    Any exception after yield will lose the test report unless caught.
    ALL post-yield code MUST be wrapped in try/except with 'return phase_report' guaranteed.
    """
    phase_report = yield

    try:
        if phase_report is not None:
            if (phase_report.when == "setup" and (phase_report.skipped is True or phase_report.failed is True)) or phase_report.when == "call":
                _record_entry(item, phase_report)
            elif phase_report.when == "teardown" and phase_report.failed is True:
                _record_teardown_error(item, phase_report)

    except Exception as e:
        # Attach CATS error to the phase report so it's visible, not swallowed
        if phase_report is not None:
            phase_report.outcome = "failed"
            phase_report.longrepr = f"CATS reporting error (test itself may have passed, but this fails the test): {e}"
        warnings.warn(f"CATS reporting failed: {e}", stacklevel=2)

    return phase_report


@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int | pytest.ExitCode) -> Generator[None, None, None]:
    """Assemble the run manifest, stamp it on the JUnit testsuite, then generate the reports and handle the exit code.

    A wrapper, because the two halves must sit either side of pytest's own junitxml plugin:
    before the yield every run fact is known (the last test has run) and junit.xml is not yet
    written, so the testsuite can still take the run properties; after the yield junit.xml is
    complete, so the writers can run and the fixed-name copy sees a whole file. ``trylast``
    makes this the innermost wrapper, so the after-yield half runs before the terminal
    reporter's own wrapper prints the summary that reads what it stored.
    """

    # Handle pytest-xdist: check if this is a worker process
    if _is_xdist_worker(session.config) is True:
        # This is a worker process - send reports back to main process via workeroutput, filled
        # before the yield because xdist's own wrapper sends workeroutput after its yield.
        # Entries were validated JSON-serializable when recorded, so this cannot mangle.
        if not hasattr(session.config, "workeroutput"):
            session.config.workeroutput = {}  # type: ignore[attr-defined]
        session.config.workeroutput["cats_reports"] = json.loads(json.dumps(entries))  # type: ignore[attr-defined]
        session.config.workeroutput[_WORKEROUTPUT_SELECTION] = _selection.as_workeroutput()  # type: ignore[attr-defined]
        yield
        return

    # This is the main process (or single-process run)
    run = session.config.stash.get(_REPORT_RUN_KEY, None)
    if run is None or exitstatus == pytest.ExitCode.USAGE_ERROR:
        # No cats.yaml, --collect-only, or reporting.enabled: false (see _reporting_active); or refused before
        # the first test (a registry conflict, a bad option): no evidence, and the previous run's fixed-name files stay.
        yield
        return
    reporting = Settings().reporting
    deployment = Settings().cats_deployment_settings
    selection = selection_from_session(
        session,
        exitstatus,
        provided=_selection.provided,
        config_excluded=list(_selection.config_excluded.values()),
        configured_exclusions=[
            ConfiguredExclusionRecord(marker=entry.marker, values=list(entry.values), reason=entry.reason)
            for entry in Settings().test_selection.excluded
        ],
    )
    session.config.stash[_RUN_SELECTION_KEY] = selection

    # The tests test_selection held back are evidence too: in `tests` as not_run, beside what ran.
    not_run_entries = [_selection.not_run[nodeid] for nodeid in sorted(_selection.not_run)]
    entries.extend(not_run_entries)

    # Deterministic output: xdist arrival order varies run to run.
    entries.sort(key=lambda entry: entry["nodeid"])

    # Built even when no file is wanted: writers embed run_id, and outputs is then [].
    files = _report_files(reporting.report_formats)
    manifest = build_run_manifest(
        run=run,
        selection=selection,
        entries=entries,
        files=files,
        finished_at=datetime.now(tz=UTC),
        exit_status=int(exitstatus),
        pytest_args=session.config.invocation_params.args,
        config_path=get_cats_config_filepath(),
        aws_region=deployment.aws_region,
        partition=deployment.partition,
        cfn_stack_prefix=deployment.cfn_stack_prefix,
        cats_version=__version__,
        python_version=platform.python_version(),
        pytest_version=pytest.__version__,
        caller_identity=_caller_identity() if _assumed_a_role(entries) is True else None,
        provenance=session.config.stash[_PROVENANCE_KEY],
    )
    _stamp_junit_run_properties(session.config, manifest)
    _add_junit_not_run_testcases(session.config, not_run_entries)

    yield  # pytest's junitxml plugin writes junit.xml here

    if run.writes_files is True:
        run.report_dir.mkdir(parents=True, exist_ok=True)
    try:
        standards_registry = session.config.stash.get(_STANDARDS_REGISTRY_KEY, None)
        session.config.stash[_WRITER_NOTES_KEY] = _run_report_writers(reporting.report_formats, run, manifest, standards_registry)
    except CatsError as e:
        # Missing compliance evidence fails the run, but as a line in the CATS section under
        # pytest's own summary, not as an INTERNALERROR traceback that replaces both.
        session.config.stash[_WRITER_NOTES_KEY] = [f"{WRITER_FAILURE_PREFIX}{e}"]
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    finally:
        # One writer failing must not lose another writer's fixed-name copy.
        _copy_latest_files(run, files)


def _assumed_a_role(recorded: Sequence[ReportEntry]) -> bool:
    """Whether any test used an account: each one assumed a test role, so there is a caller to name.

    A run that touched no account made no AWS API call, and recording who ran it must not add one.
    """
    return any(len(entry["accounts_in_test"]) > 0 for entry in recorded)


def _caller_identity() -> CallerIdentity | None:
    """Who ran CATS: ``GetCallerIdentity`` on the credential chain every test role was assumed from.

    ``None`` with one warning when STS cannot answer; the run's exit status is unaffected.
    """
    try:
        identity = attributed_session().client("sts").get_caller_identity()
    except (BotoCoreError, ClientError) as e:
        warnings.warn(f"GetCallerIdentity failed, so run.caller_identity is null: {e}", stacklevel=2)
        return None
    return CallerIdentity(account_id=identity["Account"], arn=identity["Arn"])


def _stamp_junit_run_properties(config: pytest.Config, manifest: RunManifest) -> None:
    """Put the run facts on the JUnit testsuite as ``cats:`` properties, when JUnit output is active.

    Runs on the controller before junitxml writes: under xdist no fixture runs there, and the
    controller is the process that writes junit.xml.
    """
    log_xml = config.stash.get(xml_key, None)
    if log_xml is None:
        return
    for name, value in run_properties(manifest):
        log_xml.add_global_property(f"{JUNIT_PROPERTY_PREFIX}{name}", value)


# The ``type`` of the <skipped> element on a not_run testcase: a consumer can tell it from pytest.skip and pytest.xfail.
JUNIT_NOT_RUN_TYPE = "cats.not_run"


def _add_junit_not_run_testcases(config: pytest.Config, not_run: Sequence[ReportEntry]) -> None:
    """Give every ``not_run`` test a ``<testcase>`` in junit.xml, when JUnit output is active.

    pytest's junitxml plugin writes a testcase for every test that reported, and a deselected test
    never does, so these are added through the same ``_NodeReporter`` it uses: the standard CATS
    properties, then ``<skipped type="cats.not_run" message="not run: <reason>"/>``, which JUnit
    consumers render as a skip (the format has no closer state) and which counts in the suite's
    ``tests`` and ``skipped`` totals like any skip. Runs on the controller before junitxml writes.
    """
    log_xml = config.stash.get(xml_key, None)
    if log_xml is None:
        return
    for entry in not_run:
        reporter = log_xml.node_reporter(entry["nodeid"])
        names = mangle_test_address(entry["nodeid"])
        reporter.add_attribute("classname", ".".join(names[:-1]))
        reporter.add_attribute("name", names[-1])
        reporter.add_attribute("file", entry["nodeid"].split("::", 1)[0])
        for name, value in _junit_properties(entry):
            reporter.add_property(name, value)
        reporter.append(ET.Element("skipped", type=JUNIT_NOT_RUN_TYPE, message=f"not run: {entry['reason']}"))


def _run_report_writers(
    formats: list[ReportFormat],
    run: ReportRun,
    manifest: RunManifest,
    standards_registry: Mapping[str, StandardMetadata] | None,
) -> list[str]:
    """Run every configured writer; one writer's failure must not lose another's output.

    Every writer runs even with report files off (its path is then None): a writer whose
    only product is its file returns at once, one that also delivers elsewhere still
    delivers. Returns the writers' terminal summary lines, in writer order.

    Each format's outcome lands in ``manifest["writers"]`` and, when the ``cats_json`` file
    was written, ``report.json`` is written once more so it carries them: the file handed
    to an auditor on its own says which siblings exist and whether the import succeeded.

    Raises after all writers have run if any failed: missing compliance evidence must
    fail the run loudly, not vanish behind a warning.
    """
    notes: list[str] = []
    failures: list[str] = []
    outcomes: list[WriterOutcome] = []
    cats_json: tuple[ReportWriter, Path] | None = None
    for fmt in formats:
        if fmt is ReportFormat.JUNIT:
            outcomes.append(_writer_outcome(fmt, run.primary_path(JUNIT_FILE), notes=[], error=None))  # written by pytest's junitxml plugin
            continue
        writer = get_writer(fmt)
        path = run.primary_path(writer.file)
        try:
            writer_notes = writer.write(tuple(entries), path, manifest, standards_registry) or []
        except Exception as e:
            failures.append(f"{fmt}: {e!r}")
            outcomes.append(_writer_outcome(fmt, path, notes=[], error=e))
            continue
        notes.extend(writer_notes)
        outcomes.append(_writer_outcome(fmt, path, notes=writer_notes, error=None))
        if fmt is ReportFormat.CATS_JSON and path is not None and path.exists() is True:
            cats_json = (writer, path)
    manifest["writers"] = outcomes
    if cats_json is not None:
        _rewrite_report_json_with_outcomes(cats_json[0], cats_json[1], manifest, standards_registry)
    if failures:
        raise CatsError("Report writing failed for: " + "; ".join(failures))
    return notes


def _writer_outcome(fmt: ReportFormat, path: Path | None, notes: list[str], error: Exception | None) -> WriterOutcome:
    """What one format did: failed with this error; skipped when it had no file to write and nothing to say; else ok."""
    file = path.name if path is not None and path.exists() is True else None
    if error is not None:
        return WriterOutcome(format=fmt.value, file=file, status="failed", detail=repr(error))
    if file is None and len(notes) == 0:
        return WriterOutcome(format=fmt.value, file=None, status="skipped", detail="")
    return WriterOutcome(format=fmt.value, file=file, status="ok", detail="; ".join(notes))


def _rewrite_report_json_with_outcomes(
    writer: ReportWriter, path: Path, manifest: RunManifest, standards_registry: Mapping[str, StandardMetadata] | None
) -> None:
    """Write report.json again with ``run.writers`` filled. Best-effort: the first write already holds the evidence."""
    try:
        writer.write(tuple(entries), path, manifest, standards_registry)
    except Exception as e:
        warnings.warn(f"report.json could not be rewritten with the writers' outcomes: {e!r}", stacklevel=2)


def _copy_latest_files(run: ReportRun, files: list[ReportFile]) -> None:
    """Copy each timestamped file to its fixed name, when both naming flags are on.

    A copy, not a symlink: symlinks need developer mode on Windows and are mishandled by
    some CI artifact uploaders. Only files that were actually written are copied.
    """
    for file in files:
        primary, copy = run.primary_path(file), run.copy_path(file)
        if primary is not None and copy is not None and primary.exists():
            shutil.copyfile(primary, copy)


def _summary_lines(
    run: ReportRun, selection: RunSelection, recorded: list[ReportEntry], files: list[ReportFile], notes: list[str], ran: int = 0
) -> list[str]:
    """The lines of the CATS section of pytest's terminal summary.

    Pure: the hook supplies the session state, this decides what the user is told.
    ``notes`` are the writers' own lines (see ReportWriter.write), printed after the files.
    ``ran`` is how many tests pytest itself reported: tests that ran with nothing recorded
    means the recording hook never saw them, which a runner that forks per test does.

    The section says what was written and where, and what ``test_selection`` decided when it is
    configured (see _test_selection_lines). It makes no claim about whether the run was the whole
    suite: what narrowed it is the user's command line, which pytest has already reported
    (``N deselected``, ``stopping after N failures``) and ``run.selection`` records.
    """
    if run.writes_files is False:
        return ["no report files written: reporting.latest_files and reporting.timestamped_files are both false", *notes]

    counts = count_results(recorded)
    breakdown = ", ".join(f"{counts[result]} {result}" for result in RESULT_ORDER if counts[result] > 0)
    tests = _count_tests(counts["total"]) + (f" ({breakdown})" if breakdown else "")
    lines = [f"{tests} written to {run.report_dir.resolve()}"]
    lines.extend("  " + ", ".join(path.name for path in run.paths(file)) for file in files)
    lines.extend(notes)
    lines.extend(_test_selection_lines(selection, counts["not_run"]))

    if counts["total"] == 0 and ran > 0:
        lines.append(
            f"no evidence recorded for the {_count_tests(ran)} that ran: "
            + "a plugin that runs tests out of process (other than pytest-xdist) is not supported"
        )
    return lines


def _count_tests(count: int) -> str:
    """``1 test``, ``0 tests``, ``4191 tests``."""
    return f"{count} test" if count == 1 else f"{count} tests"


# A summary line starting with this names a test_selection.excluded entry that held back no test.
UNMATCHED_EXCLUSION_PREFIX = "test_selection.excluded["


def _test_selection_lines(selection: RunSelection, not_run: int) -> list[str]:
    """What ``test_selection`` did this run, when it is configured or held a test back; nothing otherwise.

    One line for the scope and the count, then one per ``excluded`` entry that matched nothing,
    because that usually means the exclusion is not doing what its author thinks.
    """
    if not_run == 0 and len(selection.provided) == 0 and len(selection.configured_exclusions) == 0:
        return []
    provides = ", ".join(selection.provided) or "nothing"
    excluded = sum(1 for record in selection.config_excluded if record["kind"] == "excluded")
    not_provided = sum(1 for record in selection.config_excluded if record["kind"] == "not_provided")
    if not_run == 0:
        held_back = "no test held back"
    else:
        held_back = f"{_count_tests(not_run)} not run ({excluded} excluded, {not_provided} not provided), recorded as not_run with their reasons"
    lines = [f"test_selection: provides {provides}; {held_back}"]
    for entry in selection.unmatched_exclusions:
        values = ", ".join(json.dumps(value) if isinstance(value, bool) else value for value in entry["values"])
        lines.append(f"{UNMATCHED_EXCLUSION_PREFIX}{entry['index']}] ({entry['marker']}: {values}) matched 0 tests")
    return lines


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter, exitstatus: int | pytest.ExitCode, config: pytest.Config) -> None:
    """Tell the user where this run's evidence went."""
    _ = exitstatus
    if _is_xdist_worker(config) is True:
        return
    run = config.stash.get(_REPORT_RUN_KEY, None)
    selection = config.stash.get(_RUN_SELECTION_KEY, None)
    notes = config.stash.get(_WRITER_NOTES_KEY, None)
    if run is None or selection is None or notes is None:
        # Reporting inactive, or pytest_sessionfinish refused before the writers ran (a usage
        # error): nothing was written, so there is nothing to tell.
        return

    reporting = Settings().reporting
    files = _report_files(reporting.report_formats)
    ran = sum(len(terminalreporter.stats.get(result, [])) for result in RESULT_ORDER)
    terminalreporter.write_sep("-", f"CATS reports: run {run.run_id}")
    for line in _summary_lines(run, selection, entries, files, notes, ran):
        if line.startswith(WRITER_FAILURE_PREFIX):
            terminalreporter.write_line(line, red=True)
        else:
            terminalreporter.write_line(line, yellow=line.startswith(("no report files", "no evidence", UNMATCHED_EXCLUSION_PREFIX)))


def _resolve_standards_registry(config: pytest.Config) -> Mapping[str, StandardMetadata] | None:
    """Call every ``pytest_cats_standards`` implementer and merge the registries into one.

    Every implementer counts; two may declare the same standard only with the same value.
    Returns ``None`` when no implementer returned a registry. A hook implementation that
    raises, or returns something other than a mapping, is warned about and skipped, so
    reporting still runs on the registries that are sound.

    Raises:
        CatsUsageError: Two implementers declare one standard with different values (both
            are named, with their values), or a control id fails
            :func:`cats.hookspecs.control_id_problem` for the configured report formats.
    """
    hook = getattr(config.hook, "pytest_cats_standards", None)
    if hook is None:
        return None
    merged: dict[str, StandardMetadata] = {}
    declared_by: dict[str, str] = {}
    for hookimpl in hook.get_hookimpls():
        source = _plugin_label(config, hookimpl.plugin_name)
        try:
            result: object = hookimpl.function()
        except Exception as e:
            config.issue_config_time_warning(
                pytest.PytestWarning(f"pytest_cats_standards in {source} raised {e!r} - ignoring that registry"), stacklevel=2
            )
            continue
        if result is None:
            continue
        if not isinstance(result, Mapping):
            config.issue_config_time_warning(
                pytest.PytestWarning(
                    f"pytest_cats_standards in {source} returned {type(result).__name__}, expected Mapping[str, StandardMetadata] - "
                    + "ignoring that registry"
                ),
                stacklevel=2,
            )
            continue
        for standard_name, metadata in result.items():
            existing = merged.get(standard_name)
            if existing is None:
                merged[standard_name] = metadata
                declared_by[standard_name] = source
            elif existing != metadata:
                raise CatsUsageError(
                    f"pytest_cats_standards: '{standard_name}' is declared with different values by {declared_by[standard_name]} "
                    + f"({_metadata_text(existing)}) and {source} ({_metadata_text(metadata)}); one registry must own each standard."
                )
    if len(merged) == 0:
        return None
    _validate_registry_control_ids(merged, oscal=ReportFormat.OSCAL_AR in Settings().reporting.report_formats)
    return merged


def _plugin_label(config: pytest.Config, plugin_name: str) -> str:
    """A conftest by its path relative to the rootdir; any other plugin by its registered name."""
    path = Path(plugin_name)
    if path.is_absolute() is True:
        try:
            return str(path.relative_to(config.rootpath))
        except ValueError:
            return str(path)
    return plugin_name


def _metadata_text(metadata: StandardMetadata) -> str:
    return f"control_ids={metadata.control_ids!r}, control_group={metadata.control_group!r}"


def _validate_registry_control_ids(registry: Mapping[str, StandardMetadata], *, oscal: bool) -> None:
    """Every registry control id is carried into the reports as written; say what one cannot be before any test runs."""
    for standard_name, metadata in registry.items():
        for control_id in metadata.control_ids:
            problem = control_id_problem(control_id, oscal=oscal)
            if problem is not None:
                raise CatsUsageError(f"pytest_cats_standards registry: control id {control_id!r} for standard '{standard_name}' {problem}.")


@pytest.hookimpl(tryfirst=True)
def pytest_runtestloop(session: pytest.Session) -> None:
    """Resolve the standards registry before the first test runs, on the process that writes the reports.

    Collection has loaded every conftest by now, and no test has run yet, so a conflicting or
    malformed registry costs nothing but this message. ``tryfirst`` puts this ahead of
    pytest's own loop and of pytest-xdist's controller loop.
    """
    if _is_xdist_worker(session.config) is True or session.config.stash.get(_REPORT_RUN_KEY, None) is None:
        return
    try:
        session.config.stash[_STANDARDS_REGISTRY_KEY] = _resolve_standards_registry(session.config)
    except CatsUsageError as e:
        raise pytest.UsageError(str(e)) from e


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: object, error: object) -> None:
    """Collect test results and the selection outcome from worker nodes when they finish."""
    # This runs on the main process when a worker finishes
    if hasattr(node, "workeroutput") and hasattr(node.workeroutput, "get"):
        worker_output = node.workeroutput
        if hasattr(worker_output, "get") and "cats_reports" in worker_output:
            entries.extend(worker_output["cats_reports"])
        if hasattr(worker_output, "get") and _WORKEROUTPUT_SELECTION in worker_output:
            _selection.merge_workeroutput(worker_output[_WORKEROUTPUT_SELECTION])
