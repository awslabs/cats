---
hide:
  - navigation
  - toc
---

# Home

CATS checks that your AWS security controls do what you expect. You write pytest tests that try what a control should allow or block, and CATS runs them against your accounts.

## Write tests like any other code

Each tab shows a test and the entry it leaves in `report.json`.

=== "Network isolation"

    <div class="mdx-code-evidence" markdown>
    <div markdown>
    <p class="mdx-code-evidence__label">The test</p>

    ```python
    @cats(standard_name="PCI-DSS-1.3")
    def test_prod_cannot_reach_dev(prod_account: AccountUnderTest, dev_account: AccountUnderTest):
        """
        Prove production workloads cannot reach dev environments.
        Sends real TCP traffic from prod's subnet to dev's compute.
        Asserts network isolation is enforced.
        """

        result = prod_account.private.tcp_call(dev_account.private.ec2_dns_name, 443)
        assert result == CatsNetworkResponse.TIMEOUT
    ```

    </div>
    <div markdown>
    <p class="mdx-code-evidence__label">The evidence, an excerpt of report.json</p>

    ```json
    {
      "result": "passed",
      "nodeid": "test_isolation.py::test_prod_cannot_reach_dev",
      "standards": ["PCI-DSS-1.3"],
      "accounts_in_test": [
        { "account_name": "prod", "account_id": "111111111111" },
        { "account_name": "dev", "account_id": "222222222222" }
      ],
      "duration_s": 12.904
    }
    ```

    </div>
    </div>

=== "SCP guardrail"

    <div class="mdx-code-evidence" markdown>
    <div markdown>
    <p class="mdx-code-evidence__label">The test</p>

    ```python
    @cats(standard_name="SOC2-CC6.1")
    def test_scp_blocks_guardduty_modification(prod_account: AccountUnderTest):
        """
        Prove SCP actually blocks disabling GuardDuty.
        Attempts the API call and verifies the org-level deny fires.
        """

        client = prod_account.session.client("guardduty")
        detectors = client.list_detectors()["DetectorIds"]

        with pytest.raises(ClientError, match="explicit deny in a service control policy"):
            client.update_detector(DetectorId=detectors[0], Enable=False)
    ```

    </div>
    <div markdown>
    <p class="mdx-code-evidence__label">The evidence, an excerpt of report.json</p>

    ```json
    {
      "result": "passed",
      "nodeid": "test_guardrails.py::test_scp_blocks_guardduty_modification",
      "standards": ["SOC2-CC6.1"],
      "accounts_in_test": [
        { "account_name": "prod", "account_id": "111111111111" }
      ],
      "duration_s": 1.482
    }
    ```

    </div>
    </div>

=== "Every account"

    <div class="mdx-code-evidence" markdown>
    <div markdown>
    <p class="mdx-code-evidence__label">The test</p>

    ```python
    @pytest.mark.parametrize("account", get_account_fixture_names())
    @cats(standard_name="SOC2-CC6.1")
    def test_ebs_encryption_by_default(account: str, get_account_fixture: GetAccountFixture):
        """
        Prove EBS encryption is enabled in every account.
        Runs across all configured accounts automatically.
        """

        client = get_account_fixture(account).session.client("ec2")
        response = client.get_ebs_encryption_by_default()

        assert response["EbsEncryptionByDefault"] is True
    ```

    </div>
    <div markdown>
    <p class="mdx-code-evidence__label">The evidence, an excerpt of report.json</p>

    ```json
    {
      "run": {
        "counts": { "total": 3, "passed": 3, "failed": 0, "error": 0, "skipped": 0, "xfailed": 0, "xpassed": 0 }
      },
      "tests": [
        { "nodeid": "...[prod_account]", "result": "passed" },
        { "nodeid": "...[dev_account]", "result": "passed" },
        { "nodeid": "...[shared_account]", "result": "passed" }
      ]
    }
    ```

    </div>
    </div>

## How it works

Each account in your config becomes a pytest fixture. A test asks for the accounts it needs by name, and CATS assumes the role, builds the boto3 session and writes the report.

```python
import pytest
from botocore.exceptions import ClientError

from cats import AccountUnderTest


def test_s3_access_denied(prod_account: AccountUnderTest) -> None:
    """Prove the SCP blocks S3 public access."""
    s3 = prod_account.role.readonly.client("s3")

    # This should fail due to SCP
    with pytest.raises(ClientError, match="AccessDenied"):
        s3.put_public_access_block(...)
```

For network testing, CATS deploys ephemeral infrastructure (Lambdas, EC2 listeners) to prove traffic flows or is blocked.

## Design choices

- **Behavior over policy analysis:** CATS makes real calls and records what happened, catching policies that read correctly, but don't work as expected.

- **Runs where you do:** Your machine, your CI pipeline, your code to read.

- **Agent and human friendly:** Tests are ordinary pytest, so parametrize, markers, fixtures and plugins work as they always do.

- **Standards aligned:** Map tests to control standards, generating continuous evidence in your choice of [reporting](concepts/reporting.md) format.


## Control reporting

Every test is a claim about a control. `@cats(standard_name="SOC2-CC6.1")` names the control, and
every report records the claim, the accounts it ran against, and whether it held: `report.json`,
OSCAL Assessment Results, JUnit XML, or findings in AWS Security Hub. A test held back by
`test_selection` is recorded as `not_run` with its reason. See
[Reporting and Compliance Alignment](concepts/reporting.md).

## Next steps { .mdx-next-steps__heading }

<div class="mdx-next-steps" markdown="span">
[Installation](getting-started/installation.md){ .md-button .md-button--primary title="Install CATS" }
[Quickstart](getting-started/quickstart.md){ .md-button title="Run your first test" }
[How it works](concepts/how-it-works.md){ .md-button title="The config, deploy and test lifecycle" }
[Writing tests](guides/writing-tests.md){ .md-button title="Patterns and examples" }
[`cats run`](api-reference/CLI.md#cats-run-output){ .md-button title="Use CATS as a CLI" }
</div>
