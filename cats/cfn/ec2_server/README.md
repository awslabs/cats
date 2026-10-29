# The probe: one source, two transports

`probe.py` is the network probe behind `http_call()`, `tcp_call()`, and `udp_call()` in
`NetworkHandler` (`cats/resources/network_interact.py`). It is embedded once in the
rendered CloudFormation template, as `Mappings.CatsEmbeddedCode.code.probe`, and
referenced from two places: the per-target `ProbeLambda{target}` function's `ZipFile`,
and the per-account `CatsProbeDocument` SSM document, which feeds it to `python3 -`
through a quoted heredoc. Parity between the EC2 and Lambda handlers is therefore
structural: both run exactly this file.

The file carries no comments beyond its first line. It is embedded verbatim, so every
character in it counts against the Lambda `ZipFile` budget below, and everything the
code cannot say lives here instead. The budget is close to exhausted (about 125
characters of headroom): measure before adding anything, and buy space by shortening
something else. Read this before editing `probe.py`.

## Tenets

Every change to the probe or its parser is judged against these two:

1. **Transport parity.** EC2 and Lambda must report identically: the same network
   condition yields the same `CatsNetworkResponse` on either handler. Which transport a
   call uses is a deployment detail; it must never change what a test proves. Parity is
   structural (one source runs on both) and exercised behaviorally by the integration
   taxonomy tests, which arrange every category on both handlers.
2. **Classification is indisputable or it is not made.** Every category is a claim about
   what a security control did, and each claim must survive "how do you know?". A result
   that cannot be proven classifies as the less concrete `FAILURE`, with the raw evidence
   in `reason`, rather than being guessed into a concrete category.

## Constraints on `probe.py`

Each is pinned by a unit test; the test fails before the deploy does.

| Constraint | Why | Guarded by |
|---|---|---|
| Python 3.9 syntax | The EC2 listener is AL2023; its system `python3` is 3.9. `from __future__ import annotations` keeps `X \| Y` legal in annotations. | `test_embedded_scripts_python39.py` |
| Standard library only | Neither transport installs anything. | No dedicated test. `test_probe_main.py` runs the real file as a `python -` subprocess, so a third-party import fails there first. |
| No literal `${` | The source is injected through `Fn::Sub`, which would try to resolve it. The same rule binds every file in this directory and `cats/cfn/templates/userdata.sh.j2`: all of them reach the instance or the Lambda through `Fn::Sub`. | `test_embedded_file_contains_no_fn_sub_variable_syntax` |
| Source minus license header ≤ 4,096 characters | The Lambda `ZipFile` inline-code limit (`LAMBDA_ZIPFILE_LIMIT` in `cats/cfn/render.py`). | `test_embedded_probe_is_under_the_lambda_zipfile_limit` |
| Embedded verbatim | The code in the Lambda console and the SSM document is the code in the repo. | `test_mapping_holds_the_probe_source_verbatim_minus_license_header` |

## The listener's user data: one script, a prefix per listener

The other files here build the EC2 listener. Every listener runs the same script at first
boot: install `tcpserver.py` and `udpserver.py`, install their systemd units, write the env
files the units read, enable each unit whose port list is non-empty (a unit with no port
would exit at once and `Restart=always` would relaunch it every second), disable sshd, then
wait for every configured port to have a listening socket and write the verdict to the serial
console: `cats listeners ready`, or `cats listeners not bound:` with the ports still missing
(`LISTENER_READY_MARKER` and `LISTENER_FAILED_MARKER` in `cats/constants.py`). `cats infra
deploy` reads that verdict with `ec2:GetConsoleOutput` before it reports the stack deployed
(`cats/cfn/readiness.py`), which is why the script's explanations are Jinja comments: every
rendered byte is CloudFormation template budget.
That script is `cats/cfn/templates/userdata.sh.j2`: the desired output as bash, with the four
files pulled in as `{{ code.udpserver_py }}`, `{{ code.tcpserver_py }}`,
`{{ code.tcpserver_service }}` and `{{ code.udpserver_service }}`. `render_userdata(code)` in
`cats/cfn/render.py` renders it from a `ListenerCode` (the four files, license headers
stripped; `ListenerCode.load()` reads them), and the stack template embeds the result once,
as `Mappings.CatsEmbeddedCode.code.userdata`.

Only the port lists differ per listener, so each listener's `UserData` in `stack.yaml.j2` is
a prefix of shell assignments followed by `${Userdata}`, which `Fn::Sub` replaces with the
script (its string argument must be a literal, so the shared part can only arrive as a
variable; that is the one constraint that makes it two pieces):

```yaml
UserData:
  Fn::Base64:
    Fn::Sub:
      - |
        #!/bin/bash
        CATS_TCP_PORTS=8080,443
        CATS_UDP_PORTS=5000
        CATS_BUCKET=${CatsResponseBucket}
        CATS_UDP_RECEIPT_TOKEN=${CatsUdpReceiptToken}
        ${Userdata}
      - Userdata: !FindInMap [CatsEmbeddedCode, code, userdata]
```

The bucket and token lines render only for a target with UDP ports, so a TCP-only listener
never holds the receipt token. The units are static: `tcpserver.service` reads
`/etc/cats/tcpserver.env` and `udpserver.service` reads `/etc/cats/udpserver.env`
(`EnvironmentFile=`, root-only, written by the script from the prefix's variables), and
their `ExecStart` lines name the variables unbraced (`$CATS_TCP_PORTS`), which is what keeps
them free of `${`. systemd reads the env files as PID 1 before `DynamicUser` drops
privileges, so the token never sits in a world-readable unit file. The ports and the bucket
travel as `ExecStart` arguments; the receipt token does not: `udpserver`'s `main()` reads
`CATS_UDP_RECEIPT_TOKEN` from the environment systemd exported and refuses to start without
it, so the token is not in `ps` output. That is tidiness, not secrecy: the token is an
anti-stray gate, not a credential (it is a stack output, and it is in the instance's user
data), and nothing on the box should treat it as a secret.

A port list makes a three-hop journey on which every hop must agree: the prefix joins it
with commas (`CATS_TCP_PORTS=8080,443`), systemd splits an unbraced `$VAR` on whitespace
(so the value has to be one word to arrive as one argument), and each server's
`parse_ports` splits that argument on the comma. A space-joined list would pass `bash -n`
and shellcheck and then hand argparse two arguments on the box.
`test_port_lists_survive_the_journey_from_prefix_to_server` walks all three hops from the
rendered prefix, and the integration deployment in `test_network.py` listens on two ports
per protocol so the path with a real separator in it runs on a real instance.

This shape exists for the template's size: the script costs its bytes once, and a listener
costs its prefix, whatever the rest of the stack looks like. `tests/unit_tests/test_cfn_render_userdata.py`
pins the script and the prefix, assembles the two as CloudFormation does, and runs the
result through `bash -n` and shellcheck; `test_resolved_ec2_userdata_is_under_the_limit`
measures it against EC2's 16 KB user-data limit.

## Wire contract

The probe emits one JSON document on both transports (SSM stdout, Lambda payload, no
envelope):

```json
{"result": "SUCCESS|REFUSED|TIMEOUT|DNS_FAILURE|TLS_FAILURE|FAILURE|SENT|ERROR",
 "reason": null | "<str(exc)>",
 "http_status": null | 200}
```

`_parse_probe_output` in `network_interact.py` is the single parser. `ERROR` is never
a network outcome: it means the probe itself broke (caller or infrastructure bug), the
traceback is printed to stderr for the SSM log or CloudWatch, and the client raises
`CatsSSMError` or `CatsLambdaInvocationError`. Everything else becomes a
`CatsNetworkResult`, whose equality is category-only so diagnostics never become
load-bearing in assertions.

`SENT` is the UDP success: a datagram left the socket. Whether it arrived is proven
separately by `receive_s3()`.

Arguments arriving at `probe()` are trusted. The port is validated in the CATS library
(`_validate_port`) before any transport is invoked; the protocol is fixed by the calling
method rather than validated, and the SSM document's `allowedValues` rejects a bad
protocol at `send_command`. `probe()` itself still rejects an unknown protocol, or a UDP
call without a message, as `ERROR`: a malformed direct invocation must never silently
become a probe and report a network category. The URL is passed through as given: a
nonsense URL is itself a legitimate probe (that is what the nonexistent-name tests send).
`main()` still wraps its environment reads so a missing or non-integer variable produces
an `ERROR` line on stdout rather than a traceback and a non-zero exit.

## How `classify()` works

`RESULT_BY_EXCEPTION` maps exception classes to result strings and `classify()` walks the
exception's MRO looking for the first class in the table. Adding a category is one table
row; there is no ordering to reason about because the MRO supplies it. Notes on
individual rows:

- `socket.timeout` and `TimeoutError` are both listed. On 3.9 `socket.timeout` is a
  distinct `OSError` subclass; on 3.10+ it is an alias of `TimeoutError`. ruff's UP041
  would fold the two; `pyproject.toml` disables it for this directory, and
  `test_embedded_scripts_python39.py` pins the except clause in `_attempt_http`.
- `ConnectionRefusedError` and `ConnectionResetError` are listed individually rather than
  as `ConnectionError`. `ConnectionError` would also pull in `BrokenPipeError` and
  `ConnectionAbortedError`, and a broken pipe as "the control refused" is not an
  indisputable claim (tenet 2). `http.client.RemoteDisconnected` subclasses
  `ConnectionResetError`, so a server that accepts and immediately closes is `REFUSED`
  on HTTP as on TCP, even though the preflight (below) has by then proven that something
  accepted the connection. Both assertion directions stay safe: neither a denial test nor
  a positive control passes on `REFUSED`. The taxonomy text in `cats run` and the MCP
  server states the accept-then-close case explicitly.
- `http.client.HTTPException` is `FAILURE`: the peer accepted and answered, but not with
  HTTP (`BadStatusLine` carries the offending line as `reason`; `IncompleteRead` and
  `LineTooLong` are the other shapes). The path is provably open and something is
  listening, which is not `REFUSED`, and it is not silence, which is not `TIMEOUT`;
  nothing more concrete than `FAILURE` can be claimed. Without the row these would be
  `ERROR`, and a port serving SSH would raise in the test instead of reporting.
  `RemoteDisconnected` also inherits from `BadStatusLine`, but `ConnectionResetError`
  comes first in its MRO, so the row above still decides it.
- `ssl.SSLError` is `TLS_FAILURE` whatever errno it carries; `SSLCertVerificationError`
  also subclasses `ValueError`, but the MRO reaches `SSLError` first.
- Bare `OSError` is `FAILURE`: the errno stays visible in `reason`, and nothing more
  concrete is claimed.
- `NoHttpReply` subclasses `OSError` deliberately: it needs no table row (the MRO
  reaches `OSError` → `FAILURE`), which matters against the character budget.

## `DNS_FAILURE` is claimed two ways

Both are indisputable; they differ in what the evidence is.

**By type.** A `socket.gaierror` whose errno is in `DNS_ERRNOS` (`EAI_NONAME`,
`EAI_AGAIN`, `EAI_FAIL`, `EAI_NODATA` where the platform defines it) is a resolver
saying no, or being unreachable. `classify()` recognizes this wherever it arises, which
matters because `http.client` re-resolves internally (see below). Any other `gaierror` code,
such as `EAI_ADDRFAMILY`, means the resolver answered and the local stack could not use
the address family: that is `FAILURE`, not a DNS claim.

**By position.** `_resolve()` is the one place `ResolutionFailure` can be raised, and
`ResolutionFailure` is `DNS_FAILURE`. This exists because environments lie about
shape: Lambda's resolver reports NXDOMAIN and throttling as `EAI_SYSTEM`, which CPython
re-raises as a bare `OSError` carrying a system errno (`EBUSY` in practice). That
exception proves nothing on its own, so `classify()` treats a bare `OSError(EBUSY)` as
`FAILURE`. Inside the resolve step, though, the only thing that can have failed is name
resolution, so `_resolve()` converts any non-`gaierror` `OSError` into
`ResolutionFailure`. Position, not exception shape, is what makes it a DNS claim.

`_resolve()` lets `gaierror` through untouched: `classify()` already decides those by
errno, so wrapping them would duplicate the `DNS_ERRNOS` rule in two places.

## Why `_attempt_http` connects with TCP first

`_attempt_http` starts with a real `_attempt_tcp` connect (which itself starts with
`_resolve()`, so a resolver failure still classifies by position). This is what makes the
HTTP categories indisputable (tenet 2):

- If the SYN goes unanswered, the preflight raises `socket.timeout` → `TIMEOUT`, and it
  provably means the network path never answered — not "either the path is closed or the
  backend is slow", which is what a single connect+request budget conflates.
- If the connect is refused, `ConnectionRefusedError` → `REFUSED`, same as `tcp`.
- Only after the connect succeeds does the HTTP request run, with its own longer budget
  (`HTTP_TIMEOUT`, 10s). Silence *after* a successful connect is provably not a silent
  drop, so it must not classify `TIMEOUT`: it raises `NoHttpReply` (an `OSError`
  subclass, so the MRO table reports it as `FAILURE`) with the evidence in `reason`
  (`connected; no HTTP reply in 10s: ...`). This closes the denial-test false-pass where
  a hung-but-open backend satisfied `assert result == TIMEOUT`. The TLS
  handshake-timeout variant takes the same path.

Costs, accepted deliberately: every HTTP probe makes two connections (stateful
firewalls and rate limiters see doubled connects, and a single-accept listener reports
`REFUSED` on the second); and a slow-but-open positive control becomes an honest
`FAILURE` instead of a wrong `TIMEOUT`. For `http` the second connect is made explicitly
(`conn.connect()`) before the request, so a timeout there propagates as the connect
timeout it is and classifies `TIMEOUT`; `NoHttpReply` wraps only the request and the wait
for the response, and never says "connected" about a connection that did not happen. For
`https` the connect includes the TLS handshake, whose silence is the hung-backend case
above, so it stays inside the wrap and classifies `FAILURE`.

The request is `GET /` over `http.client` (`HTTPConnection` or `HTTPSConnection`,
`timeout=HTTP_TIMEOUT`); any status in reply, including 3xx, 4xx and 5xx, is `SUCCESS`
with `http_status` set. The probe asks whether the path is open and something answers
HTTP, not what the application does with a particular request. `http.client` rather than
`urllib` because the probe needs its behavior as it is:

- No proxy support, so `http_proxy`/`https_proxy` in the handler's environment is never
  honored and the request takes the same direct path as the preflight.
- No redirect following. `getresponse().status` is the first hop's status; following a
  redirect would probe some other host.
- Exceptions arrive raw: a `ConnectionResetError` or `ssl.SSLError` from the request is
  the socket's own exception, so `classify()` sees it without unwrapping. A reply that is
  not HTTP raises `http.client.BadStatusLine` with the offending line, and classifies
  `FAILURE` with that line as `reason`.
- `HTTPConnection.connect` resolves the name again and connects through
  `socket.create_connection`, which tries every resolved address and reports the last
  error: the same rule `_attempt_tcp` follows (below), so the preflight and the request
  agree on what "reachable" means. An IPv6 literal is passed unbracketed; `http.client`
  brackets it in the `Host` header itself, and the CATS library strips the brackets a
  caller may have copied from a URL before any transport is invoked.

`HTTPSConnection` with no explicit context uses `ssl.create_default_context()`, so the
certificate is verified against the system trust store and the hostname is checked; an
untrusted CA or a plaintext listener is `TLS_FAILURE`.

A read timeout escapes `getresponse()` as `socket.timeout` on 3.9 and `TimeoutError` on
3.10+, so `_attempt_http` catches both and converts them to `NoHttpReply`. The
connection is closed in a `finally` so a failed request never leaks a socket into the
next probe on the same instance.

## Every address is tried

`_resolve()` returns every `getaddrinfo` result, in resolver order. `_attempt_tcp`
connects to each in turn with its own `TIMEOUT_SECONDS` budget, stops at the first that
connects, and if none does, raises the last error, which `classify()` then decides.
That is `socket.create_connection`'s rule, so TCP and HTTP agree: a dual-stack name
whose IPv6 address is unreachable but whose IPv4 address accepts is `SUCCESS` on both.

Because the last error wins, a multi-address name whose addresses fail differently
(IPv6 dropped, IPv4 refused) classifies by the resolver's ordering. Addresses that all
fail the same way, which is what a control that drops or refuses by port produces,
classify the same whatever the order.

UDP sends to the first address only: a datagram has one destination, and sending the
same correlation id twice would make `receive_s3` prove nothing about which address
was open.

## Timeouts

`TIMEOUT_SECONDS` is 2: the budget for each TCP connect (including the HTTP preflight),
per resolved address. `HTTP_TIMEOUT` is 10: the budget for each socket operation of the
HTTP request after the connect succeeded. UDP has nothing to wait for. A `TIMEOUT` result
means every address's SYN went unanswered for two seconds, the signature of a dropped
packet. Post-connect HTTP silence is `FAILURE` with `reason: "connected; no HTTP reply
in 10s: ..."` (see above). The read timeout is per socket operation, so a server that
drips one byte inside every budget window never times out.

The transports must outlive the probe's worst case, which for a name resolving to N
addresses is roughly `N × TIMEOUT_SECONDS + N × HTTP_TIMEOUT` plus two name
resolutions: ~12 s for one address and ~24 s for a dual-stack pair, before DNS, and a
Lambda resolver can spend ~5 s per `EAI_AGAIN` retry. The `ProbeLambda` `Timeout` in
`stack.yaml.j2` is 30 s for that reason; shorter than the probe, a hung backend would
surface as a Lambda timeout (`CatsLambdaInvocationError`) on one transport and as
`FAILURE` on the other. The SSM side is bounded by `network_call_timeout_seconds`
(minimum 30). Change either probe constant and re-check both bounds.

## Changing the probe

Both transports must be redeployed to pick up a change: EC2 user data runs on first boot
only, and a named SSM document cannot change content in place. In development that is
`cats infra destroy <account> && cats infra deploy <account>`; across releases the
`cats:version` stack tag enforces it. Update the unit tests (`tests/unit_tests/test_probe*.py`)
and this file in the same change.
