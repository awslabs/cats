# How CATS Works

## The Lifecycle

```
cats.yaml → cats infra deploy → cats test → cats infra destroy
```

1. **Configure**: Define accounts and network targets in `cats.yaml`
2. **Deploy**: CATS creates CloudFormation stacks with IAM roles and optional network infrastructure
3. **Test**: pytest runs, CATS fixtures assume roles and interact with deployed resources
4. **Destroy**: Remove all CATS infrastructure

## Phase 1: Configuration

`cats.yaml` defines three things:

```yaml
# Where CATS runs from and how it deploys
cats_deployment_settings:
  parent_account_id: "999999999999"  # Account running tests
  aws_region: us-east-1

# What accounts to test
accounts_under_test:
  prod:                               # Config key → fixture name
    account_id: "111111111111"
    account_network_under_test:       # Optional: network targets
      private:
        vpc_id: vpc-abc123
        subnet_ids: [subnet-111]
```

The config key (`prod`) becomes your fixture name (`prod_account`).

## Phase 2: Deploy

`cats infra deploy prod` creates a CloudFormation stack in the target account:

**Always created:**

- IAM role trusting your parent account
- S3 bucket for UDP test receipts

**Created per network target** (if `account_network_under_test` defined):

- EC2 instance listening on configured ports
- One probe Lambda per network target for HTTP/TCP/UDP calls (if the `lambda` handler is enabled)

CloudFormation outputs store metadata (instance IDs, DNS names, bucket names) that CATS reads at test time.

Deploy returns when the stack is complete, the test role is usable, and each EC2 listener is ready,
meaning bound to its ports.
CloudFormation considers an instance complete when it is running, before its user data has started
the listeners, so each listener's user data waits for its ports and writes a ready line to its serial
console, and CATS reads that line through the EC2 API before reporting success. No network path from
the instance is needed, so a listener in a fully isolated subnet is gated like any other. The first
`cats test` never meets a listener that is still starting.

CloudFormation likewise considers an IAM role and its policy complete before IAM's authorizer has
converged on them, a window of a few seconds in which a call made as the role is denied. So the deploy
assumes the test role from the same credentials `cats test` will use and makes the first call a test
session makes, `cloudformation:DescribeStacks` on the stack, retrying a denial until it succeeds. That
proves the trust policy and CATS's own policy on the role are live; each additional role is proven
assumable. The grants you attach yourself are written earlier in the stack through the same IAM path, so
they are expected to be live too, but the deploy does not prove them: there is no call CATS could know
is safe to make under an arbitrary policy. A re-run of `cats infra deploy` with nothing to change runs
the same checks, so it also means ready.

## Phase 3: Test

When you run `cats test`, the pytest plugin:

1. Loads `cats.yaml` and registers fixtures for each account
2. When a test requests `prod_account`, CATS:
   - Assumes the IAM role in that account
   - Reads CloudFormation stack outputs
   - Returns an `AccountUnderTest` object with a boto3 session

```python
def test_something(prod_account: AccountUnderTest) -> None:
    # prod_account.session is a boto3 Session with assumed credentials
    s3 = prod_account.role.readonly.client("s3")

    # For network tests, access targets by name
    result = prod_account.private.http_call(target, 443)
```

## Control reporting

The run's output is a report, not a pytest summary. `@cats(standard_name=...)` names the control
a test proves, and every entry records that control, the accounts the test used, and the outcome,
in every format listed under `report_formats`. See
[Reporting and Compliance Alignment](reporting.md).

## Identity Model

CATS uses two distinct identities:

| Identity | Used For | Privilege Level |
|----------|----------|-----------------|
| **Deploy identity** | `cats infra deploy/destroy` | High (creates IAM roles, EC2, Lambda) |
| **Test identity** | Test execution | Scoped to test needs |

The deploy identity is typically `AWSControlTowerExecution` or `OrganizationAccountAccessRole`, roles that already exist in your accounts. You can also create a narrower role of your own for this purpose.

The test identities are created by CATS with least-privilege permissions for the deployed infrastructure.

## Treat infrastructure as ephemeral

CATS infrastructure is designed to be temporary:

```bash
cats infra deploy prod
cats test
cats infra destroy prod
```

This should happen every test cycle. In CI/CD, use `finally` blocks to ensure cleanup even on failure.

!!! warning
    CATS does not automatically clean up. Orphaned EC2 instances cost money and create security surface area.

## Data Flow Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│ cats.yaml                                                       │
│   accounts_under_test:                                          │
│     prod:                    ← config key                       │
│       account_id: "111..."                                      │
│       account_network_under_test:                               │
│         private:             ← network target name              │
│           vpc_id: vpc-abc                                       │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ CloudFormation Stack (in target account)                        │
│   Outputs:                                                      │
│     Ec2DnsNameprivate: "ip-10-0-1-5.ec2.internal"               │
│     ResponseBucketName: "cats-test-infra-prod-catsresponse..."  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ pytest fixture: prod_account                                    │
│   .session         → boto3 Session with assumed credentials     │
│   .account_id      → "111111111111"                             │
│   .private         → NetworkHandler with EC2 DNS, methods       │
│     .http_call()       → CatsNetworkResult                      │
│     .tcp_call()        → CatsNetworkResult                      │
│     .udp_call()        → CatsNetworkResult (with correlation_id)│
│     .receive_s3(sent)  → bool                                   │
│     .shell()           (requires account_settings.allow_shell: true at deploy time) │
│     .ec2_dns_name                                               │
└─────────────────────────────────────────────────────────────────┘
```

## Network Testing

When you define `account_network_under_test`, CATS deploys infrastructure to make and receive network calls:

- **EC2 listener**: Receives HTTP/TCP/UDP traffic, stores UDP receipts in S3
- **Probe Lambda** (optional): Makes outbound calls from within the VPC

Both handlers run the same probe source, so they classify outcomes identically.
Every call returns a `CatsNetworkResult`: a `CatsNetworkResponse` category
(`SUCCESS`, `REFUSED`, `TIMEOUT`, `DNS_FAILURE`, `TLS_FAILURE`, `FAILURE`, `SENT`)
plus the `reason`, `http_status`, and `correlation_id` evidence behind it.

See [Network Testing](network-testing.md) for full details on handlers, protocols, and VPC endpoints.

The same behavior is also reachable without pytest — as a stateless CLI (`cats run`) and an MCP server (`cats mcp`) for AI agents. See [Agentic](agentic.md).

This lets you prove traffic flows or is blocked:

```python
def test_traffic_flows(prod_account: AccountUnderTest, dev_account: AccountUnderTest) -> None:
    """Prove prod can reach dev on port 443."""
    target = dev_account.private.ec2_dns_name
    result = prod_account.private.http_call(target, 443)
    assert result == CatsNetworkResponse.SUCCESS


def test_traffic_blocked(prod_account: AccountUnderTest) -> None:
    """Prove prod cannot reach the internet."""
    result = prod_account.private.http_call("example.com", 443)
    assert result == CatsNetworkResponse.TIMEOUT
```
