# CATS - Control Acceptance Testing Suite

[![PyPI](https://img.shields.io/pypi/v/aws-cats)](https://pypi.org/project/aws-cats/)
[![Python](https://img.shields.io/pypi/pyversions/aws-cats)](https://pypi.org/project/aws-cats/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](https://github.com/awslabs/cats/blob/main/LICENSE)

CATS proves your AWS security controls and applications work by testing them, not
just by checking configuration. Network paths, data perimeters, SCP and IAM enforcement,
service behavior: all probed and proven through CLI, MCP, or pytest tests, across
as many accounts and regions as you point it at.

```python
from cats import AccountUnderTest, CatsNetworkResponse, cats


@cats(standard_name="SOC2-CC6.6")
def test_prod_cannot_reach_dev(
    prod_account: AccountUnderTest,
    dev_account: AccountUnderTest,
) -> None:
    """Prove the production network cannot reach development."""
    target = dev_account.private.ec2_dns_name

    result = prod_account.private.tcp_call(target, 443)

    # TIMEOUT: no response, consistent with a silent drop. REFUSED would mean the path is open.
    assert result == CatsNetworkResponse.TIMEOUT
```

CATS deploys ephemeral test infrastructure into each account under test:
IAM roles, plus EC2 listeners and Lambda probes where you define network
targets. Each account is exposed as a typed pytest fixture, and CATS reports
what the tests proved, including [OSCAL assessment results](https://github.com/awslabs/cats/blob/main/docs/guides/reporting.md)
for compliance evidence. AI agents can interact with the deployed
infrastructure through MCP or the `cats run` CLI.

## How it works

```
cats infra deploy  →  cats test  →  cats infra destroy
```

A parent account assumes a deploy role in each account under test to create a
CloudFormation stack (test role, optional network listeners), tests run
against those resources through least-privilege test roles, and the stack is
destroyed. Infrastructure is ephemeral by design: deploy, test, destroy, every
cycle.

**Security note:** CATS runs with high privilege across many
accounts using hub-and-spoke IAM trust. Treat its parent account, its deploy
role, and its configuration as production security infrastructure. See the
[identity model](https://github.com/awslabs/cats/blob/main/docs/concepts/identity-model.md)
and [security model](https://github.com/awslabs/cats/blob/main/docs/security.md).

## Use CATS with AI agents

`cats mcp` (install `aws-cats[mcp]`) exposes deployed CATS infrastructure as
MCP tools. `cats run` provides the probing operations through a shell interface
with JSON output, with no optional extra to install.

Agents can use either interface to inspect accounts and test network paths
before writing repeatable pytest tests. Generate the supporting agent
documentation with `cats prompts --all`; start initial configuration with
`cats prompts setup`.

See [Agentic](https://github.com/awslabs/cats/blob/main/docs/concepts/agentic.md)
and the [MCP reference](https://github.com/awslabs/cats/blob/main/docs/api-reference/MCP.md).

## Install

Requires Python 3.11+.

```bash
uv init && uv add aws-cats    # in a new directory; or: pip install aws-cats
```

## Quickstart

You need AWS credentials in a parent account, and a role CATS can assume in
each account under test (`AWSControlTowerExecution` in Control Tower
organizations, `OrganizationAccountAccessRole`, or your own).

```bash
export ACCOUNT_ID=<your_account_id>
uv run cats init --parent-account-id $ACCOUNT_ID --deploy-role-name "AWSControlTowerExecution"
```

Add an account under test to the generated `cats.yaml`:

```yaml
global_account_settings:
  additional_roles:
    readonly:
      managed_policies:
        - ReadOnlyAccess

accounts_under_test:
  myaccount:
    account_id: "123456789012"
```

Deploy, write a test, run it, clean up:

```bash
uv run cats infra deploy myaccount
```

```python
from cats import AccountUnderTest


def test_sts_works(myaccount_account: AccountUnderTest) -> None:
    result = myaccount_account.role.readonly.client("sts").get_caller_identity()
    assert result["Account"] == myaccount_account.account_id
```

```bash
uv run cats test
uv run cats infra destroy myaccount
```

Every account in `cats.yaml` becomes a `<name>_account` fixture. `cats
generate-types` gives the fixtures real types for IDE autocomplete and mypy
(install `aws-cats[types]` for the typed boto3 clients behind `.client(...)`).
The full walkthrough is in the
[quickstart](https://github.com/awslabs/cats/blob/main/docs/getting-started/quickstart.md).

## Documentation

The rendered documentation is at [awslabs.github.io/cats](https://awslabs.github.io/cats/);
the same pages are in `docs/` as Markdown, where the tabs, admonitions and API reference
blocks render as plain text.

| | |
|---|---|
| [Getting started](https://github.com/awslabs/cats/blob/main/docs/getting-started/quickstart.md) | Installation, first test, [multi-account setup](https://github.com/awslabs/cats/blob/main/docs/getting-started/multi-account.md) |
| [Concepts](https://github.com/awslabs/cats/blob/main/docs/concepts/how-it-works.md) | [How it works](https://github.com/awslabs/cats/blob/main/docs/concepts/how-it-works.md), [identity model](https://github.com/awslabs/cats/blob/main/docs/concepts/identity-model.md), [network testing](https://github.com/awslabs/cats/blob/main/docs/concepts/network-testing.md), [versioning and compatibility](https://github.com/awslabs/cats/blob/main/docs/concepts/versioning.md) |
| [Guides](https://github.com/awslabs/cats/blob/main/docs/guides/writing-tests.md) | [Writing tests](https://github.com/awslabs/cats/blob/main/docs/guides/writing-tests.md), [configuration](https://github.com/awslabs/cats/blob/main/docs/guides/configuration.md), [infrastructure](https://github.com/awslabs/cats/blob/main/docs/guides/infrastructure.md), [reporting and OSCAL](https://github.com/awslabs/cats/blob/main/docs/guides/reporting.md), [standards registry](https://github.com/awslabs/cats/blob/main/docs/guides/standards-registry.md) |
| [Security](https://github.com/awslabs/cats/blob/main/docs/security.md) | Threat model, listener authentication, least privilege |
| [Troubleshooting](https://github.com/awslabs/cats/blob/main/docs/troubleshooting.md) | When deploys or tests misbehave |

Run `cats --help` for the complete command reference, or see the [CLI reference](https://awslabs.github.io/cats/api-reference/CLI.html) (commands, exit codes, streams).

## Developing

```bash
uv sync
uv run pre-commit run --all-files   # lint, format, types, license headers
uv run pytest tests/unit_tests/
uv run mkdocs serve                 # docs at http://127.0.0.1:8000
```

The integration suite deploys real stacks and is run on its own, `uv run pytest tests/integration/`,
with the environment variables `tests/integration/constants.py` reads. Run the two suites
separately: the integration suite's `conftest.py` declares itself a test pack, and a `pytest tests/`
run that collects both loads it too late for that, which CATS reports as a warning.

CATS is published as open source but is
[not currently accepting contributions](https://github.com/awslabs/cats/blob/main/CONTRIBUTING.md).
Bug reports are welcome as [GitHub issues](https://github.com/awslabs/cats/issues/new/choose);
potential security issues go through [SECURITY.md](https://github.com/awslabs/cats/blob/main/SECURITY.md)
instead.

## Usage attribution

CATS adds `AwsCats/<version>` to the HTTP User-Agent header of the AWS API
calls it makes, so that usage of CATS can be attributed to the project in AWS
service logs. A test suite built on CATS may declare its own name and version
through the `pytest_cats_test_pack` hook, which CATS adds to the same header as
`AwsCatsPack/<name>#<version>`, optionally followed by release metadata the
suite declares. Nothing else is added, collected, or transmitted.

## Security

Report potential security issues according to
[SECURITY.md](https://github.com/awslabs/cats/blob/main/SECURITY.md), not as
public GitHub issues.

## License

Apache-2.0. See [LICENSE](https://github.com/awslabs/cats/blob/main/LICENSE)
and [NOTICE](https://github.com/awslabs/cats/blob/main/NOTICE).
