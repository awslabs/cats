# Identity Model

## Two Identities

| Identity | Used By | Privilege | Purpose |
|----------|---------|-----------|---------|
| **Deploy identity** | `cats infra deploy/destroy` | High | Create IAM roles, EC2, Lambda, S3 |
| **Test identity** | `cats test` | Scoped | Run tests, invoke deployed resources |

## Deploy Identity

The deploy identity is used only during `cats infra` commands. CATS assumes it in each
account under test and drives CloudFormation with it, so it needs administrative rights
over the services below in that account. A role narrower than that is possible but not
supported; if you build one, it needs at least:

| Service | Actions | Why |
|---|---|---|
| CloudFormation | `Create/Update/Delete/DescribeStacks`, `DescribeStackEvents`, `DescribeStackResources`, `ListStackResources` | The stack itself and the progress display |
| IAM | `Create/Delete/Get/UpdateRole`, `Put/Delete/GetRolePolicy`, `Attach/DetachRolePolicy`, `TagRole`, `Create/Delete/Get/AddRoleTo/RemoveRoleFromInstanceProfile`, `PassRole` | The test role, additional roles, the Lambda and EC2 roles, the instance profile |
| EC2 | `RunInstances`, `TerminateInstances`, `DescribeInstances`, `DescribeImages`, `DescribeInstanceTypes`, `DescribeSubnets`, `DescribeRouteTables`, `Create/Delete/DescribeSecurityGroups`, `Authorize/RevokeSecurityGroup*`, `Create/Delete/DescribeVpcEndpoints`, `CreateTags`, `GetConsoleOutput` (the listener instances) | The listener, its groups, the AMI and architecture lookup, the endpoints; reading each listener's serial console until it reports its ports bound, before deploy returns |
| Lambda | `Create/Delete/Get/UpdateFunction*`, `TagResource` | The probe function |
| SSM | `Create/Delete/Describe/UpdateDocument`, `GetParameter` | The probe document; resolving `/parameter` values in `cats.yaml` |
| S3 | `Create/DeleteBucket`, `Put/DeleteBucketPolicy`, `PutBucketPublicAccessBlock`, `PutLifecycleConfiguration`, `PutEncryptionConfiguration`, `ListBucket`, `DeleteObject`, `DeleteObjectVersion` | The response bucket, and emptying it at destroy |

The list follows the template; a release that adds a resource adds to it, so treat the
administrative reading as the supported one.

One requirement sits outside the deploy role, on the identity that runs `cats infra deploy`
itself (the parent-account credentials the deploy role is assumed from): it must be allowed
`sts:AssumeRole` on the CATS test roles in each account. Before reporting a deploy complete,
CATS assumes the test role and each additional role from those credentials to prove IAM has
converged on them, exactly as `cats test` will assume them. The roles trust the parent
account's root, so any identity in the parent account can assume them unless its own
policies or an SCP say otherwise; a CI topology that runs `cats infra deploy` and `cats test`
as different identities needs both allowed to assume the test roles, and a deploy identity
that cannot fails with a message naming this cause.

### Common Deploy Roles

| Role | Created By | Typical Use |
|------|------------|-------------|
| `AWSControlTowerExecution` | Control Tower | Organizations with Control Tower |
| `OrganizationAccountAccessRole` | AWS Organizations | Organizations without Control Tower |
| Custom role | You | Custom deployments |

### Configuration

```yaml
cats_deployment_settings:
  deploy_role_name: AWSControlTowerExecution
```

CATS assumes this role in each target account before deploying.

### Single-Account Mode

For testing the same account you're running from:

```yaml
cats_deployment_settings:
  allow_deploy_to_current_account: true
```

This uses your current credentials instead of assuming a role.

## Test Identity

The test identity is created by CATS during deployment. It's an IAM role in each target account that:

- Trusts the parent account (where tests run)
- Has least-privilege permissions for test operations

### Default Permissions

The CATS-created test role can:

- Invoke CATS-deployed Lambda functions
- Run SSM commands on CATS-deployed EC2 instances
- Read the account's SSM managed-node inventory, to tell a listener that has not
  registered yet from one that refused a command
- Read from the CATS S3 bucket (UDP receipts; the listeners write them)
- Read CloudFormation stack outputs

These grants are one inline policy document on the role, `CatsTestRolePolicy`, with one
statement per grant. Before a deploy reports complete, CATS assumes the role and reads the
stack's outputs as it, which proves the role's trust policy and that document have reached
IAM's authorizer; the grants you add below are not probed (see
[how it works](how-it-works.md)).

### Extending Test Role Permissions

For tests that need additional permissions (e.g., listing S3 buckets, describing EC2 instances), configure managed policies:

```yaml
cats_deployment_settings:
  # ... other settings

accounts_under_test:
  prod:
    account_id: "111111111111"
    account_settings:
      default_test_role_managed_policies:
        - arn:aws:iam::aws:policy/ReadOnlyAccess
```

For least-privilege grants, attach inline policies instead. Each entry is keyed by
policy name, and the value is either a policy document written directly in yaml, or
a string path to a JSON policy file (resolved relative to cats.yaml):

```yaml
accounts_under_test:
  prod:
    account_id: "111111111111"
    account_settings:
      default_test_role_inline_policies:
        dynamo-read:
          Version: "2012-10-17"
          Statement:
            - Effect: Allow
              Action:
                - dynamodb:GetItem
              Resource: arn:aws:dynamodb:*:*:table/my-table
        s3-scoped: ./policies/s3-scoped.json
```

Additional roles support the same via `inline_policies`:

```yaml
global_account_settings:
  additional_roles:
    s3reader:
      inline_policies:
        s3-read: ./policies/s3-read.json
```

!!! warning
    Adding broad policies like `ReadOnlyAccess` increases the blast radius if credentials are compromised. Prefer custom policies with minimum required permissions.

## Trust Relationships

```
┌─────────────────────────────────────────────────────────────────┐
│ Parent Account (999999999999)                                   │
│                                                                 │
│   Your credentials                                              │
│         │                                                       │
│         ▼                                                       │
│   cats infra deploy ──assumes──▶ AWSControlTowerExecution       │
│                                  (in target account)            │
│                                                                 │
│   cats test ──assumes──▶ CATS-test-infra-prod-default-test-role │
│                          (created by CATS in target account)    │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ Target Account (111111111111)                                   │
│                                                                 │
│   AWSControlTowerExecution                                      │
│     - Trusts: Parent account (for deployment)                   │
│     - Permissions: AdministratorAccess                          │
│                                                                 │
│   CATS-test-infra-prod-default-test-role (created by CATS)      │
│     - Trusts: Parent account (for testing)                      │
│     - Permissions: Invoke CATS Lambda, SSM, S3                  │
└─────────────────────────────────────────────────────────────────┘
```

## Session Management

`AccountUnderTest` fixtures expose all deployed roles into tests.

CATS will:

1. Assume a deployed test role name in the target account
2. Create a boto3 Session with temporary credentials
3. Expose the session via `account.role.<rolename>`

```python
def test_example(prod_account: AccountUnderTest) -> None:
    # prod_account.role.readonly assumes the readonly role
    s3 = prod_account.role.readonly.client("s3")
```

## Security Implications

### Deploy Identity Risks

The deploy identity has high privilege. Protect it by:

- Running `cats infra` only from trusted environments
- Using short-lived credentials (IAM Identity Center, OIDC)
- Auditing CloudTrail for role assumption. CATS names its sessions so the trail reads
  unaided: a deploy assumes the deploy role as `cats-infra`, a reported test run assumes
  the test roles as `cats-<run_id>`, and `cats run`, the MCP tools and scripts use
  `cats-cli` (see [Finding a run in CloudTrail](../api-reference/Reports.md#finding-a-run-in-cloudtrail))

### Test Identity Risks

There is a default role deployed with minimal access to
interact with CATS deployed networking. Custom roles
are granted the policies you choose.

The default role is scoped but still has access. Consider:

- Tests can invoke Lambda and SSM in target accounts
- Misconfigured tests could cause unintended side effects
- Test role credentials are short-lived STS credentials, refreshed for as long as the test process holds the account object; they stop refreshing when the process exits. The session duration is CATS's to choose and may change between releases

See [Security](../security.md) for the full threat model.
