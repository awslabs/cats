# Infrastructure Management

## Commands Overview

| Command | Description |
|---------|-------------|
| `cats infra deploy` | Deploy CATS infrastructure |
| `cats infra destroy` | Remove CATS infrastructure |
| `cats infra status` | Check deployment status |
| `cats infra targets` | Preview what would be deployed |
| `cats infra show-template` | View generated CloudFormation |

Every command takes account keynames. `deploy` and `destroy` require keynames or `--all`;
the three read-only commands default to every account when no keyname is given, so
`cats infra status`, `cats infra targets` and `cats infra show-template` on their own cover
the whole configuration.

## Deploy

### Single Account

```bash
cats infra deploy prod
```

### Multiple Accounts

```bash
cats infra deploy prod dev shared
```

### All Accounts

```bash
cats infra deploy --all
```

Deploys to all accounts in parallel.

### When deploy returns

`cats infra deploy` exits 0 when the stack is complete, **its test role is usable, and every EC2 listener
in it is ready**: bound to every configured TCP and UDP port. CloudFormation alone reports an EC2
instance complete when it is running, which is before its user data has installed and started the
listeners; a probe sent in that window gets `REFUSED` from a listener that does not exist yet. So after
the stack's `CREATE_COMPLETE` (or `UPDATE_COMPLETE`) line, deploy prints one line as the test role is
proven usable and one per listener as it becomes ready, then `Deployed successfully`; in a `--all`
deploy the accounts wait in parallel, shown as `Readying`, and each flips to `Created` the moment its
own role and listeners are ready: an account with no network targets is not held open by another's
EC2 boot.

The listener declares itself: the last thing its user data does is wait for its ports to be bound and
write `cats listeners ready` to the serial console, and deploy reads that line back through the EC2 API
(`ec2:GetConsoleOutput`). Nothing travels over the instance's own network, so a listener in a subnet
with no route to any endpoint, such as a data tier with an empty route table, is gated exactly like one
with SSM reachability. That listener is a target only: probe **from** it with the `lambda` handler, since
`ec2_*` probes need SSM. Reading the live console requires a Nitro-based `ec2_instance_type`; the default
`t3.micro` is one.

A listener that never binds fails the deploy with the tail of its serial console, where the cause is:

```
FAIL Deployment failed: Stack CATS-test-infra-prod reached CREATE_COMPLETE but its listeners are not ready.
EC2 listener i-0abc123def456 (private) is not ready after 41s: its user data found listener ports still unbound. Console:
  [   30.129306] cloud-init[1644]: cats listeners not bound: tcp/8080
  [   30.229306] cloud-init[1644]: x tcpserver.service - CATS TCP listener
  ...
Recreate it: cats infra destroy prod && cats infra deploy prod
```

The stack is left in place for you to inspect (`cats infra status`, or the full console with
`aws ec2 get-console-output --instance-id i-0abc123def456 --latest --output text`); it is not usable
for tests until recreated. A user data that stops before its last line, or an instance that never boots
as far as cloud-init, fails the same way after at most 180 seconds, with whatever the console holds.

The test role is checked the same way CloudFormation cannot: IAM reports a role and its policy created
seconds before its authorizer honors them, and a `cats test` started in that window fails its first call
with `AccessDenied`. So deploy assumes the test role from the credentials it was run with (the ones
`cats test` uses) and reads the stack as that role, retrying a denial for up to 180 seconds; each
additional role is proven assumable the same way. A denial that outlasts the wait, or an explicit deny,
fails the deploy naming the role and the possible causes, and since nothing deployed is wrong the
remediation is to fix the cause and deploy again; see
[troubleshooting](../troubleshooting.md#reached-create_complete-but-its-test-role-is-not-usable-yet).
A re-run with nothing to change runs the same checks, so it also means ready.

## Destroy

### Single Account

```bash
cats infra destroy prod
```

### All Accounts

```bash
cats infra destroy --all
```

!!! warning
    Always destroy infrastructure when done. EC2 instances cost money and orphaned roles create security surface area.

### How long destroy takes

A stack with only the `ec2` handler is gone in a few minutes. A stack with the `lambda` handler
can take up to about 20 minutes: CloudFormation does not report the last probe function on a
subnet and security-group combination deleted until the network interfaces it used have been
released, and that release is asynchronous on the Lambda side. The other functions sharing
those interfaces delete in seconds. The wait is CloudFormation's, not CATS's; it ends on its
own and needs no action. In CI, set the job timeout with this in mind.

## Status

Check deployment state:

```bash
cats infra status
```

Output shows CloudFormation stack status and the CATS release that deployed each stack:

```
Account          Status              Stack                    Version
prod             CREATE_COMPLETE     CATS-test-infra-prod     1.0.1
dev              UPDATE_COMPLETE     CATS-test-infra-dev      1.0.0 (installed 1.0.1)
shared           not deployed        -                        -
```

Status values are CloudFormation stack statuses (`CREATE_COMPLETE`, `UPDATE_COMPLETE`, `DELETE_IN_PROGRESS`, etc.) or `not deployed` if no stack exists.

The Version column reads the `cats:version` tag that `cats infra deploy` puts on every stack. A cell reading `X (installed Y)` means the stack was deployed by a different CATS release than the one installed. `cats test`, `cats run`, and the MCP tools refuse such a stack, and so does `cats infra deploy`, because a stack update does not replace the EC2 listener code or SSM documents built by the old release. Recreate it:

```bash
cats infra destroy dev && cats infra deploy dev
```

`unknown` means the stack carries no `cats:version` tag: something other than `cats infra deploy` created it. It is refused the same way.

## Preview Targets

See what would be deployed without deploying:

```bash
cats infra targets
```

## View CloudFormation Template

```bash
cats infra show-template prod
```

Export to file:

```bash
cats infra show-template prod --dump-dir ./templates
```

## What Gets Deployed

Everything CATS creates is in the account's one CloudFormation stack, so `cats infra destroy`
removes all of it; nothing is created outside the stack.

Per account:

- **IAM role**: Trusts the parent account, used during tests
- **S3 bucket**: Stores UDP packet receipts (empty objects, expired after one day)
- **Instance role** (unless every target sets `existing_ec2_instance_profile_name`) and, if the `lambda` handler is enabled anywhere, one **Lambda execution role**: one of each per stack, shared by every target's listener or probe function
- **SSM document** (if the `ec2` handler is enabled anywhere): `{cfn_stack_prefix}{account_key}-Probe`, the command the test role is allowed to send to the listeners (plus `AWS-RunShellScript` on accounts with `allow_shell: true`)
- **CloudWatch log group** (if the `lambda` handler is enabled anywhere): `/aws/lambda/{cfn_stack_prefix}{account_key}-probe`, shared by every probe function, 7-day retention. Declared in the stack so that destroy removes it; Lambda would otherwise create one per function that never expires and outlives the stack. An organization's log-group automation (Landing Zone Accelerator sets its own retention, encryption key and subscription filter on every new group) changes the live group. That drift is never contested: a stack update rewrites a resource only when its template properties change, and the group's never do for the life of the stack (its name derives from the stack's prefix and account, its retention is a constant), so the group is left as the automation set it and deleted with the stack
- **VPC endpoints** (only where a target lists `interface_endpoints` or `gateway_endpoints`): one interface endpoint per listed service, with a network interface in each of the target's subnets, and one gateway endpoint per listed service. See [choosing the handler for a network target](../concepts/network-testing.md#choosing-the-handler-for-a-network-target)

Per network target:

- **EC2 instance**: Listens on configured ports. Never has a public IPv4 address, whatever the subnet's `MapPublicIpOnLaunch` setting; in a subnet that assigns IPv6 addresses it has a globally routable one ([security groups on listeners](../concepts/network-testing.md#security-groups-on-listeners)). Tagged `cats:managed=true` and `cats:stack=<stack name>`, which is how to find CATS listeners with `describe-instances`
- **Security group** for the listener: Ingress scoped to the configured listen ports, one IPv4 and one IPv6 rule per port, from any source. [Security groups on listeners](../concepts/network-testing.md#security-groups-on-listeners) explains the posture; `existing_security_group_ids` replaces it with your own
- **Probe Lambda** (if the `lambda` handler is enabled): one function per network target, running the same probe as the EC2 listener for HTTP, TCP and UDP, with its own **security group** (egress only) unless `existing_security_group_ids` is set

## Stack Naming

Stacks are named: `{cfn_stack_prefix}{account_key}`

Default: `CATS-test-infra-prod`

Customize:

```yaml
cats_deployment_settings:
  cfn_stack_prefix: "MyOrg-CATS-"
```

The prefix also names the IAM roles the stack creates (`{cfn_stack_prefix}{account_key}-default-test-role`
and one per additional role), and IAM role names are global to an account while stack names are
regional. To deploy the same account in a second Region, use a second `cats.yaml` with a different
prefix (for example `CATS-euw1-`); with the same prefix the second Region's stack fails on the role
that already exists. The S3 response bucket and the security groups are named by CloudFormation and
never collide.

## Parallel Deployment

`--all` deploys to accounts in parallel. Each account's deployment is independent.

For large organizations, this significantly reduces total deployment time.

## Handling Failures

If deployment fails mid-way:

1. Check CloudFormation console for the failed stack
2. Review the failure reason
3. Fix the issue (often VPC/subnet configuration)
4. Re-run `cats infra deploy`

CloudFormation handles rollback automatically for failed creates.

A deploy that fails **after** `CREATE_COMPLETE`, because a listener did not become ready
(see [When deploy returns](#when-deploy-returns)), is not rolled back: the stack stays so
the instance can be inspected. Recreate it with `cats infra destroy <account> && cats infra deploy <account>`
once the cause is fixed.

## Updating Infrastructure

The rule is the `cats:version` stack tag: a stack deployed by the CATS release you have
installed can be updated in place; a stack deployed by any other release must be
recreated.

**Same release, changed config.** Re-running `cats infra deploy` updates the stack:

- Changed config → stack update
- Adding `securityhub` to `reporting.report_formats` (or changing `reporting.securityhub.aws_region`) → the test
  role gains its `securityhub:BatchImportFindings` grant; until you redeploy, `cats test` fails the import
  and names this command. Right after the redeploy the writer retries a denied import for up to 30 s,
  since this is the one grant the deploy's readiness check cannot see converge
- New network targets → resources added
- Removed network targets → resources deleted
- Unchanged config → no-op ("No updates are to be performed"); instances never bounce, but the readiness
  checks still run, so the deploy reports `Deployed successfully` only once the test role and listeners
  are proven ready

!!! note
    Some changes require replacement (e.g., changing VPC or the first subnet). CloudFormation
    handles this automatically.

**Different release.** `cats infra deploy` refuses, and so do `cats test`, `cats run` and
the MCP tools, because a stack update does not replace what a release builds into the
instance: EC2 user data (the listeners) runs on first boot only, and a named SSM document
cannot change content in place. Recreate instead:

```bash
cats infra destroy <account> && cats infra deploy <account>
```

Destroy works with any stack version. The per-account UDP receipt token is regenerated
on every create, so destroy + deploy is also how the token rotates.

## Cleanup Best Practices

### Local Development

```bash
# Deploy, iterate, destroy when done
cats infra deploy prod
# ... develop and test ...
cats infra destroy prod
```

### CI/CD

Always use `finally` or `after_script`:

```yaml
test:
  script:
    - cats infra deploy --all
    - cats test
  after_script:
    - cats infra destroy --all
```

### Finding Orphans

```bash
cats infra status
```

Accounts removed from your config no longer appear in status output: destroy their stacks before removing them, or delete the stack directly in CloudFormation.

## Network Target Limits

CloudFormation limits a template to 51,200 bytes. There is no fixed cap on the number of network
targets an account can hold: `cats validate` renders each account's template (offline, from
placeholders, no credentials needed) and refuses a configuration that would exceed the limit, naming
the account. `cats infra deploy` makes the same check before it assumes the deploy role.

What costs bytes: each target's listener, security groups and probe Lambda, plus two security group
ingress rules per listen port. Code shared by every target (the listener's user data, the probe, the
instance and Lambda roles, the probe log group) is embedded once per template. The security group
rules carry no `Description` for this reason: one costs about 40 bytes a rule, and at the larger
rows below that takes the template over the limit (checkov reports it as CKV_AWS_23). Measured on
this release, every target in the account having the ports shown:

| handlers | listen ports per target | targets that fit |
|---|---|---|
| `ec2` | 1 TCP + 1 UDP | 13 |
| `ec2` | 5 TCP + 5 UDP | 7 |
| `ec2` | 10 TCP + 10 UDP | 5 |
| `ec2` | 30 TCP (the per-target maximum) | 4 |
| `ec2` + `lambda` | 1 TCP + 1 UDP | 8 |
| `ec2` + `lambda` | 5 TCP + 5 UDP | 5 |
| `ec2` + `lambda` | 10 TCP + 10 UDP | 4 |
| `ec2` + `lambda` | 30 TCP (the per-target maximum) | 3 |

Inline IAM policies on the test roles count too, so an account near the boundary has less room for
them (see [configuration](configuration.md#inline-policies-on-test-roles)). When `cats validate` refuses an account,
reduce its targets, listen ports or handlers, or split the targets across a second account entry with
the same `account_id`:

```yaml
accounts_under_test:
  prod_vpc1:
    account_id: "111111111111"
    account_network_under_test:
      # First set of targets

  prod_vpc2:
    account_id: "111111111111"  # Same account, different stack
    account_network_under_test:
      # Next set of targets
```

Each entry is its own stack with its own byte budget, its own test roles (named after the entry, so
the two never collide) and its own fixture: `prod_vpc1_account` and `prod_vpc2_account`. Deploy and
destroy both; a test that spans the two takes both fixtures.
