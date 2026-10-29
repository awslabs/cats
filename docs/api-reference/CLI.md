# CLI

The `cats` command line. Commands, arguments, flags, defaults, exit codes and streams are
covered by the [compatibility contract](../concepts/versioning.md#cli). `cats --help` and
`cats <command> --help` are the reference for each command's arguments and flags.

## Commands

| Command | Purpose |
|---|---|
| `cats init` | Bootstrap a new `cats.yaml` and the other files a project needs. A no-op when `cats.yaml` exists unless `--force` is given |
| `cats validate` | Validate `cats.yaml` syntax and schema, and measure each account's CloudFormation template against the 51,200-byte limit (offline, no credentials). Regenerates `.schema.json` |
| `cats generate-types` | Generate `cats_types.py` with `Protocol` classes for typed fixtures |
| `cats test [args]...` | Run the CATS tests with pytest, defaulting to `pytest -vv -ra`. Any arguments replace the defaults and go to pytest |
| `cats infra deploy` | Deploy CATS infrastructure via CloudFormation to one or more accounts, or `--all`, in parallel |
| `cats infra destroy` | Destroy CATS infrastructure via CloudFormation. No confirmation prompt |
| `cats infra targets` | Show the accounts and network targets that deploy and destroy would affect |
| `cats infra status` | Show the CloudFormation stack status and the deploying CATS version per account |
| `cats infra show-template` | Show or export the CloudFormation template that would be deployed |
| `cats run account-info` | Runtime metadata from the stack outputs for one account: targets, handlers, EC2 DNS, tags, deployed CATS version |
| `cats run http-call` | HTTP call from an account's target network segment to a host and port |
| `cats run tcp-call` | TCP call from an account's target network segment to a host and port |
| `cats run udp-call` | UDP send from an account's target to a host and port |
| `cats run shell` | Run shell commands on an account's target EC2 instance via SSM. Requires `allow_shell: true` at deploy time |
| `cats run receive-s3` | Poll the receiving account's response bucket for a UDP receipt |
| `cats run write-profile` | Write an AWS profile that assumes an account's role from your current identity. Writes no credential |
| `cats prompts overview` | What CATS is and how it works, for an AI agent |
| `cats prompts setup` | How to configure and deploy CATS, for an AI agent |
| `cats prompts testing` | How to write CATS tests, for an AI agent |
| `cats prompts agentic` | Profile for agents connected to CATS over MCP |
| `cats mcp` | Start CATS as a Model Context Protocol server. Requires the `mcp` extra |

## `cats run` output

`cats run` is the shell surface over deployed infrastructure: every subcommand takes flags in
and prints one JSON object on stdout. Nothing is cached between invocations. The shapes below
are what this release emits; the body is [not covered](../concepts/versioning.md#not-covered)
by the compatibility contract, so read the keys you need and tolerate ones you do not know.
The same operations are MCP tools, listed in [MCP](MCP.md).

| Subcommand | Emits |
|---|---|
| `account-info` | `{"name", "account_id", "tags", "deployed_version", "targets"}`. `targets` maps each target name to `{"vpc_id", "subnet_ids", "enabled_handlers", "ec2": {"dns_name", "instance_id"}, "lambda": {"probe_lambda_name"} or null}` |
| `http-call`, `tcp-call` | `{"result", "reason", "http_status"}`, every key always present and `null` when absent. `result` values are defined in [Network Testing](../concepts/network-testing.md#response-values); `http_status` is the code on HTTP `SUCCESS` and `null` for TCP |
| `udp-call` | `{"result", "reason", "correlation_id"}`; `result` is `SENT`, `DNS_FAILURE` or `FAILURE`. When `--host` is a CATS listener, pass `--receiving-account` so the datagram carries that account's receipt token; without it the listener drops the datagram. On `SENT`, pair `correlation_id` with `receive-s3` on the receiving account |
| `receive-s3` | `{"received": true \| false}` |
| `shell` | `{"stdout", "stderr", "exit_code", "duration_ms", "stdout_truncated", "stderr_truncated"}`; needs `allow_shell: true` at deploy |
| `write-profile` | `{"profile_name", "profile_file", "role_arn", "parent"}` after writing the profile. `profile_file` is the real path written; `parent` is the `source_profile = ...` or `credential_source = ...` line that names the identity the role is assumed from |

Every failure `cats run` detects itself is `{"error": "..."}` on stderr with the exit code
above; a usage error typer detects (unknown flag, bad `--port`) is typer's usage text.

### `write-profile`

Writes an AWS profile that assumes a test role, into an AWS config file (`~/.aws/config` by
default), so the `aws` CLI, an SDK or an agent's AWS tool can act as the role. The profile holds
the role's ARN and the identity to assume it from — never a credential.

```bash
cats run write-profile --account prod            # writes [profile cats-prod-default]
aws sts get-caller-identity --profile cats-prod-default
```

```ini
[profile cats-prod-default]
role_arn = arn:aws:iam::111111111111:role/CATS-test-infra-prod-default-test-role
source_profile = my-parent
role_session_name = cats-profile
region = us-east-1
```

- The default name is `cats-<account>-<role>`; a custom `--profile-name` must start with `cats-`, so the command never overwrites a profile it did not create. The file must be an AWS config file (or not exist yet); anything else is refused untouched.
- `--profile-file` beats `AWS_CONFIG_FILE` beats `~/.aws/config`. A new file is created `0600`; an existing file keeps its permissions and every section other than the ones CATS owns, byte for byte. A file that already holds the stanza is not rewritten.
- The parent line is derived from how CATS itself resolved AWS credentials: a named profile (including SSO or a chained role) becomes `source_profile`; environment, instance-role or container credentials become `credential_source = Environment | Ec2InstanceMetadata | EcsContainer`; web-identity credentials taken from the environment are materialized as a `cats-parent-<account>` profile the role's profile sources.
- The AWS CLI and every SDK assume and refresh the role themselves, so the profile does not expire and `export AWS_PROFILE=cats-prod-default` is safe.

!!! note
    The profile contains no credential. When the AWS CLI assumes the role through it, the CLI
    caches the resulting temporary credentials under `~/.aws/cli/cache`; that is the CLI's own
    behavior for any `role_arn` profile. If you need the raw keys on stdout (for `eval` or a
    container `--env-file`), the AWS CLI prints them: `aws configure export-credentials
    --profile cats-prod-default --format env`.

## Exit codes

The same four codes on every command:

| Code | Meaning | Example |
|---|---|---|
| `0` | Success | `cats infra status` reporting every account, including one that is "not deployed" |
| `1` | The operation ran and failed | `cats infra deploy --all` where one account's stack rolled back; `cats infra status` where one account's role could not be assumed; `cats run http-call` where the AWS API call failed |
| `2` | Usage or configuration error | An unknown flag; a missing required option; `cats validate` on an invalid `cats.yaml`; `--config` pointing at a path that is not a file |
| `130` | Interrupted | Ctrl+C during `cats infra deploy` |

A batch (`--all`, or several account names) that mixes usage and operational failures
reports `2`: the configuration is the thing to fix first, and every failure is on stderr
either way.

`cats test` is the one exception: once it has handed over to pytest it returns pytest's
exit code unchanged (`0` all passed, `1` tests failed, `2` interrupted, `3` internal error,
`4` usage error, `5` no tests collected). pytest runs as a child process. A `SIGTERM` or
`SIGHUP` sent to `cats test` is passed to it unchanged, so the run ends as `pytest` ends under
the same signal: pytest handles neither, so it stops at once, finalizers do not run and no
report is written; `cats test` then returns `128 + N`, as a shell reports a child ended by
signal `N` (`143` for `SIGTERM`). GNU `timeout`, a terminal hangup and a shell-executor CI
runner signal the whole process group, so pytest receives the signal directly in any case. To
keep the reports when a run is cut short, interrupt the process group with `SIGINT`, which both
processes handle as Ctrl+C: `timeout --signal=INT 30m cats test` (`timeout` itself then exits
`124`; add `--preserve-status` to get `cats test`'s `2`), or `kill -INT -- -<pgid>`. The `kill`
form needs `SIGINT` not ignored: a background job of a non-interactive shell starts with it
ignored, and the signal then has no effect: the run continues to its normal end, so prefer
`timeout`, which installs its own handler and starts `cats test` with the default. A `SIGINT`
sent to the `cats` process alone does not reach pytest.

## Streams

Results to stdout, diagnostics to stderr.

- A **result** is what the command exists to produce: the JSON from `cats run`, the template
  from `cats infra show-template`, the tables from `cats infra status` and `cats infra
  targets`, the prompt text from `cats prompts`, the profile location from `cats run write-profile`.
- A **diagnostic** is everything else: failure text, warnings, progress, and the
  `{"error": ...}` document `cats run` emits on failure.

`cats infra deploy --all 2>errors.log` therefore captures every failure message, and
`cats validate >/dev/null` still shows why it failed.

An expected failure (a CATS error, or an AWS SDK error such as no credentials, an expired
token or an unreachable endpoint) is one `FAIL <message>` line; `cats run` reports the same
failures as its `{"error": ...}` document. `CATS_DEBUG=1` lets an AWS SDK error, including one
CATS has wrapped, propagate with its traceback instead, for the case where the request itself
was wrong.

## The config option

Every command that reads `cats.yaml` takes `--config <path>`, defaulting to the
`CATS_CONFIG` environment variable and then `./cats.yaml`. The short form is `-c` on every
command except `cats test`, which passes unknown arguments through to pytest, and pytest
owns `-c FILE`. A `--config` path that is not a file is a usage error (exit `2`), never an
empty configuration, and so is an absent default: `cats infra ...` and `cats generate-types`
refuse to run when neither `CATS_CONFIG` nor `./cats.yaml` names an existing file.
