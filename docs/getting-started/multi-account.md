# Multi-Account Setup

Add accounts under test and deploy to all of them. A high-privilege deploy role creates the test
infrastructure in each account; tests then run as a scoped role CATS creates there, so the two
privilege levels never share an identity.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│ Parent Account (runs CATS)                                      │
│   - Runs CATS tests                                             │
│   - Assumes the deploy role that exists in each target account  │
└─────────────────────────────────────────────────────────────────┘
                              │
          ┌───────────────────┼───────────────────┐
          ▼                   ▼                   ▼
┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐
│ Prod Account    │ │ Dev Account     │ │ Shared Services │
│ (Spoke)         │ │ (Spoke)         │ │ (Spoke)         │
│ - CATS IAM role │ │ - CATS IAM role │ │ - CATS IAM role │
│ - Test infra    │ │ - Test infra    │ │ - Test infra    │
└─────────────────┘ └─────────────────┘ └─────────────────┘
```

## Prerequisites

1. A parent account: the account tests run from, whose ID goes in `cats_deployment_settings.parent_account_id` and which the test roles trust
2. An existing cross-account role in each target account (e.g., `AWSControlTowerExecution`, `OrganizationAccountAccessRole`, or a custom-deployed role) that the credentials running `cats infra` can assume
3. AWS credentials for the parent account (the one that runs CATS)

## Configuration

```yaml
cats_deployment_settings:
  parent_account_id: "999999999999"          # Parent account: where CATS runs
  deploy_role_name: AWSControlTowerExecution  # Role to assume for deployment
  aws_region: us-east-1

global_account_settings:
  additional_roles:
    readonly:
      managed_policies:
        - ReadOnlyAccess

accounts_under_test:
  prod:
    account_id: "111111111111"
    account_name: "Production"
  
  dev:
    account_id: "222222222222"
    account_name: "Development"
  
  shared:
    account_id: "333333333333"
    account_name: "Shared Services"
```

## Adding Network Targets

For network testing, add `account_network_under_test` and configure listen ports:

```yaml
global_network_settings:
  tcp_listen_ports: [443, 8080]
  udp_listen_ports: [5000]

accounts_under_test:
  prod:
    account_id: "111111111111"
    account_network_under_test:
      private:
        vpc_id: vpc-abc123
        subnet_ids: [subnet-aaa111]
      dmz:
        vpc_id: vpc-abc123
        subnet_ids: [subnet-ddd333]
  
  dev:
    account_id: "222222222222"
    account_network_under_test:
      private:
        vpc_id: /ssm/dev/vpc-id        # SSM parameter reference
        subnet_ids:
          - /ssm/dev/private-subnet    # Resolved at deploy time
```

## Deploy to All Accounts

Deploy CATS infrastructure to all configured accounts in parallel:

```bash
cats infra deploy --all
```

Or deploy to specific accounts:

```bash
cats infra deploy prod dev
```

## Check Deployment Status

```bash
cats infra status
```

Output shows CloudFormation stack status and the CATS release that deployed each stack:

```
Account          Status              Stack                    Version
prod             CREATE_COMPLETE     CATS-test-infra-prod     1.0.0
dev              CREATE_COMPLETE     CATS-test-infra-dev      1.0.0
shared           not deployed        -                        -
```

## Write Cross-Account Tests

```python
from cats import AccountUnderTest, CatsNetworkResponse


def test_prod_cannot_reach_dev(
    prod_account: AccountUnderTest,
    dev_account: AccountUnderTest,
) -> None:
    """Prove production cannot reach development network."""
    target = dev_account.private.ec2_dns_name
    
    result = prod_account.private.http_call(target, 443)
    
    # TIMEOUT: no response, consistent with a silent drop. REFUSED would mean the path is open.
    assert result == CatsNetworkResponse.TIMEOUT


def test_shared_can_reach_all(
    shared_account: AccountUnderTest,
    prod_account: AccountUnderTest,
    dev_account: AccountUnderTest,
) -> None:
    """Prove shared services can reach both prod and dev."""
    for target_account in [prod_account, dev_account]:
        target = target_account.private.ec2_dns_name
        result = shared_account.private.http_call(target, 443)
        assert result == CatsNetworkResponse.SUCCESS
```

## Run Tests

```bash
cats test
```

## Destroy All Infrastructure

CATS is ephemeral by intent - end every testing session by cleaning up.

```bash
cats infra destroy --all
```

## CI/CD Pattern

In pipelines, always ensure cleanup runs:

```yaml
# GitLab CI example
test:
  script:
    - cats infra deploy --all
    - cats test
  after_script:
    - cats infra destroy --all  # Runs even if tests fail
```
