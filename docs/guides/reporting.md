# Reporting

Every run writes evidence in the formats you list under `report_formats`.

The idea behind reporting is in [Reporting and Compliance Alignment](../concepts/reporting.md); every
field of every file is in the [Reports reference](../api-reference/Reports.md).

## Output formats

The `report_formats` setting controls which writers run at the end of a test session. It is a list — CATS emits every format you list, in order. The default is `[cats_json]`: `report.json` alone.

```yaml
reporting:
  report_formats: [cats_json]                    # default — report.json only
  # report_formats: [oscal_ar]                   # OSCAL assessment-results.json only
  # report_formats: [cats_json, oscal_ar, junit] # all three files
  # report_formats: [cats_json, securityhub]     # report.json, plus findings in each account's Security Hub
```

| Format | Output file | Purpose |
|--------|-------------|---------|
| `cats_json` | `report.json` | CATS-native format. Easy to consume from scripts and dashboards. |
| `oscal_ar` | `assessment-results.json` | OSCAL 1.2.1 Assessment Results. Consumable by [compliance-trestle](https://oscal-compass.dev/compliance-trestle/) and other OSCAL tooling. |
| `junit` | `junit.xml` | JUnit XML for CI systems, with CATS properties on every testcase — see [JUnit Output](#junit-output). |
| `securityhub` | `securityhub-findings.json` | One finding per test in each account's AWS Security Hub, imported through the CATS test role; the file records what was sent and whether it landed — see [Security Hub](#security-hub-securityhub). |

## Report directory and per-run files

Every report file goes to one directory, and every run gets an id.

```yaml
reporting:
  report_dir: reports        # relative to the directory holding cats.yaml
  latest_files: true         # default: report.json, assessment-results.json, junit.xml
  timestamped_files: true    # default false: report-<run_id>.json, assessment-results-<run_id>.json, junit-<run_id>.xml
```

`report_dir` defaults to the current working directory. A relative value resolves against
the directory that holds `cats.yaml` (like `inline_policies` file paths), so the evidence
lands in the same place wherever you run `cats test` from. `--cats-report-dir` on the
command line overrides it and is relative to where you ran the command, like any CLI path.
Add the directory to your `.gitignore`.

The two naming flags are independent:

| `latest_files` | `timestamped_files` | Files written |
|---|---|---|
| `true` | `false` | The fixed names only, overwritten on every run (the default). |
| `true` | `true` | The fixed names, plus one immutable set per run that is never overwritten. |
| `false` | `true` | One set per run only. Point CI at `junit-*.xml`. |
| `false` | `false` | No report files. `securityhub` still imports its findings; `enabled: false` is the switch that disables every writer. |

With both flags on, the timestamped file is written first and the fixed name is a copy of
it, so `report.json` is always the newest run and consumers that read fixed names need no
change. `cats init` writes `report_dir: reports` and `timestamped_files: true` into new
projects; a config without the keys writes the fixed names alone, into the current directory.

The run id is `YYYYMMDDTHHMMSSZ` in UTC (no colons, so it is a valid filename everywhere,
and it sorts). A second run that starts in the same second gets a `-2` suffix rather than
overwriting anything. The same id is recorded inside every file of the run — as a
`cats-run-id` metadata prop in the OSCAL document and a `cats:run_id` testsuite property in
`junit.xml` — so the files of one run can be matched without trusting file timestamps.

At the end of every run pytest prints a `CATS reports` section naming the run id, the
counts, the directory and every file written. What narrowed the run (`-k`, `-m`, `--lf`,
`--deselect`, the paths or node ids on the command line) and whether it stopped early
(`-x`, `--maxfail`, an interrupt) is recorded in `run.selection` and `run.pytest_args` in
`report.json`. CATS records it and does not judge it: whether a run was the whole suite is a
question about the repository and the pipeline at that commit, which those records let a
reader answer. With `timestamped_files` off, the fixed-name files hold the latest run,
whatever it covered.

A test held back by [`test_selection`](configuration.md#test-selection) in cats.yaml leaves a
fuller record than a command-line selector does. It is in scope by the command line and out of
scope by configuration, and the report says so test by test: the entry is there with
`result: "not_run"` and the reason, in every format, where a test `-k` or `-m` removed leaves
no entry at all. The same section of the summary names the scope
(`test_selection: provides container-deployment; 3 tests not run (2 excluded, 1 not provided), ...`)
and, in yellow, any `excluded` entry that matched no test.

## CATS JSON (`cats_json`)

The native CATS report, `report.json`: a `run` block with the provenance of the evidence and one entry
per test. Gate a pipeline with `jq -e '.run.counts.failed == 0' report.json`; the field tables are in the
[Reports reference](../api-reference/Reports.md#reportjson).

## OSCAL Assessment Results (`oscal_ar`)

CATS can emit results as a valid OSCAL 1.2.1 [Assessment Results](https://pages.nist.gov/OSCAL/concepts/layer/assessment/assessment-results/) document so they can be imported directly into a compliance-trestle workspace (`trestle import`) or consumed by other OSCAL tooling.

Enable it alongside or instead of the CATS JSON output:

```yaml
reporting:
  report_formats: [cats_json, oscal_ar]
```

How each entry becomes an observation and each control a finding is in the
[Reports reference](../api-reference/Reports.md#oscal-assessment-results).

### Resolving standard names to controls

The `standard_name` on a `@cats(...)` marker is just a string — whatever label the test author chose. It appears verbatim in OSCAL findings as the `target-id`, so if you only care about one catalog and are happy to label tests with its IDs directly (`@cats(standard_name="AC-3")`), you are already done.

If instead you want to label tests with something friendlier (a short internal name, a vendor catalog ID) and still produce findings against a specific control catalog, you have two options:

1. **Per-test metadata on the decorator** — for small projects or one-off overrides:

   ```python
   @cats(
       standard_name="ACME-G-DCP-1",
       control_ids=["SC-7", "AC-3"],
       control_group="Governance",
   )
   def test_dcp_enforced(prod_account: AccountUnderTest) -> None: ...
   ```

2. **A registry** — for workbook- or catalog-driven setups where the same mapping applies to many tests. Implement the `pytest_cats_standards` hook in your `conftest.py`. See the [Standards registry guide](standards-registry.md) for the contract and worked examples (Excel workbook, CSV, in-memory, hybrid).

Decorator values win per field; the registry fills any gaps; if both are empty for `control_ids`, the `standard_name` itself is used as a single control ID.

## The @cats Decorator

Mark tests with compliance metadata:

```python
from cats import cats, AccountUnderTest


@cats(standard_name="SOC2-CC6.1")
def test_encryption(prod_account: AccountUnderTest) -> None:
    """Verify encryption at rest."""
    pass
```

`@cats` is optional metadata. CATS records every test in `report.json` and `junit.xml`
whether or not it is decorated; the compliance writers (OSCAL findings, Security Hub findings)
act only on tests that carry a standard.

Every option is in the [Decorator reference](../api-reference/Decorator.md).

### Examples

```python
@cats(standard_name="ISO-27001-A.10.1")
def test_crypto_controls() -> None:
    pass


@cats(standard_name=["PCI-DSS-3.4", "SOC2-CC6.1"], control_ids=["SC-28"])
def test_encryption_at_rest() -> None:
    pass


@cats(team="platform")  # no standard: recorded in report.json, not a compliance finding
def test_service_healthcheck() -> None:
    pass


@cats(standard_name="NIST-AC-2", team="platform", priority="high")
def test_access_control() -> None:
    pass
```

## Custom Markers

Define custom markers in config:

```yaml
reporting:
  custom_markers:
    - team
    - priority
    - ticket
```

Use in tests:

```python
@cats(standard_name="SOC2-CC6.1", team="security", priority="critical", ticket="SEC-123")
def test_critical_control() -> None:
    pass
```

In `report.json` these appear under each entry's `custom_markers` key. In the OSCAL writer they appear as `Observation.props[]` with the marker name as `name` and the value as `value`.

A custom marker name must be a Python identifier (it is a keyword of `@cats`) and may not be a
`@cats` keyword (`standard_name`, `control_ids`, `control_group`), a pytest built-in marker
(`skip`, `skipif`, `xfail`, `parametrize`, `usefixtures`, `filterwarnings`), start with `cats_`
(reserved for CATS's own markers, such as `cats_standard`), or appear twice. `cats validate`
rejects each with a message naming the rule.

## JUnit Output

Enable JUnit XML for CI integration by listing `junit` as a report format:

```yaml
reporting:
  report_formats: [cats_json, junit]
```

Only the `junit` format adds the CATS properties. pytest's own `--junit-xml=PATH` writes a plain junit.xml
without a CATS property on any testcase; when both are given, `--junit-xml` decides the path and the
project's own `junit_family` applies (pytest's default is `xunit2`; CATS forces `xunit1` only for the
file it names itself).

The properties CATS adds to every testcase are listed in the
[Reports reference](../api-reference/Reports.md#junit-xml).

## Security Hub (`securityhub`)

CATS proves controls by testing them; the `securityhub` format puts the result where control state is already
watched. Every test becomes a finding in the AWS Security Hub of each account it ran against, imported through that
account's CATS test role, so member-account findings surface in the delegated administrator (and in the home Region
under cross-Region aggregation) with no admin-side write. A failing `cats test` shows up as an `ACTIVE` finding in
the account it concerns; the next passing run archives it. The Region the findings go to need not be the Region
the stack deploys in: `securityhub.aws_region` is where the account's Security Hub actually lives.

```yaml
reporting:
  report_formats: [cats_json, securityhub]   # list securityhub last: report.json never waits on the import
  securityhub:
    aws_region: eu-west-1      # optional; default cats_deployment_settings.aws_region
    failed_severity: HIGH      # optional; CRITICAL | HIGH | MEDIUM | LOW | INFORMATIONAL
    only_with_standard: true   # optional; false also imports tests that carry no standard
```

Only tests that carry a standard (`@cats(standard_name=...)`) become findings: a compliance finding needs a
control to attest to, and `report.json` and `junit.xml` still record every test. The tests left out are counted
as `no_standard` in the terminal summary and the findings file. Set `only_with_standard: false` to import every
test that used an account fixture, typed `.../CATS` when it has no standard. Either way, removing `@cats` from a
test stops updating its finding, which stays `ACTIVE` in Security Hub until the 90-day retention deletes it.

Enabling the format changes the test role: it gains `securityhub:BatchImportFindings` on the account's own default
product ARN in the configured Region, and nothing else. Run `cats infra deploy` after adding it (a stack update, not
a recreate); until then the import fails with a message naming that command. Security Hub must be enabled in each
account and Region you import into; an account where it is not is skipped with a warning, not a failure.

What a finding contains, the findings file and the cost model are in the
[Reports reference](../api-reference/Reports.md#security-hub-findings).

## Filtering by Standard

Run only tests for a specific standard:

```bash
cats test -- -m "cats_standard(name='SOC2-CC6.1')"
```

Run tests matching multiple standards:

```bash
cats test -- -m "cats_standard(name='SOC2-CC6.1') or cats_standard(name='ISO-27001')"
```

A filtered run's report files hold only the selected tests, and `run.selection` in
`report.json` records the filter (`markexpr` here). With `timestamped_files: true` every
run's files are kept under their run id; with the fixed names alone, `report.json` is the
latest run, whatever it covered.

To leave a standard out of every run and keep a record of why, exclude it in cats.yaml with
a reason; where `-m` drops the test from the report, the excluded test stays in it as `not_run`:

```yaml
test_selection:
  excluded:
    - marker: standard_name
      values: [SOC2-CC6.1]
      reason: "Covered by the SOC 2 Type II report; not re-tested here"
```

See [Test selection](configuration.md#test-selection). The same mechanism holds back a
whole category of tests by a custom marker (`mutating: [true]` during a change freeze), and
`provides` runs the tests a deployment variant has in addition to the base set.

## Compliance Mapping Example

Map tests to a compliance framework:

```python
# tests/compliance/soc2.py

from cats import cats, AccountUnderTest


@cats(standard_name="SOC2-CC6.1")
def test_mfa_enabled(prod_account: AccountUnderTest) -> None:
    """CC6.1: Verify MFA is required for console access."""
    pass


@cats(standard_name="SOC2-CC6.6")
def test_network_segmentation(prod_account: AccountUnderTest) -> None:
    """CC6.6: Verify network segmentation between environments."""
    pass


@cats(standard_name="SOC2-CC7.2")
def test_cloudtrail_enabled(prod_account: AccountUnderTest) -> None:
    """CC7.2: Verify audit logging is enabled."""
    pass
```

## Configuration

Every key under `reporting:` is in the [Reports reference](../api-reference/Reports.md#the-reporting-configuration).

## Disabling Reports

```yaml
reporting:
  enabled: false
```

This disables all writers including the OSCAL writer, JUnit output and the Security Hub import.
