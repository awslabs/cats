# Versioning and Compatibility

CATS follows [Semantic Versioning 2.0.0](https://semver.org/), binding from 1.0.0.
This page is the contract: it names every surface the promise covers, states the rule
each surface follows, and lists what is not covered.

The governing rule: **only the surfaces named on this page carry compatibility
guarantees; anything else CATS exposes, documented or not, may change in any release.**

Two definitions apply everywhere below:

- A **breaking change** to a covered surface requires a **major** version. Rename, removal,
  a change of type or meaning, and a change of default are breaking.
- An **additive change** to a covered surface is a **minor** version: a new export, a new
  optional field, a new command or flag, a new enum member, a new key in a document.
  Code written against the previous minor keeps working.

## Product stance

CATS records **every** test in the session in `report.json` and `junit.xml`. The `@cats`
decorator is optional metadata: a test that attests to no standard is a first-class case,
not a degraded one. Compliance outputs, the OSCAL findings and the Security Hub findings,
act only on tests that carry a standard.

## The compatibility contract

| Surface | Covered | Breaking change means |
|---|---|---|
| [Python exports](#python-exports): every name in `cats.__all__` | **Yes** | Rename, removal, signature change, behavior change of documented semantics |
| [Dynamic namespaces](#dynamic-namespaces): `account.<target>`, `account.role.<name>`, and their reserved-name rules | **Yes** | Adding a reserved name that a config could have used |
| [The `@cats` decorator](#the-cats-decorator): its three keywords and the custom-marker channel | **Yes** | Any new keyword; any change to the three |
| [Pytest plugin surface](#pytest-plugin-surface): fixture names and scheme, marker names, command-line options | **Yes** | Any change to the naming derivation; rename or removal of a fixture, marker or option |
| [`cats.yaml`](#catsyaml) | **Yes** | Field rename or removal, type narrowing, constraint tightening, semantic change, new required field, default change |
| [CLI](#cli): commands, arguments, flags, defaults, exit codes, streams | **Yes** | Rename or removal; changed default; changed exit-code or stream semantics |
| [`report.json`](#reportjson-and-the-report-files) and the report file naming scheme | **Yes** | Field removal or type change; renaming a file; a change to `schema_version` |
| [Security Hub](#security-hub): the finding `Id` scheme, the `cats/*` `ProductFields` keys, the status mapping | **Yes** | Any change to the `Id` scheme; rename or removal of a key; a change to the mapping |
| [Exceptions](#exceptions): the `CatsError` root rule | **Yes** | A deliberate raise that is not a `CatsError`; moving an error between the three caller-facing classes |
| [CloudFormation outputs](#cloudformation-outputs): the output key scheme | **Yes** | Key rename or removal; a change to the `{target}` suffix rule |
| Everything in [Not covered](#not-covered) | **No** | May change in any release |

## Python exports

The covered Python API is **exactly `cats.__all__`**. It is pinned by
`tests/unit_tests/test_contract_surface.py`; a change to that test is a contract change.
At 1.0.0 the list is:

| Group | Names |
|---|---|
| Objects | `AccountUnderTest`, `NetworkHandler`, `CatsReporter`, `CatsTestPack`, `StandardMetadata`, `CatsConfig` (the loaded `cats.yaml`, as the `pytest_cats_provides` hook receives it: its fields are the [configuration keys](../guides/configuration.md) and are covered; its methods are internal) |
| Result types | `CatsNetworkResponse`, `CatsNetworkResult`, `ShellResult` |
| Exceptions | `CatsError`, `CatsUsageError`, `CatsConfigError`, `CatsInfrastructureError`, `CatsVersionMismatchError`, `CatsSSMError`, `CatsLambdaInvocationError` |
| Decorator | `cats`, `cats_requires` |
| Helpers | `get_account_fixture_names`, `get_accounts_by_tag`, `GetAccountFixture` |
| Metadata | `__version__` |

Covered, for each name: the documented members, every attribute and method whose name
does not start with `_`, the keyword names of every parameter, and the positional order of
every positional parameter. Anything starting with `_` is internal.

- `AccountUnderTest(name)` is the constructor: one positional argument, the account
  keyname from `cats.yaml`. An unknown name raises `CatsConfigError`.
- `NetworkHandler` is constructed by `AccountUnderTest` and reached as `account.<target>`.
  Its `__init__` is not covered; do not construct it.
- `CatsNetworkResponse` members and `ShellResult` fields may be added in a minor; removing
  or renaming one is major.

## Dynamic namespaces

`AccountUnderTest.<target>` resolves the network targets declared in `cats.yaml`, so every
attribute of `AccountUnderTest` is a name a config cannot use. The rule that keeps the two
apart:

- The reserved single-word attributes of `AccountUnderTest` are frozen at exactly
  **`name`, `partition`, `session`, `role`, `tags`**. A target with one of these names is
  rejected when the config loads.
- **Every attribute added to `AccountUnderTest` after 1.0.0 contains an underscore.**
  Target names cannot contain one, so no future attribute can collide with a target.
- `account.role` (`SessionHandler`) has **one** public name, **`default`**. A role's name (its
  key in `additional_roles`) may not start with `_`; everything on `SessionHandler` that is
  not `default` does.
- Case-only collisions are rejected: two account keynames, or two target names in one account,
  that differ only in case are a config error. Two role names in one account must differ by
  more than case and underscores: IAM role names are case-insensitive, and the CloudFormation
  logical ID drops `_`.

## The `@cats` decorator

- The keyword set is frozen at **`standard_name`, `control_ids`, `control_group`**. `@cats`
  never gains a keyword.
- `standard_name` is optional. `@cats(team="platform")` on a test that attests to nothing
  is first-class: the marker is recorded, and no standard is attached.
- Every other keyword is a custom marker. It must be declared in `reporting.custom_markers`
  in `cats.yaml`; an undeclared keyword raises `CatsUsageError` when the test module is
  imported.
- Future per-test behavior never arrives as a `@cats` keyword. It is configured in
  `cats.yaml` against a user-declared marker, or it arrives as a new `cats_*` pytest
  marker. `@cats_requires(*names)` is the first: a sibling decorator that applies one
  `cats_requires(name=...)` mark per requirement, read by `test_selection` in `cats.yaml`.
  Its positional arguments are free-form requirement names; it takes no keyword.

## Pytest plugin surface

- **Account fixtures** are named `<keyname>_account`, one per account in `cats.yaml`. No
  CATS fixture added after 1.0.0 ends in `_account`.
- **CATS's own fixtures** are `cats_report` and `get_account_fixture`.
- **Markers.** Every marker CATS registers is named `cats_*`. `@cats` emits
  `cats_standard(name, control_ids, control_group)` and `@cats_requires` emits
  `cats_requires(name)`; select on either with `-m "cats_standard(name='SOC2')"` or
  `-m "cats_requires(name='container-deployment')"`. A custom marker declared in
  `reporting.custom_markers` may not start with `cats_`. `test_selection.excluded` names the two
  engine markers as they are written on the test, `standard_name` and `cats_requires`, so it
  reserves no custom-marker name. Every
  junit property CATS writes is prefixed `cats:`
  (`cats:standard`, `cats:account`, `cats:tag:<key>`, `cats:xfail`, `cats:description`, and
  `cats:run_id`, `cats:role_session_name`, `cats:caller_identity_arn`, `cats:git_commit`,
  `cats:git_dirty`, `cats:config_sha256` and `cats:test_pack` on the testsuite); a
  custom marker's or metadata key's property keeps the user's name. A `not_run` testcase's
  `<skipped>` element has `type="cats.not_run"`.
- **Hooks** a conftest may implement: `pytest_cats_standards`, `pytest_cats_test_pack`,
  `pytest_cats_provides(cats_config)`. Parameter names are part of the contract (pluggy
  matches implementers by name).
- **Command-line options** the plugin adds to pytest: `--cats-report-dir <dir>`.

## `cats.yaml`

The schema is covered. The rules:

- **Breaking (major):** field rename or removal, type narrowing, constraint tightening,
  semantic change, a new required field, **a change of default**.
- **Additive (minor):** a new optional field, type widening, constraint relaxation.
- Keys are **case-sensitive**, at every level including the top-level sections.
- `aws_region` is required unless the `AWS_REGION` environment variable is set. CATS never
  picks a Region for you.
- `enabled_network_handlers` needs at least one handler wherever it appears. A declared
  target exists to be probed; an account with nothing to probe omits
  `account_network_under_test`.
- `subnet_id` is an input-only spelling for a single subnet. It is folded into
  `subnet_ids` on load, and a dumped config emits `subnet_ids` alone.

## CLI

Commands, arguments, flags, defaults, exit codes and streams are covered. The command
reference is [CLI](../api-reference/CLI.md).

**Exit codes**, defined once for every command:

| Code | Meaning |
|---|---|
| `0` | Success. For `cats infra status`, "not deployed" is a successful answer |
| `1` | The operation ran and failed: a stack failed to deploy, an account could not be read, an AWS call failed |
| `2` | Usage or configuration error: unknown flag, missing required option, invalid `cats.yaml`, a `--config` path that is not a file |
| `130` | Interrupted (Ctrl+C) |

A batch that mixes usage and operational failures reports `2`.

The one exception is the test run itself: once `cats test` has handed over to pytest, it
returns pytest's exit code unchanged (`0` all passed, `1` tests failed, `2` interrupted,
`3` internal error, `4` usage error, `5` no tests collected), or `128 + N` when the pytest
process was ended by signal `N` before it could exit (`143` for `SIGTERM`, the usual result
of a cancelled run; [CLI reference](../api-reference/CLI.md#exit-codes)).

**Streams:** results to stdout, diagnostics to stderr. A result is what the command
exists to produce: JSON from `cats run`, a template from `cats infra show-template`, a
table from `cats infra status`. Failure text, warnings and progress are diagnostics.

**The config option** is spelled `--config` on every command that takes one, with `-c` as
the short form everywhere except `cats test`, which passes unknown arguments to pytest, and
pytest owns `-c`.

## `report.json` and the report files

- `report.json` has three top-level keys: `schema_version`, `run` and `tests`.
  `schema_version` is `1` at 1.0.0 and increments **only** with a breaking change to the
  document. Additive fields are minor and do not move it. The corollary for a consumer: a
  reader must tolerate keys it does not know (no strict schema, no closed deserializer), because
  new keys arrive in minor releases.
- **Entry identity** is the pytest node id (`tests/test_x.py::test_y[param]`) relative to
  pytest's rootdir. `cats init` pins the rootdir with an ini file at the project root: a
  `[tool.pytest.ini_options]` section in an existing `pyproject.toml`, else a `pytest.ini`.
  The node id is the key of every entry, the sort order of the file, and the seed of every
  OSCAL and Security Hub identifier.
- **Results** are `passed`, `failed`, `skipped`, `xpassed`, `xfailed`, `error` and `not_run`.
  The six pytest outcomes are frozen. `not_run` is a test `test_selection` held back, recorded
  with its reason; it counts in `counts.total` and `counts.not_run`, and `run.selection` carries
  `provided`, `config_excluded` and `unmatched_exclusions` beside the command-line selectors.
  A new result value would be major.
- **Timestamps.** Every timestamp CATS owns (`started_at`, `finished_at`, `recorded_at`,
  the OSCAL metadata) is RFC 3339, UTC, microsecond precision, `Z` suffix:
  `2026-09-24T10:53:17.000000Z`. `run_id` is `started_at` to the second in basic form,
  `20260924T105317Z`; a second session starting in the same second against the same
  directory gets a `-2` (`-3`, ...) suffix so no file is overwritten.
- **File names.** Four files, each with a fixed name that always holds the newest run and a
  timestamped name that is written once: `report.json` / `report-<run_id>.json`,
  `assessment-results.json` / `assessment-results-<run_id>.json`, `junit.xml` /
  `junit-<run_id>.xml`, `securityhub-findings.json` / `securityhub-findings-<run_id>.json`.
  `reporting.latest_files` and `reporting.timestamped_files` choose which are written.

## Security Hub

Covered, because a finding `Id` is sticky for 90 days in the customer's hub:

- The finding `Id` scheme: `cats/{cfn_stack_prefix}{keyname}/{aws_region}/{uuid5(nodeid)}`.
- The fixed `ProductFields` key names: `cats/version`, `cats/run_id`, `cats/nodeid`,
  `cats/account_name`, `cats/aws_region`, `cats/result`, `cats/accounts_in_test`,
  `cats/error_message`, `cats/reason`, and the prefixes `cats/marker/<name>` and
  `cats/metadata/<key>`. Rename or removal is major; a new key is minor.
- The result mapping:

    | pytest result | `Compliance.Status` | `RecordState` |
    |---|---|---|
    | failed | `FAILED` | `ACTIVE` |
    | error | `NOT_AVAILABLE` | `ACTIVE` |
    | xfailed | `FAILED` | `ACTIVE` |
    | passed, xpassed | `PASSED` | `ARCHIVED` |
    | skipped | `NOT_AVAILABLE` | `ARCHIVED` |
    | not_run | not imported | — |

- A `not_run` test is never imported: it produced no control state. Its count is in the
  summary line and in `securityhub-findings.json` as `not_run`.
- `securityhub-findings.json` carries `schema_version` and follows the `report.json` rule:
  additive is minor, `schema_version` moves only for a breaking change.
- `securityhub.only_with_standard` defaults to `true`: only tests carrying a standard become
  findings, per the product stance above.

Not covered: the `Types` strings and the `Severity` mapping.

## Exceptions

Everything CATS raises deliberately is a `CatsError`. Callers see three classes for their own
mistakes and their environment:

| Class | Raised for |
|---|---|
| `CatsUsageError` | A bad argument: a URL where a hostname was expected, an invalid port, an undeclared `@cats` keyword |
| `CatsConfigError` | Your configuration: an unknown account name, a feature not enabled, an invalid `cats.yaml` (pydantic's detail is embedded in the message) |
| `CatsInfrastructureError` | AWS infrastructure missing or failing: no stack, another release's stack (`CatsVersionMismatchError`), and credential or endpoint failures at the session boundary |

`CatsTestPack` is a pydantic model; constructing it with invalid values raises pydantic's
`ValidationError`, not a `CatsError`.

## CloudFormation outputs

The output keys of a CATS stack are covered so that tooling which reads stack outputs can
rely on them. CATS itself reads outputs only from a stack of its own release (see below), so
the scheme protects external readers, not CATS.

Three fixed keys per stack:

| Key | Value |
|---|---|
| `TestRoleArn` | ARN of the test role. Read by the deploy readiness gate, which assumes it to prove it usable; `cats test` rebuilds the name from the configuration instead |
| `ResponseBucketName` | The S3 bucket UDP receipts are written to |
| `UdpReceiptToken` | The token a UDP datagram carries for the listener to record a receipt |

Six keys per network target, each suffixed with the target name exactly as written in
`cats.yaml` (`{Key}{target}`, so target `private` gives `Ec2DnsNameprivate`):

| Key | Value |
|---|---|
| `Ec2DnsName{target}` | Private DNS name of the EC2 listener |
| `Ec2InstanceId{target}` | Instance ID of the EC2 listener |
| `VpcId{target}` | The resolved VPC ID |
| `SubnetIds{target}` | The resolved subnet IDs, comma-joined |
| `EnabledHandlers{target}` | The deployed handlers, comma-joined (`ec2` or `ec2,lambda`) |
| `ProbeLambdaName{target}` | The probe function's name. Present only when `lambda` is an enabled handler |

Template internals behind the outputs are not covered.

## Every release recreates the stacks

`cats infra deploy` tags every stack `cats:version=<installed release>`. A stack deployed by
any other release, patch releases included, is refused at test time (`cats test`, `cats run`,
the MCP tools) and at deploy time, with the same instruction:
`cats infra destroy <account> && cats infra deploy <account>`. A stack update never replaces
deployed code, so recreation is the only path across a release boundary.

This is by design and is **not** a breaking change of any release. `CATS_SKIP_VERSION_CHECK=1`
downgrades the test-time refusal to a warning for diagnosis; deploy has no bypass.

## Not covered

These may change in any release:

- MCP tool names, arguments and schemas.
- The JSON body `cats run` emits.
- The OSCAL Assessment Results document shape.
- The hookspecs `pytest_cats_standards` and `pytest_cats_test_pack`. The `CatsTestPack`
  class they return is covered as an export; the hooks are not.
- `reason` text in `report.json` entries and on `CatsNetworkResult`: human-readable, no
  stable format.
- Security Hub `Types` strings and the `Severity` mapping.
- CloudFormation template internals: resource shapes, logical IDs, instance user data, SSM
  document content, Lambda source.
- Stack names and role names.
- Generated types from `cats generate-types`: regenerate after every upgrade.
- Internal modules: anything not in `cats.__all__`, and anything whose name starts with `_`.

## Deprecation cycle

- Breaking changes to any covered surface require a major version.
- Deprecations are felt, not just filed: a runtime `DeprecationWarning` at the point of
  use, plus a documented migration path in the changelog. Deprecated config fields warn
  at config load; deprecated CLI flags warn on use.
- A deprecated interface survives **at least one minor release** with its warning before
  removal, and removal happens only in a major.
- A changelog entry alone does not constitute a deprecation.

## Python version support

- CATS supports **every CPython version that has not reached upstream end-of-life**,
  subject to the code's genuine floor. At 1.0.0 that is a **3.11 floor**, and the supported
  set is **3.11, 3.12, 3.13 and 3.14**, each in CI.
- A new CPython version is added to CI and declared supported within **three months of
  its GA**.
- A Python version is dropped in a **minor** release, no earlier than its upstream EOL
  date, announced in the changelog. Dropping a Python version is not a breaking change
  and does not require a major.
