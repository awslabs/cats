# Network Testing

CATS proves a network path by probing it: a **source** makes a call to a **target** and reports
what came back on the wire. Running a probe needs three things — the rest of this page is how CATS
provides each, and how you work around the cases where it cannot:

- a **source** that runs the probe: the EC2 handler (runs on a CATS listener via SSM) or the Lambda
  handler (invoked from outside the VPC, over the control plane);
- a **target** to probe: a CATS listener, or any host, IP or DNS name — whether the path to it is
  open or blocked is what you are testing, not a precondition;
- the test role's permission to drive the source — invoke the Lambda, or send the SSM command.

Per target you declare in `account_network_under_test`, CATS deploys an **EC2 listener** (receives
HTTP/TCP/UDP on the ports you configure — the usual target), an optional **probe Lambda** (an
outbound source inside the VPC), and an **S3 bucket** (UDP receipts).

## Handlers

CATS supports two network handlers:

| Handler | How It Works | Best For |
|---------|--------------|----------|
| `ec2` | SSM commands on EC2 instance | Targets whose listener reaches SSM ([choosing the handler](#choosing-the-handler-for-a-network-target)) |
| `lambda` | Lambda function invocation | Targets whose listener cannot; faster probes. Destroy waits up to about 20 minutes for Lambda to release its network interfaces unless the target uses `existing_security_group_ids` ([how long destroy takes](../guides/infrastructure.md#how-long-destroy-takes)) |

### Configuration

```yaml
global_network_settings:
  enabled_network_handlers: [ec2]        # Default
  # enabled_network_handlers: [lambda]   # Lambda only
  # enabled_network_handlers: [ec2, lambda]  # Both (first is default)
```

The first handler in the list is used by `http_call()`, `tcp_call()`, `udp_call()`.

### Explicit Handler Methods

Force a specific handler:

```python
# Always use EC2/SSM
result = account.private.ec2_http_call(target, 443)

# Always use Lambda
result = account.private.lambda_http_call(target, 443)
```

## Network Methods

| Method | Protocol | Returns |
|--------|----------|---------|
| `http_call(host, port)` | HTTP GET | `CatsNetworkResult` |
| `https_call(host, port)` | HTTPS GET | `CatsNetworkResult` |
| `tcp_call(host, port)` | TCP connect | `CatsNetworkResult` |
| `udp_call(host, port)` | UDP send | `CatsNetworkResult` (with `correlation_id`) |
| `receive_s3(sent)` | Check S3 for the receipt of a `udp_call` result | `bool` |
| `shell(command)` | Arbitrary shell command (requires `account_settings.allow_shell: true` at deploy time) | `ShellResult` |

Each `*_call` has `ec2_` and `lambda_` variants that force a handler.

### The result object

Every network call returns a `CatsNetworkResult`: a `CatsNetworkResponse` category
plus the evidence behind it.

```python
result = account.private.https_call("example.com", 443)

result == CatsNetworkResponse.SUCCESS  # category-only equality: this is the assertion to write
result.response  # CatsNetworkResponse.SUCCESS
result.reason  # None on SUCCESS; the probe's error text otherwise
result.http_status  # 200 - only set for HTTP/HTTPS SUCCESS
result.correlation_id  # None - only set for UDP
```

Equality and hashing are category-only, so `reason` and `http_status` can never
become load-bearing in an assertion by accident; anything worth asserting has to
be a `CatsNetworkResponse` member (or an explicit assertion on `http_status`).
Comparing to a bare string is refused: `result == "TIMEOUT"` is `False`. Use
`==`, not `is`: `result is CatsNetworkResponse.TIMEOUT` is always `False`.

The diagnostics are printed by the repr, so a failing assertion is
self-diagnosing with no extra code:

```
AssertionError: assert DNS_FAILURE('[Errno -2] Name or service not known') == <CatsNetworkResponse.TIMEOUT: 'TIMEOUT'>
```

### Response Values

Each value is a precise claim about what the probing endpoint observed on the
wire. Which control produced it is interpretation, and belongs in your test's
docstring.

```python
from cats import CatsNetworkResponse

CatsNetworkResponse.SUCCESS  # Connection established. Any HTTP response counts,
# including 3xx/4xx/5xx (redirects are not followed); result.http_status has the code
CatsNetworkResponse.REFUSED  # RST received (refused or reset) - the path is OPEN,
# nothing accepted. For HTTP, also a listener that accepted then closed without any
# response. Expected traffic to be blocked? The control is NOT working
CatsNetworkResponse.TIMEOUT  # The TCP connect got no answer - consistent with a
# silent drop. The expected evidence for SG/NACL/Network Firewall DROP/route table/
# data perimeter tests; the endpoint cannot tell enforcement from transient loss
CatsNetworkResponse.DNS_FAILURE  # Name resolution failed (NXDOMAIN/NODATA, or the
# resolver was unreachable). What DNS Firewall blocks produce; no packet was sent
CatsNetworkResponse.TLS_FAILURE  # TCP connected, TLS handshake failed: untrusted CA,
# expired/mismatched certificate, plaintext listener. https_call only
CatsNetworkResponse.FAILURE  # The path could not be proven open or blocked. Includes
# a connect that succeeded with no HTTP reply inside the budget (a hung backend, NOT
# a silent drop; result.reason starts "connected; no HTTP reply") and unclassified
# errors (ENETUNREACH, ...). Never an expected value - investigate before trusting
CatsNetworkResponse.SENT  # UDP datagram left the host (arrival unconfirmed)
```

Assert the exact outcome. A "traffic is blocked" test must assert `TIMEOUT`,
not just "not SUCCESS" - a dead listener returns `REFUSED`, which proves the
network path works and should fail your blocked-traffic test. A `DNS_FAILURE`
means the resolver acted, not the network: assert it as `DNS_FAILURE`, never as
a stand-in for `TIMEOUT`.

UDP can only return `SENT`, `DNS_FAILURE`, or `FAILURE`. The probe sends on an
unconnected socket, so `REFUSED` and `TIMEOUT` are unreachable: the datagram
leaving the host is all the sender can observe. Arrival is proven by
`receive_s3`.

### What the endpoint cannot distinguish

The taxonomy is stated at the level the probing endpoint can actually observe.
These pairs look identical from there:

| Indistinguishable from the endpoint | Why | Guidance |
|---|---|---|
| SG drop vs NACL drop vs Network Firewall DROP | All silent drops, all `TIMEOUT` | Localizing among them is config-testing territory |
| Network Firewall REJECT vs nothing listening | Both are an RST | `REFUSED` is inherently ambiguous; in a denial test, `REFUSED` **fails** the isolation assertion |
| DNS Firewall NXDOMAIN vs nonexistent name | Same `EAI_NONAME` | Probe names known to exist |
| DNS Firewall OVERRIDE vs the real target | OVERRIDE resolves the name to a custom IP; the probe then reports whatever that IP does (`SUCCESS`, `REFUSED`, `TIMEOUT`) | Not visible as a DNS outcome at all. Assert on the override target's behavior, or resolve the name in a `shell()` probe |
| Proxy-injected response vs real response | Both are an HTTP response, both `SUCCESS` | `http_status` is on the result; a block page is usually 403/407/451 where the real target would be 200. Assert the integer when the distinction matters |
| TLS interception with an untrusted CA vs plaintext listener on the port vs expired certificate vs hostname mismatch | All fail the handshake, all `TLS_FAILURE` | `result.reason` (OpenSSL text: `CERTIFICATE_VERIFY_FAILED`, `WRONG_VERSION_NUMBER`, ...) usually names which; pair with a positive control against a known-good TLS endpoint |
| Empty or blackholed route table vs any other silent drop | The instance always has a default route to the VPC router (DHCP), so packets leave the instance and the VPC drops them silently; the AWS data plane returns no ICMP unreachable | `TIMEOUT`. There is no `NO_ROUTE` category: no supported AWS-native control produces `ENETUNREACH`/`EHOSTUNREACH`. When those errnos do appear (an on-premises device across DX/VPN sending ICMP unreachables, an IPv6-only target from an IPv4-only subnet, instance route tampering) they are `FAILURE` with the errno in `reason` |

### Address families

`tcp_call`, `http_call` and `https_call` try every address `getaddrinfo` returns for
the target, in resolver order, and stop at the first that connects; if none does, the
last address's error is the one classified (the same rule `socket.create_connection`
follows, so the HTTP request agrees with its own TCP preflight). `udp_call` sends to
the first address only: a datagram has one destination. An IPv6-only target probed
from an IPv4-only subnet is `FAILURE` (`ENETUNREACH`) on every protocol. A dual-stack
target from an IPv4-only subnet is decided by its IPv4 address, because the IPv6
attempt fails and the IPv4 one is tried next; when the addresses fail *differently*
(one dropped, one refused) the last one decides, so prefer single-family targets or IP
literals when that distinction matters. IPv6 literals may be passed bare (`fd00::5`)
or bracketed (`[fd00::5]`).

## HTTP Testing

Test HTTP connectivity:

```python
def test_http_works(source: AccountUnderTest, target: AccountUnderTest) -> None:
    result = source.private.http_call(
        host=target.private.ec2_dns_name,
        port=443,
    )
    assert result == CatsNetworkResponse.SUCCESS
```

`SUCCESS` means an HTTP response arrived, whatever its status. When the status
matters (a proxy answering `403` in place of the real target, say), make a
second, explicit assertion on the integer:

```python
def test_http_reaches_the_real_target(source: AccountUnderTest, target: AccountUnderTest) -> None:
    result = source.private.http_call(target.private.ec2_dns_name, 8080)
    assert result == CatsNetworkResponse.SUCCESS
    assert result.http_status == 200
```

Redirects are not followed: a `302` is reported as `SUCCESS(http_status=302)`,
not as whatever the redirect target would have said.

## TCP Testing

Test raw TCP connectivity:

```python
@pytest.mark.parametrize("port", [22, 80, 443])
def test_tcp_ports(account: AccountUnderTest, port: int) -> None:
    result = account.private.tcp_call(
        host=account.private.ec2_dns_name,
        port=port,
    )
    assert result == CatsNetworkResponse.SUCCESS
```

## UDP Testing

UDP is connectionless, so CATS uses S3 to verify receipt. `udp_call` generates a
correlation ID, sends it as the datagram, and returns it on the result; pass that
result to `receive_s3` on the receiving account:

```python
def test_udp_flow(source: AccountUnderTest, target: AccountUnderTest) -> None:
    # Send UDP packet
    sent = source.private.udp_call(
        host=target.private.ec2_dns_name,
        port=5000,
    )
    assert sent == CatsNetworkResponse.SENT

    # Verify receipt (EC2 listener writes to S3)
    assert target.private.receive_s3(sent) is True
```

`receive_s3` raises `CatsUsageError` rather than returning `False` if `sent` is not a
UDP result, or if the datagram never left the host (`DNS_FAILURE`, `FAILURE`). A
test asserting `receive_s3(...) is False` is claiming "dropped in flight"; a
datagram that was never sent must not be allowed to pass that claim.

## UDP Receipt Token

UDP receipt ingestion is authenticated, always — there is no off switch. Each
account's stack carries a per-account token (the `UdpReceiptToken` stack output),
and the EC2 listener drops any datagram that does not carry it as a `{token}:`
message prefix. Without it, any packet with network reach would become an S3
write — a cost and abuse vector.

This is transparent to every CATS flow: `udp_call` learns the target's token
automatically at test time and prepends it in transport, `receive_s3` sees the
bare correlation ID, and no test signature changes. No token is ever sent to
non-CATS targets. Automatic tokening keys on the listener's `ec2_dns_name` —
address UDP targets by `target.private.ec2_dns_name`, not by IP, or the
datagram is sent bare and the listener drops it.

Be clear about what an application-layer token can and cannot do — it cannot
close a port. The kernel receives the datagram before any CATS code runs, so a
scanner sees the port open either way:

| Concern | Control |
|---------|---------|
| Port is reachable at all | Security groups: CATS-managed listener groups admit only the configured listen ports, one IPv4 and one IPv6 rule per port |
| Unauthenticated side effects (UDP → S3 write) | UDP receipt token — valid token required before any S3 write |
| Unauthenticated data disclosure (TCP/HTTP banner) | Banner removed — the listener answers any received data with a fixed, content-free reply (today an empty `200 OK`) |

The token is a network-abuse guard, **not a bearer credential for anything
IAM-protected**. It appears in stack outputs deliberately, readable under the
existing `cloudformation:DescribeStacks` grant.

### External senders

Non-CATS senders using a listener as a receipt sink still work — include the
prefix, readable from the `udp_receipt_token` property (or the `UdpReceiptToken`
stack output). `receive_s3` takes a `CatsNetworkResult`, so describe the send
you observed as one:

```python
from uuid import uuid4

from cats import CatsNetworkResponse, CatsNetworkResult


def test_raw_udp_from_appliance(account: AccountUnderTest) -> None:
    correlation_id = str(uuid4())
    token = account.udp_receipt_token
    # have your sender transmit f"{token}:{correlation_id}"
    sent = CatsNetworkResult(response=CatsNetworkResponse.SENT, correlation_id=correlation_id)
    assert account.private.receive_s3(sent) is True
```

### Rotation

There is no rotation mechanism: rotation is `cats infra destroy` + `cats infra
deploy`, which mints a fresh token. This matches the ephemeral lifecycle — if you
destroy and redeploy every test cycle, every cycle rotates for free.

### Diagnosing drops

Dropped datagrams are logged on the listener, distinguishing "dropped by auth"
from "never arrived" (requires `allow_shell: true`):

```python
account.private.shell("journalctl -u udpserver")
# "dropped datagram: missing or mismatched receipt token"
```

## Port Configuration

Configure which ports the EC2 listener accepts:

```yaml
global_network_settings:
  tcp_listen_ports: [22, 80, 443, 8080]
  udp_listen_ports: [5000, 5001]
```

You can only test connectivity on ports you've configured here. Each port becomes
two ingress rules, IPv4 and IPv6, on the listener's CATS-managed security group
(combined limit: 30 ports per target, enforced by `cats validate`, against the
default quota of 60 rules per group). Traffic to any other port is
dropped by the group and returns `TIMEOUT` — to prove a path is open on a port,
add it here and assert `SUCCESS`. One exception: a listener probing itself
bypasses security groups (traffic to its own IP never leaves the host), so
self-calls to unlisted ports return `REFUSED`. For any other ingress posture,
attach your own groups with `existing_security_group_ids`.

### Security groups on listeners

The CATS-managed group admits each configured port from any source, `0.0.0.0/0` and `::/0`.
That is deliberate: a listener exists to be probed from other subnets, VPCs and accounts, and
CATS cannot know which. The group is not the control under test; the routes, network ACLs,
firewalls and peering between the probe and the listener are, and an admit-all group is the only
posture that lets a test read their verdict rather than the group's. A listener never has a
public IPv4 address, whatever its subnet's `MapPublicIpOnLaunch` setting, so over IPv4 the group
is reachable only from inside the VPC and whatever routes into it; and the listener itself opens
nothing but the configured ports (sshd is disabled; management is outbound-only through SSM).
Egress from the listener and the probe Lambda is unrestricted for the same reason: a probe must
be able to reach whatever the test names.

IPv6 is the exception. In a subnet that assigns IPv6 addresses on launch, the listener's address
is globally routable, and a `::/0` route to an internet gateway (not an egress-only one) exposes
every listen port to the internet. CATS does not suppress the address, because an IPv6-only
subnet needs it and the `::/0` rules exist so that IPv6 paths can be probed. Where that exposure
must not happen, create a group that admits what you intend and attach it with
`existing_security_group_ids`; CATS then creates no group of its own for that target. Template
scanners (cfn_nag, checkov) report the open ingress and egress on every rendered CATS template,
and Security Hub reports it on the deployed groups; that is this posture, not a defect.

## Choosing the handler for a network target

The EC2 handler is a source only where the target's listener can reach SSM; the Lambda handler is
a source wherever it has a route to what it probes. Whether the subnet is called public or private
does not decide this. Those words say whether its route table sends `0.0.0.0/0` to an internet
gateway, and an internet gateway carries IPv4 traffic only for an instance with a public IPv4
address, which a CATS listener never has. What decides it is the path the subnet gives the listener.

**The listener reaches SSM: `ec2`.** Through NAT, interface endpoints in the VPC, or your
organization's centralized endpoints over a Transit Gateway. Nothing to configure:

```yaml
global_network_settings:
  enabled_network_handlers: [ec2]
```

**It cannot: `lambda`.** An empty route table, or DNS that resolves the service names to
centralized endpoints the subnet cannot route to. The agent never registers, so the EC2 handler
cannot be the source here; the Lambda handler can, since CATS invokes the probe from outside the VPC
over the control plane and the probe (standard-library only) makes no AWS call, so it needs no SSM
and no endpoints, not even a `lambda` endpoint, only a route to its target. The listener remains a
target:

```yaml
global_network_settings:
  enabled_network_handlers: [lambda, ec2]  # *_call probes via Lambda; the listener is the target
```

**It cannot, and the VPC has no endpoints of its own: `ec2` with endpoints CATS creates.** The
escape hatch, typically a stock or sandbox VPC. CATS creates interface endpoints in the target's
subnets (and an S3 gateway endpoint for UDP), on a group the listener can reach, since the VPC
default group does not admit it ([why](#security-groups-on-interface-endpoints)). Where the VPC
already has an endpoint for a service, or a private hosted zone for its name, CloudFormation refuses
the create and the deploy fails; where DNS already points at centralized endpoints the subnet cannot
route to, the create succeeds and the agent still never registers. One set per VPC; `cats validate`
rejects targets that disagree:

```yaml
global_network_settings:
  enabled_network_handlers: [ec2]
  interface_endpoints: [ssm, ssmmessages, ec2messages]
  interface_endpoint_security_group_ids: [sg-0abc123def456]  # admits 443 from the listener's subnets
  gateway_endpoints: [S3]                                 # only for UDP targets
```

This is about the source. Separately, whichever handler is the source, a listener that is the
**target** of a `udp_call` must reach S3 to write the receipt (NAT, or an S3 gateway endpoint in its
route table); with no path to S3 it can be a TCP target but not a UDP one.

### Endpoint policy

Every endpoint CATS creates carries an endpoint policy that allows any action by any principal
**in the account under test**, and nothing from outside it:

```yaml
Statement:
  - Effect: Allow
    Principal: "*"
    Action: "*"
    Resource: "*"
    Condition:
      StringEquals:
        aws:PrincipalAccount: !Ref AWS::AccountId
```

An endpoint with no policy takes AWS's full-access default. The condition is scoped to the account
rather than to CATS's roles because an interface endpoint with private DNS carries the whole VPC's
traffic for that service — a role-scoped policy would deny every other workload for the test window.
Two limits:

- A shared VPC consumed from other accounts (RAM) would need `aws:PrincipalOrgID`, which CATS does
  not offer; point it at endpoints the VPC already has instead.
- A service that rejects a custom policy cannot be a CATS endpoint — CloudFormation fails with
  `only supports the full-access endpoint policy`. Everything CATS needs supports one; check any
  other with `aws ec2 describe-vpc-endpoint-services --service-names com.amazonaws.<region>.<service>
  --query 'ServiceDetails[].VpcEndpointPolicySupported'`.

### Security groups on interface endpoints

An interface endpoint answers only what its security group admits. Unless you say otherwise CATS
leaves it in the VPC default group, which admits only its own members — and the CATS listener is not
one, so the agent never registers and EC2 probes fail. Point `interface_endpoint_security_group_ids`
at a group that admits TCP 443 from the listener's subnets (an `sg-` id or an SSM parameter name, at
any settings level):

```yaml
global_network_settings:
  interface_endpoints: [ssm, ssmmessages, ec2messages]
  interface_endpoint_security_group_ids:
    - sg-0abc123def456            # or /infrastructure/endpoint-sg
```

Leave it unset only if the default group already admits 443 from those subnets. Gateway endpoints
take no security group.

## Limitations and levers

| If | Lever |
|---|---|
| The listener's subnet can't reach SSM | Probe with the Lambda handler (`enabled_network_handlers: [lambda]`) — [reaching AWS](#reaching-aws-from-a-private-subnet) |
| A CATS-created interface endpoint isn't reachable on the VPC default group | `interface_endpoint_security_group_ids` — [security groups](#security-groups-on-interface-endpoints) |
| You need a specific ingress posture, or a faster stack destroy | `existing_security_group_ids` — attaches your own group to the listener and probe Lambda |
| The target is in another account's VPC | A network path between the VPCs (VPC peering or Transit Gateway); CATS does not create it |

## What the listener requires

Every target deploys an EC2 listener. For it to deploy and be usable as a target:

- **A Nitro-based instance type.** CATS gates readiness on the live serial console
  (`ec2:GetConsoleOutput` with `Latest`), which non-Nitro types do not offer — the deploy fails
  with that reason. The default `t3.micro` is Nitro, as are current-generation types; the older
  pre-Nitro families (`t2`, `m4`, `c4`, …) are not.
- **An AMI that runs the user data under cloud-init and writes to the serial console.** The
  listener signals readiness by writing a marker there; Amazon Linux 2023 (the default) does this.
  A custom `existing_ec2_ami_id` that does not will fail readiness — cloud-init finishes with no
  ready line, or the wait times out.
- **The configured listen ports.** The listener's security group admits only `tcp_listen_ports`
  and `udp_listen_ports`; a probe to any other port is dropped.
- **A path from the source, and a group that admits the port**, to be reached as a target.

Within those constraints the listener runs the Amazon Linux image CATS selects for the instance
type's architecture (logged at deploy; the distribution is not part of the compatibility contract),
has the SSM agent, and writes UDP receipts to S3.

### Instance Type

```yaml
global_network_settings:
  ec2_instance_type: t3.micro  # Default
```

Graviton types (`t4g.micro`, `m7g.large`, ...) work as-is: CATS reads the
type's architecture from EC2 at deploy time and picks the matching Amazon
Linux 2023 image. The deploy role needs `ec2:DescribeInstanceTypes` for that
lookup (it already needs `ec2:DescribeImages`).

### Custom AMI

Override the default (the latest Amazon Linux image CATS selects for the instance
type's architecture, logged at deploy time) with a literal AMI ID:

```yaml
global_network_settings:
  existing_ec2_ami_id: ami-0123456789abcdef0
```

Or with an SSM parameter path, resolved in the deploy region at deploy time:

```yaml
global_network_settings:
  existing_ec2_ami_id: /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64
```

AMI IDs are regional, so a literal ID silently couples your config to
`cats_deployment_settings.aws_region`. AWS-published parameter paths use the
same name in every region and resolve to the correct regional AMI, making them
the portable choice.

The parameter value must be a plain AMI ID (`ami-...`). JSON "recommended"
parameters (e.g. ECS/EKS optimized AMI paths) are rejected — use their
`/image_id` sub-path instead. User-created parameters must match the same
format and exist in each account under test and deploy region, since resolution
runs in the target account.

## Lambda Caller Details

One probe Lambda function per network target, named
`{cfn_stack_prefix}{account}-probe-{target}` (`CATS-test-infra-prod-probe-private` with the
default prefix):

- Runs in your VPC, attached to every subnet in the target's `subnet_ids` (the EC2 listener
  sits in the first one only)
- Has a 30-second timeout
- Makes outbound HTTP/HTTPS/TCP/UDP calls; the protocol is chosen per invocation

The EC2 handler runs exactly the same probe source through one SSM document per
account (`{cfn_stack_prefix}{account}-Probe`), so the two handlers classify identically by
construction. The function and document names are not covered by the compatibility
contract; read them from the stack rather than deriving them.

### Timeout Configuration

```yaml
global_network_settings:
  network_call_timeout_seconds: 60  # Minimum 30
```

## Accessing EC2 DNS Name

Get the private DNS of the EC2 listener:

```python
def test_get_dns(account: AccountUnderTest) -> None:
    dns = account.private.ec2_dns_name
    # e.g., "ip-10-0-1-5.ec2.internal"
```

### Reaching a listener in another account

`ec2_dns_name` is the listener's **private** DNS name, and a probe to it from another VPC needs
three things the network under test must already provide; CATS deploys nothing between accounts:

- A path between the two VPCs: a peering connection, a Transit Gateway or a Cloud WAN segment,
  with a route to the target's CIDR in the source subnet's route table and one back.
- The listener's ports admitted from the source. CATS's own listener group admits each configured
  port from anywhere; a group of your own on `existing_security_group_ids` must admit the source
  CIDR.
- Name resolution across the path. A peering connection resolves the other VPC's private names
  only with DNS resolution enabled on both sides (`ModifyVpcPeeringConnectionOptions`,
  `AllowDnsResolutionFromRemoteVpc`); a Transit Gateway needs a shared private hosted zone or
  Route 53 Resolver rules. Without it the probe returns `DNS_FAILURE`, which is the resolver, not
  the network, so do not read it as a blocked path.

`SUCCESS` across the path proves routing, security groups and DNS together. `TIMEOUT` is any
one of a missing route, a group that does not admit the port, or a firewall between them; pair it
with a positive control, as [Testing Traffic is Blocked](#testing-traffic-is-blocked) describes.

## Testing Traffic is Blocked

Prove traffic cannot flow. A denial test needs a listener that is known to be
ready at the target and an allowed-path positive control in the same run;
otherwise a broken listener passes as "blocked". `TIMEOUT` is the only outcome
consistent with a silent DROP.

```python
def test_prod_reaches_shared(prod: AccountUnderTest, shared: AccountUnderTest) -> None:
    """Positive control: the allowed path works, so the listener is ready."""
    result = prod.private.tcp_call(shared.private.ec2_dns_name, 8080)
    assert result == CatsNetworkResponse.SUCCESS


def test_cross_env_blocked(prod: AccountUnderTest, dev: AccountUnderTest) -> None:
    """Prove prod cannot reach dev. Packets must be dropped, not refused."""
    result = prod.private.tcp_call(dev.private.ec2_dns_name, 8080)
    assert result == CatsNetworkResponse.TIMEOUT


def test_blocked_domain_does_not_resolve(account: AccountUnderTest) -> None:
    """Prove DNS Firewall blocks the domain. The resolver acted, not the network."""
    result = account.private.https_call("blocked.example.com", 443)
    assert result == CatsNetworkResponse.DNS_FAILURE
```

When such a test fails, the printed result says why: `REFUSED('[Errno 111]
Connection refused')` means the path is open; `DNS_FAILURE('[Errno -2] Name or
service not known')` means the name never resolved and no packet was sent.

UDP has no `TIMEOUT` and no `REFUSED`: a datagram that leaves the socket is `SENT`, and
`receive_s3` reports whether the listener's receipt appeared. A missing receipt is the absence of
evidence, not a classification: the datagram was dropped, or the listener dropped it, or the
listener could not write to S3, and nothing in the result says which. So a negative UDP control is
evidence only beside a positive one to the same listener in the same run:

```python
def test_udp_allowed_path(shared: AccountUnderTest, dev: AccountUnderTest) -> None:
    """Positive control: an allowed source's datagram reaches dev's listener, which writes the receipt."""
    sent = shared.private.udp_call(dev.private.ec2_dns_name, 5000)
    assert dev.private.receive_s3(sent) is True


def test_udp_cross_env_blocked(prod: AccountUnderTest, dev: AccountUnderTest) -> None:
    """Prove prod's datagrams do not reach dev's listener. Meaningful only with the positive control above."""
    sent = prod.private.udp_call(dev.private.ec2_dns_name, 5000)
    assert dev.private.receive_s3(sent) is False
```
