# Security Considerations

CATS requires high privilege to operate across multiple AWS accounts.

## Trust Assumptions

CATS assumes:

1. **Users are authorized** to execute tests in target accounts
2. **Compute environment is controlled** (your laptop, your pipeline, your container)
3. **Pipeline security is your responsibility** (CI/CD access controls, secret management)

## Privilege Model

### Deploy Identity (High Privilege)

Used by `cats infra deploy/destroy`. Can:

- Create/delete IAM roles and policies
- Create/delete EC2 instances
- Create/delete Lambda functions
- Create/delete S3 buckets
- Modify security groups

**Mitigation**: Only run `cats infra` from trusted environments. Use short-lived credentials.

### Test Identity (Scoped Privilege)

Created by CATS in each target account. Can:

- Invoke CATS-deployed Lambda functions
- Run SSM commands on CATS-deployed EC2 instances
- Read the CATS response bucket (`s3:GetObject`, `s3:ListBucket`; only the EC2 instance role writes to it)
- Read CloudFormation outputs
- Read the account's SSM managed-node inventory (`ssm:DescribeInstanceInformation`)
- Import findings into the account's own Security Hub (`securityhub:BatchImportFindings`), only when the
  `securityhub` report format is configured with reporting enabled, and only on that account's default
  product ARN in the one configured Region: the identity that ran the tests writes their results where the
  account's owner already looks, and cannot write findings for any other account or product. A finding
  carries the test's node id, docstring, assertion message, metadata, marker values and account tags, and
  is readable by everyone and everything with access to that Security Hub; see
  [what a finding contains and who can read it](api-reference/Reports.md#what-a-finding-contains-and-who-can-read-it)

**Mitigation**: Test role permissions are scoped to CATS resources by default. The two
exceptions are read-only SSM actions with no resource-level scoping: `GetCommandInvocation`
reads results of commands the role itself sent, and `DescribeInstanceInformation` is consulted
only after a `SendCommand` refusal, to tell a listener that has not registered yet from one
that is registered but refused the command. All of these grants are one inline policy
document on the role, `CatsTestRolePolicy`, one statement each; `cats infra deploy` reports
success only after assuming the role and exercising that document, so the role a test run
receives is one IAM has already agreed to.

### What a Security Hub finding carries

Each imported finding carries: the test's node id (`GeneratorId`, `cats/nodeid`); the test name as
`Title`; the docstring as `Description`; the failure text as `cats/error_message`, truncated to 2048
characters; the skip or exclusion reason as `cats/reason`; every custom marker value
(`cats/marker/*`); every `add_report_metadata` value (`cats/metadata/*`); the keynames of every
account the test used (`cats/accounts_in_test`); the account's `cats.yaml` tags as
`Resources[].Tags`; and `cats/run_id`, `cats/version` and `cats/aws_region`. In a cross-account
test the same finding, error text included, is imported into every account named in the test. The
full field list is in the [Reports reference](api-reference/Reports.md#what-each-finding-carries).

## Threat Model

### CATS never prints, returns or stores a credential

Every identity CATS uses is a short-lived assume-role session that lives in process memory and
refreshes itself; CATS writes no credential to disk and returns none in any CLI output or MCP tool
result. `cats run write-profile` and the `cats_write_profile` MCP tool write an AWS profile that
names a role (`role_arn`) and the identity to assume it from (`source_profile` or
`credential_source`), not keys: the `aws` CLI or an SDK assumes the role itself. The MCP tool
writes only `~/.aws/config`, since an agent acting on untrusted tool output must not be able to
aim the writer at another file; both entry points refuse a file that is not an AWS config file
(one whose section headers are not `default`, `profile ...`, `sso-session ...`, `services ...`,
`plugins` or `preview`) untouched. When a tool
assumes a role through such a profile, the AWS CLI caches the resulting temporary credentials under
`~/.aws/cli/cache`; that is the CLI's behavior for any `role_arn` profile and is outside CATS's
control.


### Orphaned Infrastructure

**Threat**: Compute fails mid-run, leaving EC2 instances running.

**Impact**: Cost (instances keep running), security surface (roles remain assumable).

**Mitigation**:

- Always use `finally`/`after_script` in CI/CD
- Run `cats infra status` periodically to check what is deployed
- Set up billing alerts

### Misconfigured Tests

**Threat**: Tests make incorrect assertions, giving false confidence.

**Impact**: Controls appear to work when they don't.

**Mitigation**:

- Review test logic carefully
- Test both positive and negative cases
- Validate tests catch known-bad configurations

### Dependency Vulnerabilities

**Threat**: Malicious code in CATS dependencies.

**Impact**: Arbitrary code execution with your AWS credentials.

**Mitigation**:

- Every pull request's dependency changes are checked against the GitHub Advisory Database (dependency review) before merge
- The locked runtime closure is audited against known vulnerabilities weekly and on every push to `main` (`pip-audit` in CI), so a disclosure against a version already pinned is caught between releases
- Every version is pinned in `uv.lock`, and CI installs from the lock, so a build cannot pick up an unreviewed release
- Review dependency updates

### Compute Environment Compromise

**Threat**: Attacker gains access to where CATS runs.

**Impact**: Can assume deploy role, modify infrastructure, exfiltrate data.

**Mitigation**:

- Secure CI/CD pipelines
- Use short-lived credentials
- Audit CloudTrail for unexpected role assumptions

### Malicious Test Injection

**Threat**: Attacker modifies test code to perform unauthorized actions.

**Impact**: Tests could create backdoors, exfiltrate data, or destroy resources.

**Mitigation**:

- Code review for test changes
- Restrict who can modify test code
- Use branch protection

## Deployed Infrastructure Security

### IAM Roles

- Test roles trust only the configured parent account (`parent_account_id`)
- Permissions scoped to CATS resources: the test role's actions name the stack's own Lambda, bucket, SSM document and listeners
- No wildcard IAM actions on the roles CATS creates. Two read-only SSM actions have `Resource: "*"` because the API offers no resource scoping (`ssm:GetCommandInvocation`, `ssm:DescribeInstanceInformation`); the Lambda execution role carries the AWS-managed `AWSLambdaVPCAccessExecutionRole`; security-group egress is open on both address families so the listener can reach S3 and SSM

### Lambda Functions

- Run in your VPC
- Limited to basic network calls
- No internet access unless VPC allows it

### EC2 Instances

- Run the current Amazon Linux AMI CATS selects at deploy time (or your `existing_ec2_ami_id`) with SSM agent
- Listen on configured ports only, as an unprivileged transient user with a read-only filesystem
- Security group ingress scoped to the configured listen ports: one IPv4 and one IPv6 rule per port, from any source, nothing else inbound
- No public IPv4 address, whatever the subnet's `MapPublicIpOnLaunch` setting: the interface is declared with `AssociatePublicIpAddress: false`, so over IPv4 the listen ports are reachable only from inside the VPC and whatever routes into it, and an internet gateway carries nothing for the listener. It reaches SSM through NAT, a route to the organization's endpoints, or endpoints in its VPC ([choosing the handler](concepts/network-testing.md#choosing-the-handler-for-a-network-target)). In a subnet that assigns IPv6 addresses the listener's address is globally routable, and a `::/0` route to an internet gateway exposes the listen ports to the internet; attach `existing_security_group_ids` there ([security groups on listeners](concepts/network-testing.md#security-groups-on-listeners))
- UDP listeners require a per-account receipt token before writing to S3; TCP/HTTP listeners answer with a fixed, content-free reply (today an empty `200 OK`) and disclose no content

### S3 Buckets

- Require HTTPS (TLS)
- Block public access
- Used only for UDP receipt storage

### VPC Endpoints

- Created only where `cats.yaml` names them (`gateway_endpoints`, `interface_endpoints`), in the VPC under test
- Every endpoint carries a policy allowing only principals in the account under test (`aws:PrincipalAccount`), where every CATS caller of the endpoint lives; an endpoint with no policy would take AWS's default, which admits any principal anywhere. The account's other workloads are unaffected, since an interface endpoint with private DNS carries the whole VPC's traffic for its service
- A shared VPC consumed from other accounts needs an organization condition instead, which CATS does not offer; see [the endpoint policy](concepts/network-testing.md#endpoint-policy)
- Interface endpoints are on the group `interface_endpoint_security_group_ids` names, or the VPC's default group when it is unset; CATS never creates a group for them. A stock default group admits only its own members, so the setting is what lets the listener reach a CATS-created endpoint ([security groups on interface endpoints](concepts/network-testing.md#security-groups-on-interface-endpoints))

## UDP receipt token

The EC2 listeners accept traffic on all configured ports — an application-layer
control cannot close a port (the kernel completes the handshake or receives the
datagram before any CATS code runs). What each layer controls:

| Concern | Control |
|---------|---------|
| Port is reachable at all | Security groups: CATS-managed listener groups admit only the configured listen ports, one IPv4 and one IPv6 rule per port |
| Unauthenticated side effects (UDP → S3 write) | UDP receipt token: a valid `{token}:` message prefix is required before any S3 write. Always enforced, no opt-out |
| Unauthenticated data disclosure (TCP/HTTP banner) | Banner removed: any received data is answered with a fixed, content-free reply (today an empty `200 OK`) |

The token is generated per account at `create_stack`, carried forward unchanged
on updates, and exposed as the `UdpReceiptToken` stack output. It is a
network-abuse guard, **not a bearer credential for anything IAM-protected** —
its threat model is unauthenticated *network* actors, not principals with AWS
read access. Any principal with `cloudformation:DescribeStacks` on the stack,
including the reduced-privilege CATS test role, can read the token — this is
intended, so secret scanners flagging the plaintext CFN output are reporting
expected behavior. It is equally visible to anyone who can read the listener's
user data (`ec2:DescribeInstanceAttribute`) or the stack's parameters, and to
any process on the listener itself, where it is an argument on the `udpserver`
command line. CATS callers never handle it: `udp_call` prepends it
automatically and never transmits it to non-CATS targets.

Rotation is destroy + deploy (a fresh token is generated on create), matching
the ephemeral lifecycle.

## Shell access (`allow_shell`)

Off by default at the account level. When `account_settings.allow_shell: true` at deploy time, CATS extends the test role's SSM policy with the run-command document `shell()` uses (today AWS's `AWS-RunShellScript`), which lets `NetworkHandler.shell()` run arbitrary commands on the EC2 listeners of that account's CATS stack.

**What this protects against.** With the flag off at deploy time, a compromised test role cannot run arbitrary commands on CATS EC2 instances: only the probe document is invocable. This is a real IAM reduction for the test role's standing permissions.

**What this does NOT protect against.**

- A compromised deploy identity. Anyone able to edit `cats.yaml` and run `cats infra deploy` can flip `allow_shell` to `true` and redeploy in seconds. The deploy identity is the real boundary; this flag sits well inside it.
- Test authors (human or LLM) who can edit `cats.yaml`. The Python guard on `NetworkHandler.shell()` is a convenience — it reads the live `cats.yaml` and surfaces a clear error. It is not a security control.

**Blast radius when enabled.**

- Commands run as the SSM agent's command user, which on the Amazon Linux images CATS deploys today is root.
- They inherit the EC2 instance's IAM role, not the CATS test role. The role CATS creates grants exactly what SSM Agent needs to register and run commands (`ssm:UpdateInstanceInformation`, the `ssmmessages` and `ec2messages` channel actions) plus `s3:PutObject` on the response bucket, so a shell on the listener gains no read of SSM parameters, no write to SSM inventory or compliance data, and no access outside the bucket. Supply `existing_ec2_instance_profile_name` to use a profile of your own instead; anything that profile grants, a shell inherits.
- Instance targeting is scoped to the instances of the same stack (the `cats:stack` tag), so two CATS stacks in one account cannot reach each other's listeners.
- No command allow-list. Enable only where callers and deploy identity are trusted.

The flag is equivalent to granting the test role `ssm:SendCommand` on the run-command document yourself; it removes that boilerplate.

## Reporting Security Issues

If you discover a potential security issue in CATS, notify AWS/Amazon Security
via the [vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting/)
or email [aws-security@amazon.com](mailto:aws-security@amazon.com) — see
[SECURITY.md](https://github.com/awslabs/cats/blob/main/SECURITY.md). Please do
*not* create a public GitHub issue.
