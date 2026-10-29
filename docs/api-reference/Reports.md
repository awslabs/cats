# Reports

The contract for every report CATS writes. The [reporting guide](../guides/reporting.md) covers
choosing formats and the report directory; [Reporting and Compliance Alignment](../concepts/reporting.md)
is the idea behind them.

## `report.json`

The file is a JSON object with three keys: `schema_version`, the shape of this document;
`run`, the provenance of the evidence; and `tests`, one entry per test sorted by `nodeid`.
Query the entries with `jq '.tests[]'`; gate a pipeline with
`jq -e '.run.counts.failed == 0' report.json`.

`schema_version` is `1` and moves only with a breaking change to the document; new fields
are added without moving it, so a consumer checks one integer to know whether its reader
still applies. Every timestamp CATS writes is RFC 3339 UTC with microsecond precision and a
`Z` suffix (`2026-09-24T10:53:17.000000Z`); `run_id` is `started_at` to the second in the
basic form a filename can carry.

The entry key is pytest's `nodeid`, relative to pytest's rootdir. `cats init` pins that
rootdir at the project root (`[tool.pytest.ini_options]` in an existing `pyproject.toml`,
else `pytest.ini`), so the same test has the same `nodeid`, and therefore the same OSCAL and
Security Hub identifiers, however pytest is invoked.

```json
{
  "schema_version": 1,
  "run": {
    "run_id": "20260924T105317Z",
    "started_at": "2026-09-24T10:53:17.000000Z",
    "finished_at": "2026-09-24T10:55:02.000000Z",
    "role_session_name": "cats-20260924T105317Z",
    "caller_identity": { "account_id": "999999999999", "arn": "arn:aws:sts::999999999999:assumed-role/ci-runner/job-8812" },
    "test_packs": [{ "name": "acme-controls", "version": "0.99.4", "metadata": { "framework": "NIST-800-53r5" } }],
    "git": { "commit": "3f2a9c1e0d4b7f8a9c2e1d3f5b6a7c8d9e0f1a2b", "dirty": false },
    "config_sha256": "71aa0c2d6e8f1b3a5c7d9e0f2a4b6c8d0e1f3a5b7c9d1e3f5a7b9c1d3e5f7a9b",
    "cats_version": "1.0.0",
    "python_version": "3.13.2",
    "pytest_version": "9.1.1",
    "pytest_args": ["-vv", "-ra"],
    "config_path": "cats.yaml",
    "aws_region": "us-east-1",
    "partition": "aws",
    "cfn_stack_prefix": "CATS-test-infra-",
    "exit_status": 0,
    "selection": {
      "keyword": "", "markexpr": "", "last_failed": false, "deselect": [], "explicit_args": [], "stopped_early": "",
      "provided": ["container-deployment"], "config_excluded": [], "unmatched_exclusions": []
    },
    "counts": { "total": 1, "passed": 1, "failed": 0, "error": 0, "skipped": 0, "xfailed": 0, "xpassed": 0, "not_run": 0 },
    "accounts": [{ "account_name": "prod", "account_id": "111111111111" }],
    "outputs": ["report-20260924T105317Z.json", "report.json"],
    "writers": [{ "format": "cats_json", "file": "report-20260924T105317Z.json", "status": "ok", "detail": "" }]
  },
  "tests": [
    {
      "result": "passed",
      "accounts_in_test": [
        {
          "account_name": "prod",
          "account_id": "111111111111",
          "tags": { "env": "production" }
        }
      ],
      "test_name": "test_cloudtrail_enabled",
      "nodeid": "tests/test_logging.py::test_cloudtrail_enabled",
      "test_docstring": "Verify CloudTrail is enabled.",
      "standards": ["SOC2-CC6.1"],
      "standard_metadata": {},
      "custom_markers": { "team": ["security"] },
      "additional_metadata": {},
      "xfail": false,
      "error_message": null,
      "reason": null,
      "duration_s": 1.482,
      "recorded_at": "2026-09-24T10:53:17.204512Z"
    }
  ]
}
```

### The `run` block

| Key | Type | Meaning |
|---|---|---|
| `run_id` | string | `YYYYMMDDTHHMMSSZ`; also in the timestamped file names, the OSCAL `cats-run-id` prop and the JUnit `cats:run_id` property |
| `started_at`, `finished_at` | string | RFC 3339 UTC, microsecond precision, `Z` suffix |
| `role_session_name` | string | `cats-<run_id>`, e.g. `cats-20260924T105317Z`: the `RoleSessionName` the run assumed every test role under, so every API call the run made in an account under test carries it in the principal ARN. Also the OSCAL `cats-role-session-name` prop and the JUnit `cats:role_session_name` property. See [Finding a run in CloudTrail](#finding-a-run-in-cloudtrail) |
| `caller_identity` | `{account_id, arn}` or null | The principal CATS ran as (`GetCallerIdentity` on the controller): the pipeline role and job, or the SSO user, that the test roles were assumed from. `null` when no test used an account (nothing was assumed, and STS is not asked) or when the call failed (a warning says so). The ARN is the OSCAL `cats-caller-identity-arn` prop and the JUnit `cats:caller_identity_arn` property |
| `test_packs` | array of `{name, version, metadata}` | Every test pack a `pytest_cats_test_pack` hook declared, sorted by name; `[]` when none. The same values render as the `AwsCatsPack/...` User-Agent token, which is the OSCAL `cats-test-pack` prop and the JUnit `cats:test_pack` property, one per pack. See [Which code produced a report](#which-code-and-configuration-produced-a-report) |
| `git` | `{commit, dirty}` or null | The checkout pytest's rootdir sits in, as it was when the run started: `commit` is `HEAD`, `dirty` is `true` when a tracked file under the rootdir was changed or an untracked `.py` file sat under it (an untracked file of any other kind, such as a report directory, does not count). `null` with a warning when `git` is not on `PATH`, the rootdir is not in a work tree, or `HEAD` has no commit. Also the OSCAL `cats-git-commit` and `cats-git-dirty` props and the JUnit `cats:git_commit` and `cats:git_dirty` properties |
| `config_sha256` | string or null | Lowercase hex SHA-256 of the bytes of the file at `config_path`, as read when the run started; `sha256sum cats.yaml` prints the same value. Also the OSCAL `cats-config-sha256` prop and the JUnit `cats:config_sha256` property. `null` with a warning when the file could not be read |
| `cats_version`, `python_version`, `pytest_version` | string | What produced the evidence |
| `pytest_args` | array of strings | The pytest command line exactly as invoked (`cats test` passes `-vv -ra` by default) |
| `config_path` | string | `cats.yaml` as it was named on the command line or in `CATS_CONFIG`, not resolved: relative when it was given relative |
| `aws_region` | string or null | `cats_deployment_settings.aws_region` after the `AWS_REGION` fallback; `null` when the run had no Region configured |
| `partition` | string | `cats_deployment_settings.partition`, derived from the Region when unset. With `aws_region` and `cfn_stack_prefix`, an input to every deployed role name and Security Hub finding `Id` |
| `cfn_stack_prefix` | string | `cats_deployment_settings.cfn_stack_prefix` |
| `exit_status` | integer | pytest's exit code |
| `selection` | object | What narrowed the run, as a record. The command-line selectors: `keyword` (`-k`), `markexpr` (`-m`), `last_failed` (`--lf`), `deselect`, `explicit_args` (the paths or node ids on the command line), `stopped_early` (why the run stopped before the last test, or `""`). Then what `test_selection` decided: `provided` (every requirement the run provided, sorted: `test_selection.provides` plus the `pytest_cats_provides` hooks), `config_excluded` (one record per test held back, sorted by nodeid: `nodeid`, `standards`, `requires`, `kind` of `excluded` or `not_provided`, the matching `excluded_index`, `marker` and `value` or the `not_provided` requirements, and the `reason`), and `unmatched_exclusions` (the `test_selection.excluded` entries that held back no test, with their `index`). No field says whether the run was the whole suite; that depends on the repository and pipeline at that commit |
| `counts` | object | `total` plus one integer per result, `not_run` included; `total` is the number of entries in `tests` |
| `accounts` | array of `{account_name, account_id}` | Every keyname and account id pair that appears in `tests`, first-seen order; two keynames on one account id are two entries |
| `outputs` | array of strings | The files this run set out to write, relative to the report directory; `writers` says which were written |
| `writers` | array of `{format, file, status, detail}` | One entry per `report_formats` value, filled after every writer ran (`report.json` is written a second time to carry them). `status` is `ok`, `skipped` (report files off and the format only writes a file) or `failed` (the run exits 1); `file` is the file written or `null`; `detail` is the failure text, or the writer's own summary, such as the Security Hub import outcome per account |

### Entry shape

Every key is present on every entry: a value that does not apply is `null` (or an empty
object), never an absent key, so entries always have the same columns.

| Key | Type | Meaning |
|---|---|---|
| `result` | `"passed" \| "failed" \| "skipped" \| "xpassed" \| "xfailed" \| "error" \| "not_run"` | The outcome pytest reports, or `not_run` for a test `test_selection` held back |
| `accounts_in_test` | array of `{account_name, account_id, tags}` | Every account fixture the test used; a tag declared without a value has `null` |
| `test_name` | string | pytest's item name: the function name plus the parametrize id, e.g. `test_ssh_closed[prod_account]` (can collide across files; use `nodeid` as the key) |
| `nodeid` | string | The pytest node id — unique per test, includes parametrize ids |
| `test_docstring` | string or null | The test's docstring |
| `standards` | array of strings | `standard_name`s from `@cats`, outermost scope first, deduplicated |
| `standard_metadata` | object | Per-standard `{control_ids, control_group}` from `@cats`, keyed by standard name; `{}` without standards |
| `custom_markers` | object of string arrays | Values of the markers declared in `reporting.custom_markers` |
| `additional_metadata` | object | Data added via `cats_report.add_report_metadata`; `{}` when none |
| `xfail` | bool | Whether the test was marked or imperatively declared expected-to-fail |
| `error_message` | string or null | The failure or error message when `result` is `failed` or `error` |
| `reason` | string or null | The skip or xfail reason when `result` is `skipped` or `xfailed`; the exclusion's reason, or the gap (`requires {a, b}; not provided {b}`), when it is `not_run` |
| `duration_s` | number | Wall-clock seconds of the phase recorded (the test call, or setup for a setup skip or error); `0.0` for `not_run` |
| `recorded_at` | string | When the entry was recorded: RFC 3339 UTC, microsecond precision, `Z` suffix |

A `result` of `error` records a test whose setup or teardown raised: a fixture error
before the test body ran, or a teardown failure after it passed (the entry is upgraded,
so a broken teardown never stands as passing evidence).

A `result` of `not_run` records a test [`test_selection`](../guides/configuration.md#test-selection) held
back: it requires something the deployment does not provide, or it matches an `excluded`
entry. The entry is built at collection, so `accounts_in_test` comes from the account fixtures
the test asks for (as for a setup skip), the standards and markers from its decorators, and
`reason` from the configuration. It is not a pytest outcome; `run.selection.config_excluded`
carries the structured record behind each one.

The tables above are the contract for `report.json`; the `ReportDocument`, `RunManifest`,
`ResultCounts` and `ReportEntry` TypedDicts in the package mirror them and are internal.

The report writers themselves (the `ReportWriter` protocol and its registry in
`cats.report_writers`) are internal too: there is no supported way to add a report format in
1.x, and `report_formats` accepts only the four values above. To produce another format,
consume `report.json` from your own tooling.

### Verifying a report

A report answers three questions about itself: which code and configuration it was produced
with, what the run did in AWS, and, if your process needs one, how to attach a signature.

#### Which code and configuration produced a report

`run.git` is the checkout pytest's rootdir sits in. `commit` with `dirty: false` is a
complete statement: that commit holds every test module, helper, conftest and plugin the run
collected, and a reviewer with the repository checks it with `git show <commit>`.
`dirty: true` means the run corresponds to no commit; the report says so, and the answer for
evidence that has to be reproducible is to rerun from a clean commit, not to argue that the
run was mostly the released code. An untracked file that is not `.py` never sets `dirty`,
so a report directory inside the checkout that is not ignored does not dirty every run, and
neither does a change elsewhere in a monorepo.

`run.test_packs` is the suite's own statement of which release it is, as its
`pytest_cats_test_pack` hook declared it (see [Usage attribution](https://github.com/awslabs/cats/blob/main/README.md#usage-attribution)).
`git` says whether that statement matches what ran. When the tests are installed rather than
checked out (`pytest --pyargs acme_controls`), `git` describes the project directory, not the
package, and the pack version is the provenance field: the installed wheel's `RECORD` file
already lists a SHA-256 per file, so a per-file check exists without CATS.

`run.config_sha256` is the hash of `cats.yaml` as read when the run started, and `cats.yaml`
decides the accounts, Region and roles a run tests, so it is part of "what ran":

```bash
sha256sum "$(jq -r .run.config_path report.json)"   # must print run.config_sha256
```

`run.git` is `null` (with a warning) when `git` is not on `PATH`, the rootdir is not inside a
work tree, or `HEAD` has no commit; `config_sha256` is `null` when the file could not be
re-read. Under pytest-xdist all three are read once on the controller, so a parallel run
records exactly what a serial run records.

#### Finding a run in CloudTrail

Every test that used an account fixture made its AWS API calls under a test role assumed
with `RoleSessionName` equal to `run.role_session_name`, and so did the Security Hub import
at the end of the run. In the CloudTrail of the account under test, every event the run
caused therefore carries that value in `userIdentity.arn`
(`arn:aws:sts::111111111111:assumed-role/CATS-test-infra-prod-default-test-role/cats-20260924T105317Z`),
and in the parent account the matching `AssumeRole` events carry it in
`requestParameters.roleSessionName`. To list what one run did in an account under test:

```bash
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=Username,AttributeValue=cats-20260924T105317Z \
  --start-time 2026-09-24T10:53:17Z --end-time 2026-09-24T10:55:02Z
```

`Username` is how CloudTrail exposes the session name of an assumed role; the times are
`run.started_at` and `run.finished_at`. The same lookup works from the account's side
without the report: an account owner who sees `cats-20260924T105317Z` in their trail knows
which run's files to ask for. Those events also carry the `AwsCatsPack/...` token of every
pack in `run.test_packs` in their `userAgent` field, the same string the OSCAL
`cats-test-pack` prop holds.

`run.caller_identity` names the principal those roles were assumed from: the pipeline role
and job, or the SSO user, that ran `cats test`. It is `null` when no test used an account,
because then no role was assumed and there is nothing in any trail to match, and `null` with
a warning when `GetCallerIdentity` failed.

Four session names tell the ways CATS assumes a role apart: `cats-infra` is a deploy or
destroy (`cats infra`), `cats-<run_id>` is a reported test run, `cats-cli` is anything else
CATS itself does — `cats run`, the MCP tools, or an `AccountUnderTest` built in a script — and
`cats-profile` is a role assumed by your own AWS tool through a profile that `cats run
write-profile` or `cats_write_profile` wrote. A pytest run with `reporting.enabled: false` has
no run id and uses `cats-cli` too.

#### Signing the report files

CATS does not sign its files. The fields above say which run a file came from, what code
and configuration produced it and what it did in AWS; a signature adds who vouches for the
file itself, and that statement is best made by the process that produced it, with the keys
and verification tooling it already has. CATS is built to make that easy: with
`timestamped_files` every run writes each file exactly once under a name that never changes
afterwards, so the file a pipeline signs is the file an auditor later reads.

The usual shapes all work on a CATS report as they do on any build artifact:

- **Sign the file.** `cosign sign-blob report-20260924T105317Z.json` (Sigstore) produces a
  signature and, with keyless signing, a certificate naming the pipeline identity; `cosign
  verify-blob` checks both. The same file can carry a `.sig` from `gpg --detach-sign` or
  `openssl dgst -sign`.
- **Attest the build.** GitLab Runner's artifact provenance metadata
  (`RUNNER_GENERATE_ARTIFACTS_METADATA`), GitHub's artifact attestations, and in-toto/SLSA
  provenance in general all record that a given CI job produced a given artifact digest.
  Point them at the report directory and the report's provenance chain reads: the run
  (`run_id`), the code (`run.git.commit`), the runner (`run.caller_identity`), and the CI
  job that vouches for the file.
- **Store immutably.** An S3 bucket with Object Lock, or an artifact store that records the
  upload's digest, fixes the file at the moment it was produced; `sha256sum` of the stored
  copy against the digest in the attestation closes the loop.

Whichever you choose, sign the timestamped file, not the fixed-name copy: `report.json` is
rewritten by every run, so a signature on it is valid for one run and misleading afterwards.

## OSCAL Assessment Results

CATS writes `assessment-results.json` to the report directory. The top-level shape is:

```json
{
  "assessment-results": {
    "uuid": "…",
    "metadata": { "title": "CATS Assessment Results", "oscal-version": "1.2.1", "…": "…" },
    "import-ap": { "href": "" },
    "results": [
      {
        "local-definitions": { "inventory-items": [] },
        "reviewed-controls": { "control-selections": [] },
        "observations": [],
        "findings": []
      }
    ]
  }
}
```

### How CATS results map to OSCAL

One test run produces one `Result`. Within that result:

| CATS data | OSCAL target |
|-----------|--------------|
| One `ReportEntry` (one test, one account parameterization) | One `Observation` |
| Each resolved control ID on that entry | One `Finding`, linked back to the observation |
| `test_name` | `Observation.title` |
| `test_docstring` | `Observation.description` |
| `accounts_in_test[]` (deduplicated) | `InventoryItem` entries under `local-definitions.inventory-items`; the observation references them by `subject-uuid` |
| Account `tags` | `InventoryItem.props[]` |
| Resolved `control_ids` for each `standard_name` | `Finding.target.target-id`, and collected into `reviewed-controls.control-selections[].include-controls[]`. `Finding.target.type` is `objective-id`: OSCAL's enum offers only `statement-id` and `objective-id`, and CATS findings are about a control as a whole, so `objective-id` with the control id as target is the conventional reading |
| Resolved `control_ids` for the whole observation | `Observation.remarks` (space-joined), so OSCAL consumers that scan free-text for catalog IDs pick them up |
| Resolved `control_group` | `Observation.props[name="control-group"]` |
| pytest outcome | `Observation.props[name="result"]` with value `Pass`, `Fail`, `Error`, `N/A`, or `Not run` |
| `custom_markers`, `additional_metadata`, `xfail` | `Observation.props[]` |
| `error_message`, else the skip or xfail `reason` | `Finding.target.status.remarks` |
| pytest outcome, collapsed | `Finding.target.status.state` and `Finding.target.status.reason` (see the mapping below) |

Granularity: parameterized tests that touch multiple accounts produce one observation per parameterization, not one per test function. This keeps per-account evidence distinguishable in the OSCAL output.

A `not_run` test ([held back by `test_selection`](../guides/configuration.md#test-selection)) is an observation and
nothing more: `result` prop `Not run`, its standards as `cats-standard` props, and the reason as
`Observation.remarks`. No finding is produced, because a finding states a control's state and no
control was assessed, so its control ids are not in `reviewed-controls` either. The run states the
fact and the reason; it claims neither "satisfied" nor "not applicable".

### Pass/fail mapping

OSCAL findings use a binary `state`. CATS also sets `Finding.target.status.reason` (`pass`, `fail` or `other`) so an auditor can tell a failure from a test that never ran inside the finding itself, and surfaces the pytest outcome more precisely as an observation `result` prop so consumers can distinguish skipped and errored tests from genuine failures.

| CATS result | OSCAL state | `status.reason` | `result` prop |
|-------------|-------------|-----------------|---------------|
| `passed` | `satisfied` | `pass` | `Pass` |
| `xpassed` | `satisfied` | `other` | `Pass` |
| `failed` | `not-satisfied` | `fail` | `Fail` |
| `xfailed` | `not-satisfied` | `other` | `Fail` |
| `error` | `not-satisfied` | `fail` | `Error` |
| `skipped` | `not-satisfied` | `other` | `N/A` |
| `not_run` | no finding | — | `Not run` |

Skipped and expected-failure tests are treated as `not-satisfied` because they do not produce passing evidence for the control. The finding status `remarks` carry the `error_message` for failures and errors, and the skip or xfail `reason` otherwise, so the consuming workspace can explain each result. A `not_run` test has an observation with the reason as its remarks and no finding.

### Validating OSCAL output

OSCAL 1.2.1 JSON schemas are published at the [OSCAL releases page](https://github.com/usnistgov/OSCAL/releases). CATS's unit tests validate the writer's real output against a vendored copy of `oscal_assessment-results_schema.json` for that version, so a release cannot ship output the schema rejects. Schema validity is not the same as every consumer accepting the document: `compliance-trestle` and other tools apply their own rules on top, so validate against the tool you feed.

## JUnit XML

`junit` combines with any other format in `report_formats`. The file itself is written by pytest's own junitxml
plugin during the run; CATS switches it on and adds a property per standard, account, tag, marker and metadata key
to every testcase. CATS's own properties are namespaced: `cats:standard`, `cats:account` (`prod (111111111111)`),
`cats:tag:<key>`, `cats:xfail`, `cats:description`. A custom marker or `add_report_metadata` key keeps its own
name, so no CATS property can ever collide with one of yours. `junit.xml` follows `report_dir`, `latest_files` and
`timestamped_files` like every other report file. The testsuite carries the run's facts as properties: `cats:run_id`,
`cats:role_session_name`, `cats:config_sha256`, and, when known, `cats:caller_identity_arn`, `cats:git_commit`,
`cats:git_dirty` and one `cats:test_pack` per pack (see [The `run` block](#the-run-block)).

A test [held back by `test_selection`](../guides/configuration.md#test-selection) never reports to pytest, so CATS adds its
testcase itself: the same `classname`, `name` and `file`, `time="0.000"`, the same CATS properties, and
`<skipped type="cats.not_run" message="not run: <reason>"/>`. JUnit has no closer state than skipped; the `type`
tells it apart from `pytest.skip` and `pytest.xfail`, and it counts in the testsuite's `tests` and `skipped` totals
like any skip.

## Security Hub findings

### What a finding looks like

One finding per test per account in the test's `accounts_in_test`, so `test_prod_cannot_reach_dev(prod, dev)`
produces a finding in both prod's and dev's Security Hub, each naming every participant in
`ProductFields["cats/accounts_in_test"]`. A test with no account fixture has nowhere to go and is counted as
unattributed in the terminal summary and the findings file.

Which parts of this are covered by semver is stated on the
[versioning page](../concepts/versioning.md#security-hub): the `Id` scheme, the fixed `cats/*` `ProductFields`
keys and the `Compliance.Status`/`RecordState` mapping are; `Types` and the severity mapping are not.

| ASFF field | Value |
|---|---|
| `Id` | `cats/<cfn_stack_prefix><account keyname>/<cats_deployment_settings.aws_region>/<uuid5 of the node id>`: stable across runs, so a re-run updates the finding in place; namespaced so two CATS projects testing one account, or one suite run against two Regions, do not collide |
| `Title` / `Description` | the test name / its docstring (whitespace collapsed), else the test name |
| `GeneratorId` | the pytest node id |
| `Types` | `Software and Configuration Checks/Industry and Regulatory Standards/<standard>` per `@cats` standard, or `.../CATS` when the test has none |
| `Compliance.RelatedRequirements` | each standard, then its control ids resolved the same way the OSCAL writer resolves them (decorator, then `pytest_cats_standards` registry) |
| `Resources` | the account (`AWS::::Account:<id>`) in the Region the stack deploys into, with its `cats.yaml` tags |
| `ProductFields` | `cats/version`, `cats/run_id`, `cats/nodeid`, `cats/account_name`, `cats/aws_region` (the deploy Region), `cats/result`, `cats/accounts_in_test`, `cats/error_message` (failed and error), `cats/reason` (skipped and xfailed), `cats/marker/<name>`, `cats/metadata/<key>`. The message lives here rather than in `Compliance.StatusReasons`, whose `ReasonCode` the API restricts to AWS Config conditions |
| `ProductName` | `CATS` |
| `CreatedAt`, `UpdatedAt` | both the run's finish time. `BatchImportFindings` replaces the stored finding, so every import resets them: a control that has failed for a month reads as created today, and "open for more than N days" automation cannot use CATS findings. CATS does not read findings back to preserve the first-seen time; the `cats/run_id` history in your own report files is the record of how long a failure has stood |

| pytest result | `Compliance.Status` | `RecordState` | Severity |
|---|---|---|---|
| failed | `FAILED` | `ACTIVE` | `failed_severity` (default `HIGH`: a control that does not enforce) |
| error | `NOT_AVAILABLE` | `ACTIVE` | `MEDIUM` (evidence missing; needs attention) |
| xfailed | `FAILED` | `ACTIVE` | `LOW` (a known, accepted gap stays visible) |
| passed, xpassed | `PASSED` | `ARCHIVED` | `INFORMATIONAL` |
| skipped | `NOT_AVAILABLE` | `ARCHIVED` | `INFORMATIONAL` |
| not_run | not imported | — | — |

Passes are imported as `ARCHIVED` on purpose: the same import call that would raise a failure retires the previous
one, the default console view stays clean, and the evidence remains queryable (filter on `RecordState = ARCHIVED`
and `ProductName = CATS`). Skipped tests are still sent so a stale failure is retired when someone skips the test.

A `not_run` test ([held back by `test_selection`](../guides/configuration.md#test-selection)) is not imported. Security Hub
holds control state and the test produced none: a test the deployment does not provide for is a control that does
not apply to this account, and an excluded test is a risk acceptance, which in Security Hub's own vocabulary is
`Workflow.Status: SUPPRESSED` on the existing finding (yours to set, see below). The count is in the summary line
(`N tests not run (test_selection; not imported)`) and in the findings file's `not_run`. A finding left `ACTIVE` by a
test you have since excluded is handled like one for a deleted test, below.

A finding's `Id` is a function of the node id, so a test that is renamed, moved to another file or deleted, or
whose parametrize id changes, is never imported again under its old `Id`: its last finding stays as it was. For a
failing test that is an `ACTIVE`, `FAILED` finding in the member's and the administrator's Security Hub until Security
Hub deletes findings not updated for 90 days. To retire it sooner, re-import it as a pass (edit the old `Id`'s entry
in the findings file to `Compliance.Status: PASSED`, `RecordState: ARCHIVED` and replay it, see below), or archive it
in the console. `Workflow`, `Note` and `UserDefinedFields` are yours: CATS never sets them and a re-import never
changes them.

### What a finding contains and who can read it

The `securityhub` format is the one CATS output that leaves the machine running the tests. Each finding carries the
test's node id and docstring, the assertion message (`cats/error_message`, which in network tests names hosts, ports
and sometimes ARNs), every `add_report_metadata` value (`cats/metadata/*`), every custom marker value
(`cats/marker/*`) and the account's `cats.yaml` tags. It is readable by anyone with Security Hub access in the member
account, by the delegated administrator, in the aggregation Region, and by every EventBridge rule, SIEM export or
third-party integration that consumes that Security Hub. `report.json` holds the same data and stays on the runner;
enable `securityhub` only where that audience is the intended one.

#### What each finding carries

- `GeneratorId` and `ProductFields["cats/nodeid"]`: the test's node id.
- `Title`: the test name (`test_name`, function name plus parametrize id).
- `Description`: the test's docstring with whitespace collapsed, else the test name.
- `ProductFields["cats/error_message"]`: the assertion or error text of a failed test, truncated to 2048 characters.
- `ProductFields["cats/reason"]`: the skip, xfail or exclusion reason.
- `ProductFields["cats/marker/<name>"]`: every custom marker's values, comma-joined.
- `ProductFields["cats/metadata/<key>"]`: every `add_report_metadata` value.
- `ProductFields["cats/accounts_in_test"]`: the keynames of every account the test used.
- `Resources[0].Tags`: the account's `cats.yaml` tags (a valueless tag becomes `"true"`).
- `ProductFields["cats/run_id"]`, `["cats/version"]`, `["cats/aws_region"]`, `["cats/account_name"]`, `["cats/result"]`.
- `Types`: one per standard name (`/` in a name becomes `-`), or a single CATS type when the test names no standard.
- `Compliance.RelatedRequirements`: each standard name followed by its control ids, from the decorator and from the
  [standards registry](../guides/standards-registry.md): a registry entry adds identifiers to every finding for that
  standard without any change to the test.
- `Remediation.Recommendation`: fixed CATS text naming the run id and the report files.

ASFF limits are applied before import: `Title` 256, `Description` 1024 and `GeneratorId` 512
characters; `ProductFields` keys 128 and values 2048 characters; resource tag keys 128 and values 256
characters; at most 50 `ProductFields`, 50 `Types`, 32 `RelatedRequirements` and 50 resource tags.
Blank keys and values are dropped. The CATS fields are placed first, so the `ProductFields` cap
drops marker and metadata pairs, never a CATS field.

In a cross-account test the same finding, error text included, is imported into every account named in
`accounts_in_test`, so each of those accounts' Security Hub readers sees what the test observed about the others.

### The findings file

`securityhub-findings.json` is written **before** the first API call, holding exactly what will be sent with
`"delivery": null` for every account, and rewritten after the import with each account's outcome. It follows the
`report.json` rule: `schema_version` moves only with a breaking change, new keys are additive.

```json
{
  "schema_version": 1,
  "run_id": "20260924T105317Z",
  "cats_version": "1.0.0",
  "region": "eu-west-1",
  "aws_region": "us-east-1",
  "unattributed": 3,
  "no_standard": 12,
  "not_run": 4,
  "accounts": [
    {
      "account_name": "prod",
      "account_id": "111111111111",
      "role_arn": "arn:aws:iam::111111111111:role/CATS-test-infra-prod-default-test-role",
      "product_arn": "arn:aws:securityhub:eu-west-1:111111111111:product/111111111111/default",
      "delivery": {"status": "imported", "findings_imported": 42, "detail": null, "delivered_at": "2026-09-24T10:53:40.120000Z"},
      "findings": ["...ASFF AwsSecurityFinding objects, exactly as sent..."]
    }
  ]
}
```

`delivery.status` is one of `imported`, `not_enabled` (Security Hub is off in that account and Region; skipped),
`access_denied` (the import was still denied after 30 s of retries: the stack predates the format, so run
`cats infra deploy <account>`, or a deploy that just added the grant had not converged in IAM yet, so run again)
or `failed` (the role could not be assumed, the API rejected findings, or a call failed after retries); `detail`
carries the message. A file still holding `"delivery": null` after a run means the process died before anything
was confirmed sent. Any `access_denied` or `failed` account fails the run, like a writer that could not write
its file; `not_enabled` is a warning.

Import is idempotent by `Id`, so replaying an account's whole list is always safe: it updates what landed and
adds what did not. With credentials in the account (the test role, or any principal allowed
`securityhub:BatchImportFindings` on the account's default product ARN):

```bash
jq -c '.accounts[] | select(.account_name == "prod") | .findings | _nwise(100)' securityhub-findings.json \
  | while read -r batch; do aws securityhub batch-import-findings --region eu-west-1 --findings "$batch"; done
```

With `latest_files` and `timestamped_files` both `false` there is no file; the import still runs, the terminal
summary reports it, and a failure's error text says how to get a replayable copy.

### Scale and cost

Findings go 100 per API call, accounts concurrently, with adaptive retries. At the 100,000-test scale that is
about 1,000 calls per account against a rate-limited API, so expect the import to take a minute or two at the end
of the session. Every import, archived passes included, is a Security Hub finding-ingestion event; see the
[Security Hub pricing page](https://aws.amazon.com/security-hub/pricing/).

## The `reporting` configuration

All keys under `reporting:` in `cats.yaml`:

| Key | Type | Default | Purpose |
|-----|------|---------|---------|
| `enabled` | bool | `true` | Emit any reports at all. Setting `false` disables all writers. |
| `report_formats` | list of `cats_json` \| `oscal_ar` \| `junit` \| `securityhub` | `[cats_json]` | Which report writers to run, in order; at least one (an empty list is rejected: to write nothing, set `enabled: false`). List `securityhub` last: it makes network calls and the files before it never wait on them. |
| `custom_markers` | list of str | `[]` | Marker names permitted as `@cats(...)` keywords. Identifiers only; not a `@cats` keyword, a pytest built-in marker, or a `cats_*` name; no duplicates. |
| `oscal_import_ap_href` | str | `""` | Recorded as `import-ap.href` in the OSCAL document; point it at your assessment plan. |
| `securityhub.aws_region` | str | unset (`cats_deployment_settings.aws_region`) | Region whose Security Hub receives the findings. The test role's grant is scoped to it: changing it needs `cats infra deploy`. |
| `securityhub.failed_severity` | `CRITICAL` \| `HIGH` \| `MEDIUM` \| `LOW` \| `INFORMATIONAL` | `HIGH` | Severity of the finding for a failed test. |
| `securityhub.only_with_standard` | bool | `true` | Import findings only for tests that carry a standard; `false` imports every test that used an account fixture. |
| `report_dir` | str | unset (current directory) | Directory for every report file. Relative to the `cats.yaml` directory; `--cats-report-dir` overrides. |
| `latest_files` | bool | `true` | Write the fixed-name files, overwritten each run. |
| `timestamped_files` | bool | `false` | Also write one `<name>-<run_id>.<ext>` set per run, never overwritten. |

