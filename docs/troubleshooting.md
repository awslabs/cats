# Troubleshooting

See also: [Infrastructure Management](guides/infrastructure.md) for deployment commands, [Identity Model](concepts/identity-model.md) for role configuration.

## Deployment Issues

### An AWS failure is one line and you need the traceback

**Symptom**: a command ends with a single `FAIL ...` line quoting an AWS SDK message (no
credentials, an expired token, an endpoint that could not be reached) and exit code 1.

That is the intended report: the SDK's message says what happened, and the environment, not
CATS, is what needs fixing. If you suspect the request itself was wrong (a parameter
validation error names a CATS bug, not your environment), set `CATS_DEBUG=1` and the
error propagates with its full traceback instead.

### "Access Denied" During Deploy

**Symptom**: `cats infra deploy` fails with AccessDenied.

**Causes**:

1. Missing permissions to assume deploy role
2. Deploy role doesn't exist in target account
3. Trust policy doesn't include your account

**Solutions**:

```bash
# Verify you can assume the role manually
aws sts assume-role \
  --role-arn arn:aws:iam::111111111111:role/AWSControlTowerExecution \
  --role-session-name test
```

Check the role's trust policy includes your account.

### CloudFormation Stack Stuck

**Symptom**: Deploy hangs or stack shows `UPDATE_IN_PROGRESS` for a long time.

**Solutions**:

1. Check CloudFormation console for the stack
2. Look at Events tab for the blocking resource
3. Common causes: EC2 instance failing to start, SSM agent not connecting

### "reached CREATE_COMPLETE but its listeners are not ready"

**Symptom**: The stack completed, then deploy failed naming an EC2 listener, followed by the last
lines of that instance's serial console. Four forms:

- `its user data found listener ports still unbound`: the console shows `cats listeners not
  bound: tcp/8080` and the `systemctl status` of the `tcpserver`/`udpserver` units, which says why
  the unit did not bind.
- `cloud-init finished without the listeners' ready line, so the user data stopped early`: the
  console shows cloud-init's own error for the line that failed.
- `it did not report ready within 180s`: the console shows how far the boot got. Empty console
  output means the instance has not booted at all (a stopped or impaired instance).
- `its instance type has no live serial console`: `ec2_instance_type` is not Nitro-based, so the
  console cannot be read while the instance boots. Use a current-generation type; the default
  `t3.micro` is one.

Deploy waits for listeners so that the first test never probes an instance whose listener has
not started yet (that would report `REFUSED`, a wrong answer that looks like a control failure).
The wait reads the instance's serial console through the EC2 API (`ec2:GetConsoleOutput`), so it
needs no network path from the instance: a listener in a subnet with no route to any SSM endpoint
is gated the same way. The stack is left in place for inspection:

```bash
# The instance's full serial console, from the account under test
aws ec2 get-console-output --instance-id i-0abc123 --latest --output text

# Then recreate once the cause is fixed
cats infra destroy <account> && cats infra deploy <account>
```

If the deploy identity is a custom role, it needs `ec2:GetConsoleOutput` on the listener instances
for this wait; see the [identity model](concepts/identity-model.md#deploy-identity). An
`UnauthorizedOperation` from that call fails the deploy with that message.

### "reached CREATE_COMPLETE but its test role is not usable yet"

**Symptom**: The stack completed, then deploy failed naming the test role, after up to 180 s. Three
forms, each ending with the AWS denial message:

- `could not be assumed after 180s`: `sts:AssumeRole` on the test role kept being denied. Either
  IAM had still not converged on the role's trust policy, or the identity running `cats infra
  deploy` is not allowed to assume CATS test roles. The deploy assumes the test role from the
  credentials it was run with (the same ones `cats test` uses), not from the deploy role, so check
  that identity's own policies and any SCP on the parent account.
- `was assumed but cloudformation:DescribeStacks on the stack is still denied after 180s`: the role
  exists but its `CatsTestRolePolicy` was still not honored. Either IAM had still not converged on
  it, or a service control policy or permissions boundary in the account under test denies
  `cloudformation:DescribeStacks` to the role.
- `is explicitly denied`: the denial names an explicit deny, which waiting does not change. Find
  the policy that states it: a service control policy, a permissions boundary, or a resource policy.

Deploy waits for the test role because CloudFormation reports an IAM role complete when IAM's
control plane has accepted it, seconds before IAM's authorizer honors it; a `cats test` started in
that window fails its first call with `AccessDenied`. The wait makes that first call itself (assume
the role, read the stack as it) and retries a denial until it succeeds. Nothing deployed is wrong
when it fails, so the stack is left in place and the remediation is to fix the cause and run the
deploy again:

```bash
# Confirm the identity running the deploy can assume the role
aws sts assume-role --role-arn arn:aws:iam::<account>:role/<prefix><account>-default-test-role --role-session-name check

# Then deploy again once the cause is fixed
cats infra deploy <account>
```

Only CATS's own grants are proven this way. Policies you attach through
`default_test_role_managed_policies`, `default_test_role_inline_policies` or an additional
role's `managed_policies`/`inline_policies` are written earlier in the same stack and are expected
to be live by the time the check passes, but a test whose first call relies on one and runs within
a few seconds of the deploy can still see `AccessDenied` on a slow IAM day; retry at your own
client (boto3's retry configuration) or run level.

### VPC/Subnet Not Found

**Symptom**: Deploy fails with "subnet not found" or "vpc not found".

**Causes**:

1. Wrong VPC/subnet ID in config
2. SSM parameter doesn't exist (if using parameter references)
3. Wrong region

**Solutions**:

```bash
# Verify VPC exists
aws ec2 describe-vpcs --vpc-ids vpc-abc123

# Verify SSM parameter exists
aws ssm get-parameter --name /my/vpc/id
```

### "conflicting DNS domain" creating an interface endpoint

**Symptom**: Deploy fails at an `InterfaceEndpoint<Service>Vpc<N>` resource with CloudFormation's
`there is already a conflicting DNS domain for <service>.<region>.amazonaws.com in the VPC`.

**Cause**: the VPC already has an endpoint for that service, or a private hosted zone for its name
(your organization's centralized endpoints). Only one may answer for the name, so CATS cannot add
another; `interface_endpoints` is an escape hatch for a VPC that has neither.

**Solution**: remove `interface_endpoints`. If the listener's subnet routes to the existing
endpoints, the EC2 handler needs nothing further; if it does not, enable the Lambda handler, which
needs no path from the listener ([choosing the handler](concepts/network-testing.md#choosing-the-handler-for-a-network-target)).
Then recreate: `cats infra destroy <account> && cats infra deploy <account>`.

### Version Mismatch: "was deployed by CATS X but CATS Y is installed"

**Symptom**: `cats test` fixtures error, `cats run` and MCP tools fail, or `cats infra deploy` refuses with:

```
CATS stack 'CATS-test-infra-prod' in target account 'prod' was deployed by CATS 1.0.0
but CATS 1.0.1 is installed. Deployed listeners and handlers may not match this library.
Recreate it: cats infra destroy prod && cats infra deploy prod
```

**Cause**: The CATS library was upgraded after the stack was deployed. Every stack carries a `cats:version` tag naming the release that deployed it, and CATS compares that tag with the installed release before using or updating the stack. The EC2 listener code and SSM documents in a stack are built by the release that deployed them, and a CloudFormation update does not replace them: EC2 user data runs on first boot only, and named SSM documents cannot change content in place. Running `cats infra deploy` again would report success while the instances kept running old code, so deploy refuses instead.

A variant reads "has no cats:version tag": the stack was created by something other than `cats infra deploy`, so CATS cannot confirm what it contains.

**Solution**: Recreate the stack.

```bash
cats infra destroy prod && cats infra deploy prod

# See which accounts are affected
cats infra status
```

To run against a mismatched stack anyway, set `CATS_SKIP_VERSION_CHECK=1`. CATS logs a warning and continues. Results may not reflect the installed library, so treat this as a diagnostic aid rather than a workflow; it does not apply to `cats infra deploy`.

## Test Issues

### "No Fixture Named X"

**Symptom**: pytest fails with `fixture 'prod_account' not found`.

**Causes**:

1. Account not in `cats.yaml`
2. Config file not found
3. Missing or wrong `CATS_CONFIG` path

**Solutions**:

```bash
# Verify config is valid
cats validate

# Check CATS_CONFIG if not using default location
CATS_CONFIG=/path/to/cats.yaml cats test
```

### Role Assumption Fails During Test

**Symptom**: Test fails with "Unable to assume role".

**Causes**:

1. CATS infrastructure not deployed
2. Test role was deleted
3. Trust policy changed

**Solutions**:

```bash
# Check deployment status
cats infra status prod

# Redeploy if needed
cats infra deploy prod
```

### A call returns `TIMEOUT`

**Symptom**: `http_call()` or `tcp_call()` returns `CatsNetworkResponse.TIMEOUT`.

This is a result, not an error: the probe ran on the listener and the TCP connect got no
answer within the probe's own timeout, which is consistent with a silent drop. Check, in
order, the security groups and NACLs on the path, the route to the target, and whether
anything listens on that port. If the target is another CATS listener, only its configured
`tcp_listen_ports` and `udp_listen_ports` are admitted by the group CATS manages.

### `CatsSSMError` from the EC2 handler

The EC2 handler runs the probe through SSM. Three messages name three different causes:

- `EC2 instance i-... not registered with SSM after 90s`: the listener's subnet gives it no path to
  the SSM services. The listener has no public IPv4 address, so an internet gateway carries no
  IPv4 traffic for it; it needs NAT, a route to your organization's endpoints, or interface
  endpoints in its VPC.
  Without that path the EC2 handler cannot be the source for the target; use the Lambda handler as
  the source, by enabling it or, where it is enabled, by calling `lambda_<protocol>_call`
  ([choosing the handler](concepts/network-testing.md#choosing-the-handler-for-a-network-target)).
- `SSM command was not delivered to the instance within Ns`: the instance registered once but the
  command never reached it. The instance is stopping, or its agent has lost its path to the SSM
  services.
- `SSM command was still InProgress after Ns and CATS stopped waiting`: the command reached
  the instance and is a slow one (a long `shell()` script, or a probe against a host that
  hangs). It may still be running on the instance. Raise `network_call_timeout_seconds` for
  commands that legitimately take longer.

**Solutions**:

```bash
# Check the EC2 listeners are running (CATS tags them cats:managed=true)
aws ec2 describe-instances --filters "Name=tag:cats:managed,Values=true"

# Check SSM connectivity
aws ssm describe-instance-information
```

### UDP Test Fails

**Symptom**: `receive_s3()` returns False even though `udp_call` returned `SENT`.

**Causes**:

1. UDP packet dropped by network
2. S3 bucket permissions
3. Timing: packet hasn't arrived yet
4. Packet dropped by the listener's receipt-token check — expected for raw senders
   that omit the `{token}:` prefix
5. The listener cannot reach S3 to write the receipt: its route table has no NAT and no S3
   gateway endpoint. The listener has no public IPv4 address, so a route to an internet gateway
   does not count. Every datagram to that listener then reads as not received, so pair each
   negative UDP control with a positive one (a datagram that must arrive) to the same listener
   in the same run, as [Testing Traffic is Blocked](concepts/network-testing.md#testing-traffic-is-blocked)
   describes; `journalctl -u udpserver` on the listener shows `failed to write receipt`

**Solutions**:

```python
# Give the receipt longer to appear (default timeout is 10s, polled every 2s)
sent = source.private.udp_call(target.private.ec2_dns_name, 5000)
assert target.private.receive_s3(sent, timeout=20)
```

Distinguish "dropped by auth" from "never arrived" on the listener
(requires `allow_shell: true`):

```python
result = target.private.shell("journalctl -u udpserver")
# "dropped datagram: missing or mismatched receipt token" means the packet
# arrived but failed the token check
```

**Symptom**: `receive_s3()` raises `CatsUsageError` instead of returning a bool.

The result you passed was not a `SENT` UDP result: either it came from an HTTP/TCP
call, or the datagram never left the sender (`DNS_FAILURE`, `FAILURE`). The
message includes the result's repr with the probe's reason. This is deliberate: a
datagram that was never sent must not read as "dropped in flight".

### Stack Deployed by Another CATS Release

**Symptom**: `CatsInfrastructureError: Stack ... was deployed by another CATS release and
its outputs do not match this version`, or `CatsSSMError: SSM document '...-Probe' not
found: either 'ec2' is not in enabled_network_handlers (add it and run: cats infra deploy
...), or the stack was deployed by another CATS release`.

These are the shape-level checks behind the version check (see "Version Mismatch"
under Deployment Issues, which normally fires first): the stack's outputs or documents
do not have the shape this release expects, which means another release built them.
Recreate: `cats infra destroy <account> && cats infra deploy <account>`. Destroy works
with any stack version.

## Configuration Issues

### Validation Errors

**Symptom**: `cats validate` reports errors.

**Solutions**:

```bash
# Get detailed error output
cats validate

# Common fixes:
# - account_id must be 12 digits as string: "123456789012"
# - subnet_ids must be a list, not string
# - vpc_id required if account_network_under_test defined
```

### Schema Not Found

**Symptom**: Editor shows "schema not found" warning.

**Solutions**:

```bash
# Regenerate schema
cats validate

# Verify .schema.json exists
ls -la .schema.json
```

## Debugging Network Tests

### Manual SSM Command

Test SSM connectivity directly:

```bash
# Get instance ID from CloudFormation outputs
INSTANCE_ID=$(aws cloudformation describe-stacks \
  --stack-name CATS-test-infra-prod \
  --query "Stacks[0].Outputs[?OutputKey=='Ec2InstanceIdprivate'].OutputValue" \
  --output text)

# Run a command
aws ssm send-command \
  --instance-ids $INSTANCE_ID \
  --document-name AWS-RunShellScript \
  --parameters 'commands=["curl -v http://target:443"]'
```

### Check EC2 Listener

```bash
# Connect with SSM Session Manager (SSH is disabled on the listener)
aws ssm start-session --target $INSTANCE_ID

# Check listeners
ss -tlnp  # TCP
ss -ulnp  # UDP
```

### Check Security Groups

```bash
# Get security group from instance
SG_ID=$(aws ec2 describe-instances \
  --instance-ids $INSTANCE_ID \
  --query "Reservations[0].Instances[0].SecurityGroups[0].GroupId" \
  --output text)

# Check rules
aws ec2 describe-security-groups --group-ids $SG_ID
```

## Cleanup Issues

### Orphaned Stacks

**Symptom**: a CATS stack exists in an account that is no longer in `cats.yaml`
(status only reports accounts in your config, so orphans are invisible to it —
check the CloudFormation console).

**Solutions**:

```bash
# Delete manually
aws cloudformation delete-stack --stack-name CATS-test-infra-orphan

# Or add account back to config and destroy
cats infra destroy orphan
```

### Stack Delete Fails

**Symptom**: `cats infra destroy` fails or hangs.

**Causes**:

1. Resources in use
2. S3 bucket not empty
3. IAM role has dependencies
4. A service control policy denies the deploy role a delete (`logs:DeleteLogGroup` on
   `CatsProbeLogGroup`, `ssm:DeleteDocument`, `lambda:DeleteFunction`): the stack ends
   `DELETE_FAILED` at that resource with an explicit-deny message in its events

**Solutions**:

1. Check CloudFormation console for failure reason
2. `cats infra destroy` empties the bucket; if a destroy failed part-way, empty it manually
3. Delete stack from console with "retain resources" option
4. The deploy role must be exempt from such guardrails, as the Control Tower and Landing Zone
   Accelerator execution roles are by principal ARN; a custom deploy role needs the same exemption

## Getting Help

1. Check `cats --help` and subcommand help
2. Run `cats validate` to check config
3. Check CloudFormation console for deployment issues
4. Review CloudTrail for permission errors
