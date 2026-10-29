# Quickstart

Test the account you're running from.

## 1. Initialize Configuration

With AWS credentials available, create a `cats.yaml`:

```bash
cats init --parent-account-id 123456789012
```

Replace `123456789012` with your AWS account ID.

This creates:

- `cats.yaml` - CATS configuration
- `.schema.json` - JSON schema for editor validation
- `tests/example_test.py` - a starter test using your account fixture
- `pytest.ini` - pins pytest's root directory at the project, so test identifiers in reports are stable (added to an existing `pyproject.toml` instead when there is one)

## 2. Add an Account Under Test

`cats init` writes a placeholder account, `myaccount`, with a made-up account ID, and expects a
deploy role to assume in each account under test. For this single-account walkthrough, edit
`cats.yaml` so the account under test is the account you are running from:

```yaml
cats_deployment_settings:
  allow_deploy_to_current_account: true
  parent_account_id: "123456789012"
  aws_region: us-east-1

global_account_settings:
  additional_roles:
    readonly:
      managed_policies:
        - ReadOnlyAccess

accounts_under_test:
  myaccount:
    account_id: "123456789012"
```

`cats validate` names every `cats init` placeholder still in the file, so run it before deploying.

!!! note
    `allow_deploy_to_current_account: true` permits testing the same account you're running from. For production use with multiple accounts, see the [multi-account guide](multi-account.md).

## 3. Deploy Test Infrastructure

Deploy the CATS IAM role to your account:

```bash
cats infra deploy myaccount
```

This creates a CloudFormation stack with an IAM role that CATS uses during tests.

## 4. Write Your First Test

Create `tests/test_first.py`:

```python
from cats import AccountUnderTest


def test_sts_works(myaccount_account: AccountUnderTest) -> None:
    """Verify CATS can assume the test role and call STS."""
    sts = myaccount_account.role.readonly.client("sts")
    identity = sts.get_caller_identity()

    assert identity["Account"] == myaccount_account.account_id
```

The fixture name `myaccount_account` comes from your config key (`myaccount`) plus `_account`.

## 5. Run the Test

```bash
cats test
```

You should see:

```
tests/test_first.py::test_sts_works PASSED
```

## 6. Clean Up

Remove the test infrastructure:

```bash
cats infra destroy myaccount
```

## What's Next

You've proven CATS can assume a role in your account and make API calls. From here:

- [Writing Tests](../guides/writing-tests.md) - Fixtures, assertions, and test patterns
- [Network Testing](../concepts/network-testing.md) - Deploy EC2/Lambda to test traffic flows
- [Multi-Account Setup](multi-account.md) - Expand to hub-and-spoke testing
- [Reporting](../guides/reporting.md) - Map tests to compliance standards

## Example: Prove an Action is Denied

A test that proves a role cannot do something it should not:

```python
import pytest
from botocore.exceptions import ClientError
from cats import AccountUnderTest


def test_readonly_cannot_stop_logging(myaccount_account: AccountUnderTest) -> None:
    """Prove the read-only role cannot stop a CloudTrail trail."""
    cloudtrail = myaccount_account.role.readonly.client("cloudtrail")

    with pytest.raises(ClientError, match="AccessDenied"):
        cloudtrail.stop_logging(Name="my-trail")
```

The same shape proves an SCP: use `myaccount_account.session` (the default test role) for the client and the
denial comes from the organization policy instead.
