# Reporting and Compliance Alignment

Every CATS test is a claim about a control. The report records the claim, the accounts it ran
against, and whether it held. The test is how the evidence was gathered; the report is the
evidence.

## A test names its control

`@cats(standard_name=...)` labels a test with the control it proves. The name is yours: a
catalog ID used as it is (`SC-7`, `CC6.1`), a framework reference (`SOC2-CC6.1`), or an
internal name (`ACME-G-DCP-1`).

```python
@cats(standard_name="SOC2-CC6.1")
@pytest.mark.parametrize("account", get_account_fixture_names())
def test_ebs_encryption_by_default(account: str, get_account_fixture: GetAccountFixture) -> None:
    """Prove EBS encryption by default is on."""
    ec2 = get_account_fixture(account).session.client("ec2")
    assert ec2.get_ebs_encryption_by_default()["EbsEncryptionByDefault"] is True
```

A parametrized test that runs across twenty accounts is twenty claims, each attributed to its
account. One test that takes two account fixtures is one claim attributed to both. The report
entry for each carries the standard, the accounts, the outcome and the test's docstring:

```json
{
  "nodeid": "tests/test_encryption.py::test_ebs_encryption_by_default[prod_account]",
  "result": "passed",
  "standards": ["SOC2-CC6.1"],
  "accounts_in_test": [{ "account_name": "prod", "account_id": "111111111111" }],
  "test_docstring": "Prove EBS encryption by default is on."
}
```

## Names resolve to controls

A `standard_name` is a label. When the OSCAL output must name catalog controls and the label is
not one, CATS resolves it: per test, through `control_ids=[...]` on the decorator, or for a
whole project, through the `pytest_cats_standards` hook, which returns a mapping from your names
to catalog IDs from a workbook, a CSV or a database. One friendly name can resolve to several
controls, and each becomes its own finding. See [Standards registry](../guides/standards-registry.md).

## One run, every format

A run writes the same record in every format listed under `report_formats`:

| Format | For |
|---|---|
| `report.json` | Scripts and dashboards; the CATS-native contract |
| OSCAL Assessment Results | Compliance tooling such as compliance-trestle; one observation per test, one finding per control |
| JUnit XML | CI systems; every CATS property on every testcase |
| AWS Security Hub | The account's owner; one finding per test in each account it ran against, where the security team already looks |

The formats differ in shape, not content. A failed test is a `failed` entry, a `not-satisfied`
finding, a JUnit `<failure>` and an `ACTIVE` Security Hub finding, all from the same record.

## The report says what was not checked

A test held back by `test_selection` is recorded as `not_run` with its reason, in every format
that can carry it. An exclusion in `cats.yaml` names the standard and why it is not tested here,
and the report repeats both. This is the difference from narrowing a run with `-k` or `-m`: those
leave only the selector in `run.selection`, while an exclusion leaves a record an auditor can read.
See [Test selection](../guides/configuration.md#test-selection).

## The report says how it was produced

The `run` block records the CATS, Python and pytest versions, the git commit and whether the tree
was dirty, the SHA-256 of `cats.yaml`, the pytest command line, the identity CATS ran as and the
`RoleSessionName` every assumed role carried. A report is reproducible from that block, and every
API call the run made in an account under test is findable in CloudTrail from the session name.

## Where to go next

- [Reporting guide](../guides/reporting.md): choosing formats, the report directory, filtering by standard, the worked compliance example.
- [Standards registry](../guides/standards-registry.md): resolving your names to catalog controls.
- [Decorator](../api-reference/Decorator.md): every `@cats` option.
