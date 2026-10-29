# Configuration Guide

Every field is documented in the [Configuration Reference](../api-reference/Configuration.md).

## File Location

CATS looks for `cats.yaml` in the current directory by default. Override with:

```bash
CATS_CONFIG=/path/to/cats.yaml cats test
```

Under pytest, if the current directory has no `cats.yaml`, the plugin also looks in pytest's
rootdir (the directory holding `pyproject.toml`, `pytest.ini` or `tox.ini`), so running from a
subdirectory or an IDE finds the project's file. When neither location has one, collection
continues with no account fixtures; a test that requests `<name>_account` fails as "fixture not
found", and a `PytestWarning` in the summary names the file CATS looked at and the accounts it
declares.

## Minimal Configuration

Single-account testing:

```yaml
cats_deployment_settings:
  allow_deploy_to_current_account: true
  parent_account_id: "123456789012"
  aws_region: us-east-1

accounts_under_test:
  myaccount:
    account_id: "123456789012"
```

`parent_account_id` is the account whose identities the test roles trust — the account
running CATS. It has no default: `cats infra deploy` and `cats infra show-template`
refuse with a clear error when it is not set, so a trust policy can never be rendered
with an account nobody chose.

## Multi-Account Configuration

Hub-and-spoke with network testing:

```yaml
cats_deployment_settings:
  parent_account_id: "999999999999"
  deploy_role_name: AWSControlTowerExecution
  aws_region: us-east-1

global_network_settings:
  enabled_network_handlers: [ec2]
  tcp_listen_ports: [443, 8080]

accounts_under_test:
  prod:
    account_id: "111111111111"
    account_network_under_test:
      private:
        vpc_id: vpc-abc123
        subnet_ids: [subnet-111]
  
  dev:
    account_id: "222222222222"
```

## Config Sections

| Section | Purpose |
|---------|---------|
| `cats_deployment_settings` | How CATS deploys infrastructure |
| `global_network_settings` | Default network config for all accounts |
| `global_account_settings` | Default account settings (test roles, `allow_shell`, fixture scope) for all accounts |
| `accounts_under_test` | Target accounts and their network targets |
| `reporting` | Test report options: which formats to write (`cats_json`, `oscal_ar`, `junit`, `securityhub`), where, and the `securityhub` Region and severity. See [Reporting](reporting.md). |
| `test_selection` | Which tests run against this deployment: what it `provides` for tests marked `@cats_requires`, and the tests `excluded` with a reason. Held-back tests are recorded as `not_run` with the reason. See [Test selection](#test-selection). |

## Key rules

- Keys are case-sensitive at every level: `REPORTING:` is an unknown key, not `reporting:`.
- Account keynames and network target names may not differ only by case within their scope
  (`prod` and `Prod`): IAM role names are case-insensitive, so they would collide at deploy.
- Role names in one account must differ by more than case and underscores (`read_only` and
  `readonly`): IAM role names are case-insensitive, and the CloudFormation logical ID drops `_`.
- A role in `additional_roles` declares at least one of `managed_policies` and
  `inline_policies`; `readonly: {}` is refused.
- `enabled_network_handlers` names at least one handler wherever it appears (global, account
  or target level): a declared target exists to be probed.
- A network target gives `subnet_ids` (a list) or the single-subnet spelling `subnet_id`,
  not both. `subnet_id` is input only; the loaded configuration carries `subnet_ids` alone.
- `vpc_id`, every `subnet_ids` entry and every `existing_security_group_ids` entry is either the
  literal AWS id (`vpc-…`, `subnet-…`, `sg-…`) or an SSM parameter name starting with `/`. A bare
  parameter name such as `my-vpc` is rejected at `validate`.

## Fixture Naming

Config keys become fixture names:

```yaml
accounts_under_test:
  prod:           # → prod_account
  shared_services: # → shared_services_account
```

Network targets become attributes:

```yaml
account_network_under_test:
  private:        # → prod_account.private
  public:         # → prod_account.public
```

A target name may not be `name`, `partition`, `session`, `role` or `tags`, the single-word
attributes of `AccountUnderTest`. Every other attribute of `AccountUnderTest` contains `_`, which a
target name cannot, so a name that validates today keeps validating.

Additional roles are keyed the same way, and the key becomes the attribute:

```yaml
additional_roles:
  readonly:       # → prod_account.role.readonly
  audit_reader:   # → prod_account.role.audit_reader
```

A role name is a Python identifier; `default` is reserved, and a name may not start with `_`
because `SessionHandler` has one public name, `default`, and keeps everything else
underscore-private.

## Settings Inheritance

Network settings cascade: `global_network_settings` → account `network_settings` → target `network_settings`.

```yaml
global_network_settings:
  tcp_listen_ports: [443]        # Default

accounts_under_test:
  prod:
    network_settings:
      tcp_listen_ports: [443, 8080]  # Override for account
    account_network_under_test:
      special:
        network_settings:
          tcp_listen_ports: [22]     # Override for target
```

To disable an inherited setting, use empty list:

```yaml
network_settings:
  tcp_listen_ports: []  # No TCP ports
```

### `allow_shell`

Off by default. Set `allow_shell: true` on `global_account_settings` or per-account `account_settings` to enable `NetworkHandler.shell()`, which runs arbitrary shell commands on CATS EC2 instances through SSM run-command. Commands run as root with the EC2 instance's IAM role, not the test role. This is a deploy-time setting — changing it in `cats.yaml` has no effect until `cats infra deploy` runs again. See [security](../security.md#shell-access-allow_shell) for implications.

```yaml
global_account_settings:
  allow_shell: true  # Enables shell() for all accounts unless overridden
```

### `existing_security_group_ids`

By default CATS creates its own security groups per network target: one for the EC2 listener with ingress scoped to the target's `tcp_listen_ports` and `udp_listen_ports` (one IPv4 and one IPv6 rule per port), and an egress-only one for the Lambda handlers. Set `existing_security_group_ids` on a target to attach your own groups instead — they apply to the EC2 listener and, when the `lambda` handler is enabled, to the probe Lambda. This is also the escape hatch for any other ingress posture: open-all groups, custom source CIDRs, or a fully closed listener.

```yaml
account_network_under_test:
  private:
    vpc_id: /infrastructure/vpc-id
    subnet_ids:
      - /infrastructure/private-subnet
    existing_security_group_ids:
      - sg-0abc123def456        # or an SSM parameter path
```

The main reason to set this is destroy speed with the `lambda` handler. CloudFormation cannot delete a security group CATS owns until Lambda releases its Hyperplane ENIs, which AWS documents as taking up to ~20 minutes. Groups CATS does not own are never waited on, so `cats infra destroy` returns immediately. Reusing the same subnet and group across runs also lets Lambda reuse those ENIs, which speeds up deploy and avoids cold first-invoke ENI provisioning.

The trade-off is that the groups must allow the traffic your tests need, in both directions.

Outbound is the requirement most likely to catch you out. CATS's own Lambda group allows all egress, and so does any security group with no explicit egress rule — but adding a single `SecurityGroupEgress` rule replaces that allow-all default. If the groups you provide restrict egress, `http_call`, `tcp_call` and `udp_call` see their packets dropped silently: HTTP and TCP return `TIMEOUT` for destinations they cannot reach, UDP still returns `SENT` with no receipt, and there is no error at deploy time.

Inbound matters too. A group you provide has to permit inbound access on the target's `tcp_listen_ports` and `udp_listen_ports` for anything to reach the listener — the same scope CATS's own EC2 group grants. When the probe Lambda calls the listener in its own target they share these groups, so a self-referencing ingress rule covers that path.

### `interface_endpoint_security_group_ids`

The interface VPC endpoints CATS creates (`interface_endpoints`) answer only what their security group admits, and without this setting they are in the VPC's default group. A default group's stock inbound rule admits members of that group alone, and the CATS listener is in its own group, so in a stock VPC, as in a hardened one, the endpoints never answer the listener and the SSM agent cannot register. Name a group that admits TCP 443 from the listener's subnets. It is a network setting, so it lives at the global, account or target level and inherits like the endpoint lists; targets sharing a VPC must agree on it, because the endpoints are created once per VPC. Gateway endpoints take no security group.

```yaml
global_network_settings:
  interface_endpoints: [ssm, ssmmessages, ec2messages]
  interface_endpoint_security_group_ids:
    - sg-0abc123def456            # or an SSM parameter path
```

Leave it unset only when the VPC's default group already admits 443 from the listener's subnets; CATS never creates a group for the endpoints. `existing_security_group_ids` is a different setting: it attaches to the EC2 listener and probe Lambda of one target, not to the endpoints. The policy every endpoint carries is described in [network testing](../concepts/network-testing.md#endpoint-policy).

The endpoints CATS creates are an escape hatch for a VPC that has no endpoint for the service and no DNS already answering for it, typically a stock or sandbox VPC. Where the VPC has an endpoint or a private hosted zone for the name, CloudFormation refuses the create; where a Resolver rule already sends the name elsewhere, the endpoint is created and never used, and the agent never registers. Where your organization provides endpoints, the listener uses them with nothing configured; where it cannot reach them, use the Lambda handler ([choosing the handler](../concepts/network-testing.md#choosing-the-handler-for-a-network-target)).

### Inline policies on test roles

Attach inline IAM policies to the default test role with `default_test_role_inline_policies`, or to additional roles with `inline_policies`. Entries are keyed by policy name; each value is either a policy document written inline in yaml, or a string path to a JSON policy file resolved relative to cats.yaml. Files are loaded and validated by `cats validate`, so missing files or malformed JSON fail before deploy.

```yaml
global_account_settings:
  default_test_role_inline_policies:
    dynamo-read:
      Version: "2012-10-17"
      Statement:
        - Effect: Allow
          Action:
            - dynamodb:GetItem
          Resource: arn:aws:dynamodb:*:*:table/my-table
    s3-scoped: ./policies/s3-scoped.json  # JSON file, relative to cats.yaml
```

Like other account settings, per-account `account_settings.default_test_role_inline_policies` replaces the global value entirely (no merging). See [identity model](../concepts/identity-model.md#extending-test-role-permissions) for role permission concepts.

Two size limits apply. IAM allows 10,240 characters of inline policy per role (whitespace excluded) — roughly 80 typical statements; IAM enforces it when the role is created, so a breach surfaces as a CloudFormation resource failure during `cats infra deploy`. CATS itself enforces CloudFormation's 51,200-byte template limit: `cats validate` renders each account's template and refuses one that would exceed it, before any credentials are used. Policies are embedded compactly (~130 bytes per typical statement), so an account with few network targets has room for hundreds of statements; an account close to the boundary in [network targets and listen ports](infrastructure.md#network-target-limits) has less to spare.

## Test selection

`test_selection` decides which tests run against this deployment, in configuration rather than
on the command line. A command-line selector (`-k`, `-m`, `--deselect`) narrows a run, and a
deselected control vanishes from the report with no record of why. A `test_selection` run
defines its scope in the configuration itself, and every test it holds back is in every report
as `not_run` with the reason.

```yaml
reporting:
  custom_markers: [mutating]

test_selection:
  provides: [container-deployment]
  excluded:
    - marker: standard_name
      values: [ACME-G-SCP-34]
      reason: "RDS not in use; risk accepted by the CISO"
    - marker: mutating
      values: [true]
      reason: "Change freeze; read-only run"
```

| Key | Type | Default | Purpose |
|-----|------|---------|---------|
| `provides` | list of str | `[]` | The requirements this deployment satisfies, matched against `@cats_requires(...)` on tests. A test runs only when every requirement it declares is provided; otherwise it is `not_run` with the gap named (`requires {a, b}; not provided {b}`). Unioned with what every `pytest_cats_provides` hook returns, so a test pack can provide its defaults from its conftest. Free-form names, each listed once. |
| `excluded` | list of `{marker, values, reason}` | `[]` | Tests held back by marker value, each with a reason. A test matching any entry is `not_run` with that `reason`. |
| `excluded[].marker` | str | required | The name written on the test: `standard_name` (the `@cats` keyword), `cats_requires` (the decorator), or a name declared in `reporting.custom_markers`. Anything else fails `cats validate`, with the nearest accepted name suggested. |
| `excluded[].values` | list of str or bool | required, non-empty | Values that exclude a test, compared for equality with the marker's value: `true` matches `@cats(mutating=True)`, `"true"` does not, and `1` is rejected. |
| `excluded[].reason` | str | required, non-empty | Why these tests do not run here. Recorded on every excluded test as its `not_run` reason in every report format. |

The rules behind the shape:

- The marker check is deliberate. `excluded: [{marker: mutating, values: [true], ...}]` is the
  mechanism for a read-only run, so a misspelled marker (`mutatng`) that silently matched
  nothing would let writes into a run its author believes is read-only. A typo is an error.
- A test that both matches an `excluded` entry and lacks a requirement is recorded as
  `excluded`: the explicit, reasoned decision is the more informative fact.
- An `excluded` entry that held back no test is not an error (a `-k` run or a renamed test
  produces one legitimately), but it is named in the terminal summary and in
  `run.selection.unmatched_exclusions`, because it usually means the exclusion is not doing
  what was intended.
- Misconfiguration fails loudly rather than being prevented: providing both
  `standard-deployment` and `container-deployment` runs the pipeline tests against a
  deployment with no pipeline, and they fail. CATS keeps no registry of requirement names and
  no exclusivity rules.
- A marker is named as it is written on the test, so nothing is reserved for this feature: `standard_name` is
  already a `@cats` keyword, and the `cats_` prefix on `cats_requires` is what keeps every engine mark clear
  of every custom marker. `requires` is as free a custom-marker name as any other.

Which deployments a test applies to is the test author's knowledge and goes on the test
(`@cats_requires`, see [Writing tests](writing-tests.md#tests-that-apply-only-to-some-deployments));
a property of this environment or this policy goes here, with a reason. Both are reported in
[`run.selection`](../api-reference/Reports.md#the-run-block).

## SSM Parameter References

Use SSM parameters instead of hardcoded IDs:

```yaml
account_network_under_test:
  private:
    vpc_id: /infrastructure/vpc-id
    subnet_ids:
      - /infrastructure/private-subnet
```

CATS resolves these at deploy time in the target account.

## Schema Validation

Enable editor autocomplete by pointing the YAML language server at the schema, either
the published one for your installed release or a local copy:

```yaml
# yaml-language-server: $schema=https://awslabs.github.io/cats/schema/latest.json
```

`cats validate` writes the same schema next to your config as `.schema.json`, for
editors without network access or a pinned check-in:

```yaml
# yaml-language-server: $schema=./.schema.json
```

The published schema for a specific release is at
`https://awslabs.github.io/cats/schema/v{version}.json`, which is also the `$id` inside
every generated `.schema.json`.

## Environment Variables

| Variable | Description |
|----------|-------------|
| `CATS_CONFIG` | Path to config file |
| `AWS_REGION` | Fills `cats_deployment_settings.aws_region` when cats.yaml leaves it unset; a key in cats.yaml wins. One of the two is required by every command that touches AWS |

## Full Schema Reference

See [Configuration Reference](../api-reference/Configuration.md) for all fields and their types.
