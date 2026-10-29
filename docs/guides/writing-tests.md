# Writing Tests

CATS tests are standard pytest tests. The key difference: account fixtures give you authenticated boto3 sessions in target accounts.

## Account Fixtures

Each account in `cats.yaml` becomes a pytest fixture named `{config_key}_account`:

```yaml
# cats.yaml
global_account_settings:
  additional_roles:
    readonly:
      managed_policies:
        - ReadOnlyAccess

accounts_under_test:
  prod:
    account_id: "111111111111"
  dev:
    account_id: "222222222222"
```

```python
# test_example.py
from cats import AccountUnderTest


def test_prod(prod_account: AccountUnderTest) -> None:
    # prod_account.session is a boto3 Session
    sts = prod_account.role.readonly.client("sts")


def test_both(prod_account: AccountUnderTest, dev_account: AccountUnderTest) -> None:
    # Test interactions between accounts
    pass
```

## The AccountUnderTest Object

| Attribute | Type | Description |
|-----------|------|-------------|
| `session` | `boto3.Session` | The account's default test role, assumed once per account and shared by every probe and client. Its credentials refresh themselves, so a session held for the whole run never expires |
| `role` | `SessionHandler` | Sessions for the roles in `additional_roles`, by name: `account.role.read_only`. Cached the same way as `session` |
| `account_id` | `str` | AWS account ID |
| `name` | `str` | Config key name |
| `tags` | `dict[str, str \| None]` | The account's `tags` from `cats.yaml` |
| `available_network_targets` | `list[str]` | Names of the deployed network targets |
| `deployed_version` | `str \| None` | The CATS release that deployed the stack (the `cats:version` tag) |
| `udp_receipt_token` | `str \| None` | The token this account's UDP listeners require; `udp_call` applies it for you |
| `partition`, `aws_region`, `cfn_stack_prefix` | `str` | The deployment settings the account was built from; with the keyname, the inputs to every deployed role name |
| `network_handlers` | `Sequence[NetworkHandler]` | Every deployed network target, in `cats.yaml` order |
| `network_handlers_by_tag(name, value=None)` | `Iterator[NetworkHandler]` | The targets whose `tags` carry the key (and value, when given) |
| `get_network_handler_by_tag(name, value=None)` | `NetworkHandler` | Exactly one target with the tag; `CatsConfigError` for none or several |
| `{target}` | `NetworkHandler` | Network target (if configured) |

`cats generate-types` writes the same attributes into `CatsAccountProtocol`, so a test typed with
the generated protocols has every one of them.

## Typed Fixtures

`cats generate-types` generates `cats_types.py` with a Protocol per account, plus
`CatsAccountProtocol` for functions that accept any account. With generated types,
the type checker catches typos in network target names and knows your additional
roles (`account.role.readonly` autocompletes).

Each class is named from the account keyname split on `_` and `-`, each part with its first letter
uppercased and the rest kept, joined, plus `Account`: `prod` becomes `ProdAccount`, `my_account`
becomes `MyAccountAccount`, `SharedServices` becomes `SharedServicesAccount`.
Two keynames that map to one class name are a configuration error naming both keynames; rename
one, since a file defining the class twice would type-check as the last one.

The rule: without generated types, annotate fixtures as `AccountUnderTest`. With
generated types, use the protocols everywhere - `ProdAccount` for a specific
account, `CatsAccountProtocol` for any account. Don't mix the two: passing a
protocol-typed fixture to an `AccountUnderTest`-typed function does not type-check.

```python
from cats import CatsNetworkResponse
from cats_types import CatsAccountProtocol, DevAccount, ProdAccount


def assert_ssh_closed(account: CatsAccountProtocol) -> None:
    """Helper accepting any account."""
    for handler in account.network_handlers:
        result = handler.tcp_call(host=handler.ec2_dns_name, port=22)
        # REFUSED: nothing listens on 22 (sshd is disabled on the CATS listener).
        # This is a self-call - traffic to the instance's own IP never leaves the
        # host, so security groups don't apply. A probe from any other host gets
        # TIMEOUT instead: the listener's group only admits configured listen ports.
        assert result == CatsNetworkResponse.REFUSED


def test_ssh_closed_prod(prod_account: ProdAccount) -> None:
    assert_ssh_closed(prod_account)


def test_ssh_closed_dev(dev_account: DevAccount) -> None:
    assert_ssh_closed(dev_account)
```

Re-run `cats generate-types` after every `cats.yaml` change. For the typed boto3
clients behind `.client(...)` (autocomplete and mypy on the generated file),
install the stubs extra: `pip install 'aws-cats[types]'`.

## Basic Assertions

### Prove an API Call Succeeds

```python
def test_can_list_buckets(prod_account: AccountUnderTest) -> None:
    """Verify the test role can list S3 buckets."""
    s3 = prod_account.role.readonly.client("s3")
    response = s3.list_buckets()
    
    assert "Buckets" in response
```

### Prove an API Call is Denied

```python
import pytest
from botocore.exceptions import ClientError


def test_cannot_delete_bucket(prod_account: AccountUnderTest) -> None:
    """Verify SCP blocks bucket deletion."""
    s3 = prod_account.role.readonly.client("s3")

    with pytest.raises(ClientError, match="AccessDenied"):
        s3.delete_bucket(Bucket="critical-bucket")
```

### Prove a Resource Exists

```python
def test_cloudtrail_enabled(prod_account: AccountUnderTest) -> None:
    """Verify CloudTrail is configured."""
    cloudtrail = prod_account.role.readonly.client("cloudtrail")
    trails = cloudtrail.describe_trails()["trailList"]
    
    assert len(trails) > 0
    assert any(t["IsMultiRegionTrail"] for t in trails)
```

## Network Testing

If you've configured `account_network_under_test`, you can test traffic flows:

```python
from cats import AccountUnderTest, CatsNetworkResponse


def test_http_connectivity(
    source_account: AccountUnderTest,
    target_account: AccountUnderTest,
) -> None:
    """Prove source can reach target on port 443."""
    target = target_account.private.ec2_dns_name
    result = source_account.private.http_call(target, 443)

    assert result == CatsNetworkResponse.SUCCESS


def test_http_blocked(prod_account: AccountUnderTest) -> None:
    """Prove prod cannot reach the internet."""
    result = prod_account.private.http_call("example.com", 443)

    # TIMEOUT: no response, consistent with a silent drop. REFUSED would mean the path is open.
    assert result == CatsNetworkResponse.TIMEOUT
```

### Network Methods

| Method | Description |
|--------|-------------|
| `http_call(host, port)` / `https_call(host, port)` | HTTP(S) GET request |
| `tcp_call(host, port)` | TCP connection test |
| `udp_call(host, port)` | UDP packet send; the result carries the generated `correlation_id` |
| `receive_s3(sent)` | Check if the UDP packet from `sent` (a `udp_call` result) was received |
| `shell(command)` | Run shell command on EC2 instance (requires `account_settings.allow_shell: true` at deploy time) |

`host` is a hostname or IP literal (`example.com`, `10.0.1.5`, `fd00::5`, or a bracketed `[fd00::5]`), never a URL: the method chooses the protocol and `port` is separate. A value with a scheme, path, userinfo, port suffix, or whitespace raises `CatsUsageError` before anything is probed, so a pasted URL cannot be misreported as `DNS_FAILURE`.

### Network outcomes

Every network call returns a `CatsNetworkResult`. Compare it with `==` to a
`CatsNetworkResponse` member; `reason`, `http_status`, and `correlation_id` ride
along as diagnostics that print on failure but never take part in equality.

| Outcome | What happened on the wire | In a denial test |
|---------|---------------------------|------------------|
| `SUCCESS` | Connection made; for HTTP any response arrived (`result.http_status` has the code) | Control failed |
| `REFUSED` | RST received: nothing accepted the connection (for HTTP, also a listener that accepted then closed without any response) | Control failed: the path is open |
| `TIMEOUT` | The TCP connect got no answer: consistent with a silent drop | The control worked, if the positive control passed: the endpoint cannot tell enforcement from transient loss |
| `DNS_FAILURE` | Name did not resolve; no packet was sent | The *resolver* acted (DNS Firewall): assert it as such, not as `TIMEOUT` |
| `TLS_FAILURE` | TCP connected, TLS handshake failed (`https_call` only) | TLS inspection with an untrusted CA, or the wrong kind of listener |
| `FAILURE` | The path could not be proven open or blocked: a connect that got no HTTP reply (hung backend), or an unclassified errno in `result.reason` | Never expected: investigate |
| `SENT` | UDP datagram left the host | Prove arrival with `receive_s3` |

A negative (isolation) test needs a listener known to be ready at the target and
an allowed-path positive control in the same run; otherwise a broken listener
passes as "blocked". `TIMEOUT` is the only outcome consistent with a silent DROP.

The full taxonomy, and what the endpoint cannot distinguish, is in
[Network Testing](../concepts/network-testing.md).

### `shell()` — arbitrary commands on the EC2 instance

When `account_settings.allow_shell: true` at deploy time, each target exposes `shell(command)` for running arbitrary shell commands against its EC2 listener through SSM run-command. Commands run as root with the EC2 instance's IAM role, not the test role. The check is runtime-only for ergonomics; real enforcement is the test role's IAM policy set at deploy time. Returns a `ShellResult` with `stdout`, `stderr`, `exit_code`, `duration_ms`, and two truncation flags: SSM returns at most 24,000 characters of stdout and 8,000 of stderr, and a flag is set when a stream came back at exactly that maximum, so the rest is lost. Write large output to S3 from the command instead.

```python
from cats import AccountUnderTest


def test_dns_resolves(prod_account: AccountUnderTest) -> None:
    result = prod_account.private.shell("dig +short example.com")
    assert result.exit_code == 0


def test_multiline_script(prod_account: AccountUnderTest) -> None:
    result = prod_account.private.shell(
        [
            "cd /tmp",
            "echo hello > test.txt",
            "cat test.txt",
        ]
    )
    assert result.stdout == "hello"
```

Non-zero exit codes do not raise — the caller decides whether an exit code means failure. Infrastructure failures (instance unreachable, SSM timeout) raise `CatsSSMError`. Calling `shell()` on a target without `allow_shell: true` raises `CatsConfigError`.

### UDP Testing Pattern

UDP is connectionless, so CATS uses S3 to verify receipt. `udp_call` generates a
correlation ID and returns it on the result; pass the result to `receive_s3` on
the receiving account:

```python
def test_udp_flow(source: AccountUnderTest, target: AccountUnderTest) -> None:
    """Prove UDP traffic flows between accounts."""
    # Send UDP packet
    sent = source.private.udp_call(
        host=target.private.ec2_dns_name,
        port=5000,
    )
    assert sent == CatsNetworkResponse.SENT

    # Verify receipt
    assert target.private.receive_s3(sent) is True
```

`receive_s3` raises `CatsUsageError` if `sent` is not a `SENT` UDP result: a datagram
that never left the host (`DNS_FAILURE`) must not read as "dropped in flight".

## Parameterization

### Test Multiple Ports

A listener answers only on the ports in `tcp_listen_ports`; a self-call to any other port is
`REFUSED` (port 22 above). Parametrize over the configured ports:

```python
import pytest


@pytest.mark.parametrize("port", [80, 443, 8080])  # the account's tcp_listen_ports
def test_port_open(prod_account: AccountUnderTest, port: int) -> None:
    result = prod_account.private.tcp_call(
        host=prod_account.private.ec2_dns_name,
        port=port,
    )
    assert result == CatsNetworkResponse.SUCCESS
```

### Test All Accounts

Parametrize over the account fixture names and resolve each one through the
`get_account_fixture` fixture. It calls `request.getfixturevalue` for you and records the
account in the test's evidence; a bare `request.getfixturevalue(name)` would run the test but
leave `accounts_in_test` empty in `report.json`, because pytest does not list dynamically
resolved fixtures among a test's arguments.

```python
import pytest
from cats import AccountUnderTest, GetAccountFixture, get_account_fixture_names


@pytest.mark.parametrize("account_name", get_account_fixture_names())
def test_all_accounts(account_name: str, get_account_fixture: GetAccountFixture) -> None:
    """Run this test against every configured account."""
    account: AccountUnderTest = get_account_fixture(account_name)

    sts = account.role.readonly.client("sts")
    identity = sts.get_caller_identity()
    assert identity["Account"] == account.account_id
```

`get_accounts_by_tag("env", "prod")` narrows the parametrization to the accounts whose
`cats.yaml` tags match. Both helpers and `GetAccountFixture` are exported from `cats`.

### Multi-account fixtures

Two CATS fixtures are always available:

- `get_account_fixture`: resolve an account fixture by name and record the account in the
  evidence, as above.
- `cats_report`: the test's `CatsReporter`. `cats_report.add_account(account)` records an
  account the test reached some other way; `cats_report.add_report_metadata({...})` attaches
  JSON-serializable values to the test's entry. See [Manual Metadata](#manual-metadata).

## Compliance Markers

Use the `@cats` decorator to map tests to compliance standards:

```python
from cats import cats, AccountUnderTest


@cats(standard_name="SOC2-CC6.1")
def test_encryption_at_rest(prod_account: AccountUnderTest) -> None:
    """Verify S3 buckets have encryption enabled."""
    s3 = prod_account.role.readonly.client("s3")
    # ... test implementation
```

This adds metadata to the test report for audit evidence.

## Tests That Apply Only to Some Deployments

A test pack often has tests that are true of one deployment variant and meaningless
against another: a container deployment has no pipeline to test. Declare
what a test needs with `@cats_requires`, and declare what a deployment provides in
`test_selection.provides` of cats.yaml:

```python
from cats import AccountUnderTest, cats, cats_requires


@cats_requires("container-deployment")
@cats(standard_name="ACME-C-DEP-1")
def test_app_runs_as_ecs_task(prod_account: AccountUnderTest) -> None:
    """Runs only when the deployment provides container-deployment."""


@cats_requires("pipeline-deployment", "container-deployment")
@cats(standard_name="ACME-PIPE-1")
def test_pipeline_signs_container_images(prod_account: AccountUnderTest) -> None:
    """Runs only when both are provided."""
```

```yaml
test_selection:
  provides: [container-deployment]
```

A test runs only when every requirement it names is provided; otherwise it is held back and
recorded in every report as `not_run` with the gap named (`requires {container-deployment,
pipeline-deployment}; not provided {pipeline-deployment}`), where a test `-k` or `-m` removed would leave no record.
Requirement names are free-form, the semantics are AND only, and a test
with no requirements always runs. Stacked decorators union their requirements, and the
decorator applies to a class like any mark.

Providing a requirement only ever runs more tests. It never changes what a test asserts, and
tests cannot read the provided set: `@cats_requires` is a precondition on the deployment, not
a feature flag. A control that behaves differently in a variant is two tests with two
standard names, not one test with a branch. A test pack can provide defaults from its
conftest with the `pytest_cats_provides` hook; see [Configuration](configuration.md#test-selection).

## Test Cleanup

If your test creates resources, clean them up. These examples use an additional role named
`iam_user_writer` with an inline policy allowing `iam:CreateUser`, `iam:GetUser` and `iam:DeleteUser`
on `user/test-user-cats`; `readonly` cannot create anything, and a broad role would grant the test
far more than it needs.

```python
def test_create_and_delete_user(prod_account: AccountUnderTest) -> None:
    """Test that IAM user creation works, then clean up."""
    iam = prod_account.role.iam_user_writer.client("iam")
    username = "test-user-cats"
    
    # Create
    iam.create_user(UserName=username)
    
    # Assert
    user = iam.get_user(UserName=username)
    assert user["User"]["UserName"] == username
    
    # Cleanup
    iam.delete_user(UserName=username)
```

For complex cleanup, use pytest fixtures with `yield`:

```python
import pytest


@pytest.fixture
def test_user(prod_account: AccountUnderTest):
    """Create a test user, yield it, then delete."""
    iam = prod_account.role.iam_user_writer.client("iam")
    username = "test-user-cats"

    iam.create_user(UserName=username)
    yield username
    iam.delete_user(UserName=username)


def test_user_exists(prod_account: AccountUnderTest, test_user: str) -> None:
    iam = prod_account.role.iam_user_writer.client("iam")
    user = iam.get_user(UserName=test_user)
    assert user["User"]["UserName"] == test_user
```

## Table-Driven Tests

Define inputs and expected outputs in a table for complex test matrices:

```python
import pytest
from cats import AccountUnderTest, CatsNetworkResponse

# (source_target, dest_target, port, expected)
traffic_matrix = [
    ("private", "public", 443, CatsNetworkResponse.SUCCESS),
    # 22 is dropped by the security group: a silent drop is TIMEOUT.
    # Pair with the 443 row as the positive control proving the path works.
    ("private", "public", 22, CatsNetworkResponse.TIMEOUT),
    ("public", "private", 443, CatsNetworkResponse.SUCCESS),
]


@pytest.mark.parametrize("source,dest,port,expected", traffic_matrix)
def test_traffic_matrix(
    prod_account: AccountUnderTest,
    source: str,
    dest: str,
    port: int,
    expected: CatsNetworkResponse,
) -> None:
    """Test traffic flows according to expected matrix."""
    source_handler = getattr(prod_account, source)
    dest_handler = getattr(prod_account, dest)
    
    result = source_handler.tcp_call(
        host=dest_handler.ec2_dns_name,
        port=port,
    )
    
    assert result == expected
```

## Multi-Region Testing

Create clients in specific regions to test across your AWS footprint:

```python
from cats import AccountUnderTest


def test_multi_region(prod_account: AccountUnderTest) -> None:
    """Check resources in multiple regions."""
    for region in ["us-east-1", "us-west-2", "eu-west-1"]:
        ec2 = prod_account.role.readonly.client("ec2", region_name=region)
        vpcs = ec2.describe_vpcs()["Vpcs"]
        
        assert len(vpcs) > 0
```

## Skipping Tests

Mark tests to skip or expect failure:

```python
import pytest
from cats import AccountUnderTest


@pytest.mark.skip(reason="Firewall not yet deployed")
def test_firewall_blocks_traffic(prod_account: AccountUnderTest) -> None:
    """Prove firewall blocks unauthorized traffic."""
    pass


@pytest.mark.xfail(reason="Known issue - ticket CATS-123")
def test_flaky_network(prod_account: AccountUnderTest) -> None:
    """Test that sometimes fails due to timing."""
    pass
```

A skip is a decision in test code about a test that is in scope: "not yet", "not on this
Python". A decision about this deployment or this organization's policy, such as a control
that does not apply or a category of tests to hold back during a change freeze, belongs in
`test_selection.excluded` of cats.yaml, with a reason. The test is then recorded in every
report as `not_run` with that reason, the run stays complete, and the decision lives in your
configuration rather than in a test pack you may update from upstream. See
[Configuration](configuration.md#test-selection). When you want neither the control nor a
record of it, delete the test.

## Account Attribution

CATS automatically detects which accounts are used in each test:

```python
def test_cross_account(prod_account: AccountUnderTest, dev_account: AccountUnderTest) -> None:
    pass
```

Report includes:

```json
{
  "nodeid": "tests/test_x.py::test_cross_account",
  "accounts_in_test": [
    { "account_name": "prod", "account_id": "111111111111", "tags": {} },
    { "account_name": "dev", "account_id": "222222222222", "tags": {} }
  ]
}
```

In OSCAL output, the same accounts become deduplicated `InventoryItem` entries in `local-definitions`, and the observation references them via `subjects[].subject-uuid`.

## Manual Metadata

Add arbitrary data to a test's report:

```python
from cats import CatsReporter


def test_with_metadata(prod_account: AccountUnderTest, cats_report: CatsReporter) -> None:
    cats_report.add_report_metadata({"resources_checked": 42, "regions": ["us-east-1", "eu-west-1"]})
```

In OSCAL output these become `Observation.props[]`, keyed by the metadata key.
