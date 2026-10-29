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

"""One test session's reporting run: its id, where its files go, and what it selected.

The pytest plugin resolves a ``ReportRun`` once, in ``pytest_configure`` on the xdist
controller, and every later step reads that one value: junit routing, the writers, the
fixed-name copies and the terminal summary can never disagree about a path.
"""

import json
from collections import Counter
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypedDict

import pytest

from cats.config import CatsReportingSettings
from cats.reporting import ReportEntry, ResultType
from cats.selection import ConfigExclusionRecord
from cats.user_agent import CatsTestPack

# UTC, basic ISO 8601: no colons (Windows-safe), sorts lexically, e.g. 20260924T105317Z.
RUN_ID_FORMAT = "%Y%m%dT%H%M%SZ"

# report.json and securityhub-findings.json carry this as their top-level ``schema_version``.
# It moves only with a breaking change to the document; additive fields leave it alone, so a
# consumer needs no semver parsing to know whether its reader still applies.
REPORT_SCHEMA_VERSION = 1

_RFC3339_MICROSECONDS_Z = "%Y-%m-%dT%H:%M:%S.%fZ"


def rfc3339_timestamp(moment: datetime) -> str:
    """``2026-09-28T10:39:23.549000Z``: every CATS-owned timestamp, in one shape.

    UTC, fixed microsecond precision, ``Z`` suffix. ``datetime.isoformat()`` is not used because
    it drops the fraction when the microsecond is exactly zero and writes ``+00:00``, so one run
    would carry two shapes. ASFF and junit keep the forms their consumers dictate.
    """
    return moment.astimezone(UTC).strftime(_RFC3339_MICROSECONDS_Z)


# The order results are counted and printed in: what failed before what did not run. ``not_run`` is a
# test ``test_selection`` held back, recorded with its reason and never a pytest outcome (see cats.selection).
RESULT_ORDER: tuple[ResultType, ...] = ("passed", "failed", "error", "skipped", "xfailed", "xpassed", "not_run")


@dataclass(frozen=True)
class ReportFile:
    """A kind of report file, named by its stem and suffix: ``report`` + ``.json``."""

    stem: str
    suffix: str

    def fixed_name(self) -> str:
        """``report.json``: the name that always holds the newest run."""
        return f"{self.stem}{self.suffix}"

    def timestamped_name(self, run_id: str) -> str:
        """``report-20260924T105317Z.json``: one immutable file per run."""
        return f"{self.stem}-{run_id}{self.suffix}"


JUNIT_FILE = ReportFile(stem="junit", suffix=".xml")


@dataclass(frozen=True)
class ReportRun:
    """Where this session's report files go and how they are named."""

    report_dir: Path
    run_id: str
    started_at: datetime
    latest_files: bool
    timestamped_files: bool

    @property
    def writes_files(self) -> bool:
        """False when both naming flags are off: no report file is written at all."""
        return self.latest_files is True or self.timestamped_files is True

    @property
    def role_session_name(self) -> str:
        """``cats-`` plus the run id (``cats-20260924T105317Z``): the RoleSessionName this run assumes every test role under.

        Every AWS API call the run makes in an account under test then carries it in the
        principal ARN, so the run can be found in that account's CloudTrail by this one value.
        The deploy role is assumed as ``cats-infra`` instead (``cats.cfn.session``). Fits STS's
        ``[\\w+=,.@-]{2,64}`` with room for the ``-2`` suffix of :func:`new_run_id`.
        """
        return f"cats-{self.run_id}"

    def primary_path(self, file: ReportFile) -> Path | None:
        """The path a writer writes to.

        The timestamped file when enabled (the immutable evidence, written once), else
        the fixed name, else ``None`` when no files are wanted.
        """
        if self.timestamped_files is True:
            return self.report_dir / file.timestamped_name(self.run_id)
        if self.latest_files is True:
            return self.report_dir / file.fixed_name()
        return None

    def copy_path(self, file: ReportFile) -> Path | None:
        """The fixed-name copy made after writing, only when both flags are on."""
        if self.timestamped_files is True and self.latest_files is True:
            return self.report_dir / file.fixed_name()
        return None

    def paths(self, file: ReportFile) -> list[Path]:
        """Every path this run produces for one file kind, primary first."""
        return [path for path in (self.primary_path(file), self.copy_path(file)) if path is not None]


def resolve_report_dir(cli_report_dir: str | None, configured_report_dir: str | None, config_path: Path) -> Path:
    """Apply the precedence ``--cats-report-dir`` > ``reporting.report_dir`` > current directory.

    The CLI value is relative to the current directory, like any command-line path. The
    configured value is relative to the directory holding ``cats.yaml``, the same rule
    ``inline_policies`` file paths follow, so behavior does not depend on where pytest
    is invoked.
    """
    if cli_report_dir is not None:
        return Path(cli_report_dir)
    if configured_report_dir is not None:
        configured = Path(configured_report_dir)
        return configured if configured.is_absolute() else config_path.parent / configured
    return Path(".")


def new_run_id(now: datetime, report_dir: Path, files: Sequence[ReportFile], reserve: bool = False) -> str:
    """The run id for a session starting at ``now``.

    A second run starting in the same second against the same directory gets a ``-2``
    (``-3``, ...) suffix, so a timestamped file is never overwritten. With ``reserve`` the id
    is claimed at once by creating the first file's timestamped path (``open(path, "x")``),
    so a concurrent session that checks a moment later sees it and takes the next suffix;
    without it, two sessions starting in one second could choose the same id. Only a run
    that writes timestamped files reserves: creating a fixed-name file here would truncate
    the previous run's evidence before this run has any. The writer overwrites the empty
    file at session end; after a crash it stays as the trace of a run that started.
    """
    base = now.strftime(RUN_ID_FORMAT)
    if reserve is True:
        report_dir.mkdir(parents=True, exist_ok=True)
    attempt = 1
    candidate = base
    while True:
        if any((report_dir / file.timestamped_name(candidate)).exists() for file in files):
            attempt += 1
            candidate = f"{base}-{attempt}"
            continue
        if reserve is False or len(files) == 0:
            return candidate
        try:
            with (report_dir / files[0].timestamped_name(candidate)).open("x"):
                return candidate
        except FileExistsError:
            attempt += 1
            candidate = f"{base}-{attempt}"


def resolve_report_run(
    cli_report_dir: str | None,
    settings: CatsReportingSettings,
    config_path: Path,
    files: Sequence[ReportFile],
    now: datetime,
) -> ReportRun:
    """Resolve the directory, run id and naming flags for one session; a timestamped run reserves its id (see new_run_id)."""
    report_dir = resolve_report_dir(cli_report_dir, settings.report_dir, config_path)
    return ReportRun(
        report_dir=report_dir,
        run_id=new_run_id(now, report_dir, files, reserve=settings.timestamped_files),
        started_at=now,
        latest_files=settings.latest_files,
        timestamped_files=settings.timestamped_files,
    )


@dataclass(frozen=True)
class RunSelection:
    """What narrowed this session below what pytest would otherwise have collected, as a record, and what ``test_selection`` decided.

    Recorded in the run manifest as ``selection``. CATS records the narrowing and does not
    judge it: every command-line field but ``stopped_early`` is a choice the user made on the
    command line, and whether a run was the whole suite is a question about the repository and
    the pipeline at that commit, which the record lets a reader answer. The config-driven
    selection is the other kind of narrowing and leaves a fuller record: a test ``test_selection``
    held back is in the report as ``not_run`` with its reason, so ``provided``, ``config_excluded``
    and ``configured_exclusions`` say what the configured scope was and what it held back, where
    the command-line fields only say that something narrowed the run.
    """

    keyword: str  # -k expression, "" when unset
    markexpr: str  # -m expression, "" when unset
    last_failed: bool  # --lf
    deselect: tuple[str, ...]  # --deselect node ids
    explicit_args: tuple[str, ...]  # test paths or node ids given on the command line
    stopped_early: str  # why the session stopped before the last test, "" when it did not
    provided: tuple[str, ...] = ()  # test_selection.provides plus every pytest_cats_provides result, sorted
    config_excluded: tuple[ConfigExclusionRecord, ...] = ()  # one record per test held back, sorted by nodeid
    configured_exclusions: tuple["ConfiguredExclusionRecord", ...] = ()  # test_selection.excluded as written, in order

    @property
    def unmatched_exclusions(self) -> list["UnmatchedExclusionRecord"]:
        """The ``test_selection.excluded`` entries that held back no test, with their position.

        Not an error: a ``-k`` run or a renamed test produces one legitimately. Reported because it
        usually means the exclusion is not doing what its author thinks.
        """
        matched = {record["excluded_index"] for record in self.config_excluded if record["excluded_index"] is not None}
        return [
            UnmatchedExclusionRecord(index=index, marker=entry["marker"], values=entry["values"], reason=entry["reason"])
            for index, entry in enumerate(self.configured_exclusions)
            if index not in matched
        ]

    def as_record(self) -> "RunSelectionRecord":
        """The JSON shape recorded in the run manifest."""
        return RunSelectionRecord(
            keyword=self.keyword,
            markexpr=self.markexpr,
            last_failed=self.last_failed,
            deselect=list(self.deselect),
            explicit_args=list(self.explicit_args),
            stopped_early=self.stopped_early,
            provided=list(self.provided),
            config_excluded=list(self.config_excluded),
            unmatched_exclusions=self.unmatched_exclusions,
        )


class ConfiguredExclusionRecord(TypedDict):
    """One ``test_selection.excluded`` entry as the configuration states it."""

    marker: str
    values: list[str | bool]
    reason: str


class UnmatchedExclusionRecord(ConfiguredExclusionRecord):
    """A ``test_selection.excluded`` entry that held back no test, with its position in the list."""

    index: int


class RunSelectionRecord(TypedDict):
    """``run.selection`` in report.json: what narrowed the session, in pytest's own terms, and what ``test_selection`` decided.

    - ``keyword`` .. ``stopped_early``: the command-line selectors and whether the run stopped early, as a record.
    - ``provided``: every requirement the run provided (``test_selection.provides`` plus the
      ``pytest_cats_provides`` hooks), sorted: the scope the ``cats_requires`` tests were selected against.
    - ``config_excluded``: one :class:`cats.selection.ConfigExclusionRecord` per test held back, sorted by
      nodeid; each is also a ``not_run`` entry in ``tests``.
    - ``unmatched_exclusions``: the ``test_selection.excluded`` entries that held back no test.
    """

    keyword: str
    markexpr: str
    last_failed: bool
    deselect: list[str]
    explicit_args: list[str]
    stopped_early: str
    provided: list[str]
    config_excluded: list[ConfigExclusionRecord]
    unmatched_exclusions: list[UnmatchedExclusionRecord]


class RunAccount(TypedDict):
    """One account that appeared in this run's evidence."""

    account_name: str
    account_id: str


class CallerIdentity(TypedDict):
    """``run.caller_identity``: the principal CATS ran as, from the environment's credential chain.

    ``GetCallerIdentity`` on the controller: the pipeline role and job, or the SSO user, that
    every test role of the run was assumed from.
    """

    account_id: str
    arn: str


class RunTestPack(TypedDict):
    """One entry of ``run.test_packs``: a test pack as its ``pytest_cats_test_pack`` hook declared it.

    The same three values render as the ``AwsCatsPack/<name>#<version>#<key>#<value>...`` token in
    the User-Agent of every AWS API call the run made, so the pack in the report and the pack in
    CloudTrail's ``userAgent`` field are one string apart.
    """

    name: str
    version: str
    metadata: dict[str, str]


class RunGit(TypedDict):
    """``run.git``: the checkout pytest ran from, as it was when the session started.

    - ``commit``: ``git rev-parse HEAD``.
    - ``dirty``: true when a tracked file under pytest's rootdir was changed, or an untracked
      ``.py`` file sat under it. An untracked file of any other kind never dirties a run (a
      report directory that is not ignored, a scratch file); a change elsewhere in a monorepo
      does not either.
    """

    commit: str
    dirty: bool


@dataclass(frozen=True)
class RunProvenance:
    """What code and configuration the run tested with, gathered once on the controller at session start.

    Gathered before any writer touches the report directory, so a tracked fixed-name report file
    the run is about to overwrite cannot make the run that overwrites it dirty.

    - ``test_packs``: every registered pack, sorted by name; ``[]`` when no conftest declares one.
    - ``git``: :class:`RunGit`, or ``None`` (warned) when ``git`` is not on ``PATH``, the rootdir
      is not inside a work tree, ``HEAD`` has no commit, or ``git`` did not answer in time.
    - ``config_sha256``: lowercase hex SHA-256 of the bytes of ``cats.yaml`` as it was read, or
      ``None`` (warned) when the file could not be read.
    """

    test_packs: list[RunTestPack]
    git: RunGit | None
    config_sha256: str | None


class WriterOutcome(TypedDict):
    """One entry of ``run.writers``: what a report format did this session.

    - ``format``: the ``report_formats`` value.
    - ``file``: the file it wrote, relative to the report directory; null when report files
      are off.
    - ``status``: ``ok`` (its file exists and/or it delivered), ``skipped`` (report files off
      and the format's only product is a file), ``failed`` (it raised; the run exits 1).
    - ``detail``: the error text when it failed, else the writer's own summary (the Security
      Hub import outcome), else empty.
    """

    format: str
    file: str | None
    status: Literal["ok", "skipped", "failed"]
    detail: str


class ResultCounts(TypedDict):
    """``run.counts`` in report.json: ``total`` (the number of entries in ``tests``), then one key per
    result in RESULT_ORDER. Every key is always present, ``0`` for a result the run did not produce."""

    total: int
    passed: int
    failed: int
    error: int
    skipped: int
    xfailed: int
    xpassed: int
    not_run: int


class RunManifest(TypedDict):
    """``run`` in report.json: the provenance of the evidence in ``tests``.

    - ``run_id``: ``YYYYMMDDTHHMMSSZ``, also in the timestamped file names, the OSCAL
      ``cats-run-id`` prop and the JUnit ``cats:run_id`` testsuite property.
    - ``started_at`` / ``finished_at``: RFC 3339, UTC.
    - ``role_session_name``: ``cats-<run_id>`` (``cats-20260924T105317Z``, the id being the run's
      start time), the RoleSessionName the run assumed every test role under (see
      :attr:`ReportRun.role_session_name`); the value to look the run up by in the CloudTrail
      of an account under test.
    - ``caller_identity``: the principal that ran CATS (see :class:`CallerIdentity`); null when
      no test used an account, so no role was assumed and there is nothing to correlate, or
      when ``GetCallerIdentity`` failed (warned).
    - ``test_packs`` / ``git`` / ``config_sha256``: what code and configuration the run tested
      with (see :class:`RunProvenance`). ``git`` describes the checkout pytest ran from: with
      ``--pyargs`` the test code lives in site-packages and ``test_packs[].version`` is the
      provenance field instead.
    - ``pytest_args``: the pytest command line exactly as invoked (``cats test`` passes
      ``-vv -ra`` by default).
    - ``config_path``: ``cats.yaml`` as it was named, not resolved, so the evidence carries
      no machine-specific absolute path.
    - ``aws_region``: ``cats_deployment_settings.aws_region`` after the ``AWS_REGION`` fallback; null
      when the run had no Region configured (no test that needs one can have passed).
    - ``partition`` / ``cfn_stack_prefix``: with ``aws_region``, the inputs to every deployed
      role name and Security Hub finding ``Id``, so an auditor can correlate this file with
      findings and CloudTrail.
    - ``selection``: what narrowed the run (``-k``, ``-m``, ``--lf``, ``--deselect``, the paths
      or node ids on the command line) and whether it stopped early, as a record (see
      :class:`RunSelectionRecord`); the file makes no claim about whether that was the whole suite.
      A test ``test_selection`` held back is recorded rather than dropped: it is in ``tests`` as
      ``not_run`` with its reason, and ``selection`` says what the configured scope was.
    - ``counts``: ``total`` plus one key per result, ``not_run`` included (see
      :class:`ResultCounts`); ``total`` is the number of entries in ``tests``.
    - ``accounts``: every (keyname, account id) pair that appears in ``tests``, first-seen order.
    - ``outputs``: the names of every file this run was configured to write, relative to the
      report directory; ``writers`` says which were.
    - ``writers``: one :class:`WriterOutcome` per report format, filled after the writers ran
      (``report.json`` is rewritten with them), so the file says whether its siblings exist
      and whether the Security Hub import succeeded.
    """

    run_id: str
    started_at: str
    finished_at: str
    role_session_name: str
    caller_identity: CallerIdentity | None
    test_packs: list[RunTestPack]
    git: RunGit | None
    config_sha256: str | None
    cats_version: str
    python_version: str
    pytest_version: str
    pytest_args: list[str]
    config_path: str
    aws_region: str | None
    partition: str
    cfn_stack_prefix: str
    exit_status: int
    selection: RunSelectionRecord
    counts: ResultCounts
    accounts: list[RunAccount]
    outputs: list[str]
    writers: list[WriterOutcome]


def count_results(entries: Sequence[ReportEntry]) -> ResultCounts:
    """The ``run.counts`` block. Spelled out, not looped over RESULT_ORDER: a ResultType value with no key is then a type error."""
    tally = Counter(entry["result"] for entry in entries)
    return ResultCounts(
        total=len(entries),
        passed=tally["passed"],
        failed=tally["failed"],
        error=tally["error"],
        skipped=tally["skipped"],
        xfailed=tally["xfailed"],
        xpassed=tally["xpassed"],
        not_run=tally["not_run"],
    )


def _accounts_seen(entries: Sequence[ReportEntry]) -> list[RunAccount]:
    """Every (keyname, account id) pair in the entries, first-seen order: two keynames on one id are two accounts to a test."""
    seen: dict[tuple[str, str], RunAccount] = {}
    for entry in entries:
        for account in entry["accounts_in_test"]:
            key = (account["account_name"], account["account_id"])
            seen.setdefault(key, RunAccount(account_name=account["account_name"], account_id=account["account_id"]))
    return list(seen.values())


def build_run_manifest(
    run: ReportRun,
    selection: RunSelection,
    entries: Sequence[ReportEntry],
    files: Sequence[ReportFile],
    finished_at: datetime,
    exit_status: int,
    pytest_args: Sequence[str],
    config_path: Path,
    aws_region: str | None,
    partition: str,
    cfn_stack_prefix: str,
    cats_version: str,
    python_version: str,
    pytest_version: str,
    caller_identity: CallerIdentity | None,
    provenance: RunProvenance,
) -> RunManifest:
    """Assemble the ``run`` block from the session's resolved state. Pure; ``writers`` is filled after the writers ran."""
    return RunManifest(
        run_id=run.run_id,
        started_at=rfc3339_timestamp(run.started_at),
        finished_at=rfc3339_timestamp(finished_at),
        role_session_name=run.role_session_name,
        caller_identity=caller_identity,
        test_packs=provenance.test_packs,
        git=provenance.git,
        config_sha256=provenance.config_sha256,
        cats_version=cats_version,
        python_version=python_version,
        pytest_version=pytest_version,
        pytest_args=list(pytest_args),
        config_path=str(config_path),
        aws_region=aws_region,
        partition=partition,
        cfn_stack_prefix=cfn_stack_prefix,
        exit_status=exit_status,
        selection=selection.as_record(),
        counts=count_results(entries),
        accounts=_accounts_seen(entries),
        outputs=[path.name for file in files for path in run.paths(file)],
        writers=[],
    )


def run_properties(run: RunManifest) -> list[tuple[str, str]]:
    """The ``run`` facts every report format carries beside its evidence, as ordered (name, value) pairs.

    One table, two writers: OSCAL writes each as ``metadata.props[name="cats-<name>"]`` with
    the underscores as dashes (``cats-run-id``), JUnit as a ``cats:<name>`` testsuite property
    (``cats:run_id``). A fact that is null in the manifest has no entry, since neither format
    carries a null property; a name may repeat (one ``test_pack`` per pack). Adding a fact
    here adds it to both files.

    ``test_pack`` carries the pack's User-Agent token, byte for byte the string in the
    ``userAgent`` field of every CloudTrail event the run caused.
    """
    properties = [("run_id", run["run_id"]), ("role_session_name", run["role_session_name"])]
    if run["caller_identity"] is not None:
        properties.append(("caller_identity_arn", run["caller_identity"]["arn"]))
    if run["git"] is not None:
        properties.append(("git_commit", run["git"]["commit"]))
        properties.append(("git_dirty", json.dumps(run["git"]["dirty"])))
    if run["config_sha256"] is not None:
        properties.append(("config_sha256", run["config_sha256"]))
    for pack in run["test_packs"]:
        properties.append(("test_pack", CatsTestPack(**pack).user_agent_token))
    return properties


def selection_from_session(
    session: pytest.Session,
    exitstatus: int | pytest.ExitCode,
    provided: Collection[str] = (),
    config_excluded: Sequence[ConfigExclusionRecord] = (),
    configured_exclusions: Sequence[ConfiguredExclusionRecord] = (),
) -> RunSelection:
    """Read the selection pytest applied to this session from its config and final state, plus what ``test_selection`` decided.

    Every option is read with a default: ``--lf`` belongs to the cacheprovider plugin and
    ``-k``/``-m`` to the mark plugin, and a run with either disabled (``-p no:cacheprovider``)
    has no such option to read. ``provided``, ``config_excluded`` and ``configured_exclusions``
    are what the plugin gathered at collection (see cats.selection); records are kept sorted by
    nodeid so the manifest is diffable across xdist schedules.
    """
    config = session.config
    deselect = config.getoption("deselect", default=None) or []
    explicit_args = tuple(config.args) if config.args_source == pytest.Config.ArgsSource.ARGS else ()

    stopped_early = ""
    if exitstatus == pytest.ExitCode.INTERRUPTED:
        stopped_early = "interrupted"
    elif session.shouldfail:
        stopped_early = str(session.shouldfail)
    elif session.shouldstop:
        stopped_early = str(session.shouldstop)

    return RunSelection(
        keyword=config.getoption("keyword", default="") or "",
        markexpr=config.getoption("markexpr", default="") or "",
        last_failed=config.getoption("lf", default=False) is True,
        deselect=tuple(deselect),
        explicit_args=explicit_args,
        stopped_early=stopped_early,
        provided=tuple(sorted(provided)),
        config_excluded=tuple(sorted(config_excluded, key=lambda record: record["nodeid"])),
        configured_exclusions=tuple(configured_exclusions),
    )
