# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Helper functions and result types for use in tests, alongside the fixtures.

`get_account_fixture_names` and `get_accounts_by_tag` drive test
parameterization across accounts.
"""

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel

from cats.config import Settings


class CatsNetworkResponse(StrEnum):
    """Outcome of a network call, classified by what happened on the wire.

    Each value is a precise claim about what the probing endpoint observed. Which
    control produced it is interpretation, and belongs in the test's docstring.

    - SUCCESS: Connection established. For HTTP/HTTPS, any HTTP response (including
      4xx/5xx and 3xx, which are not followed) is SUCCESS - the call arrived. The
      status code is on CatsNetworkResult.http_status for tests that care.
    - REFUSED: RST received (ECONNREFUSED or ECONNRESET) - the network path is OPEN
      but nothing accepted the connection. For HTTP, a listener that accepted the
      connection and closed it without any response is also REFUSED. On a
      blocked-traffic test this means the control is NOT working. Indistinguishable
      from a Network Firewall REJECT.
    - TIMEOUT: The TCP connect got no answer within the budget - consistent with a
      silent drop. This is the expected outcome for SG/NACL/Network Firewall DROP/
      route-table isolation tests, but the endpoint cannot distinguish policy
      enforcement from transient loss: all silent drops look the same.
    - DNS_FAILURE: Name resolution failed - the resolver answered NXDOMAIN/NODATA or
      was unreachable. What Route 53 Resolver DNS Firewall blocks produce, and also
      what a typo produces. No packet was ever sent to the target.
    - TLS_FAILURE: TCP connected, the TLS handshake failed - untrusted or mismatched
      certificate, a plaintext listener on the port, or a protocol mismatch. What
      TLS inspection with an untrusted CA produces. The OpenSSL text is in reason.
    - FAILURE: The path could not be proven open or blocked. Either a connect that
      succeeded with no HTTP reply inside the budget (a hung backend, NOT a silent
      drop; reason starts "connected; no HTTP reply"), or an unclassified network
      error (ENETUNREACH, EHOSTUNREACH, EADDRNOTAVAIL, ...; the errno is in reason).
      Never an expected value.
    - SENT: UDP datagram left the host (connectionless - use receive_s3 to confirm
      arrival). REFUSED and TIMEOUT are unreachable for UDP.
    """

    SUCCESS = "SUCCESS"
    REFUSED = "REFUSED"
    TIMEOUT = "TIMEOUT"
    DNS_FAILURE = "DNS_FAILURE"
    TLS_FAILURE = "TLS_FAILURE"
    FAILURE = "FAILURE"
    SENT = "SENT"


@dataclass(frozen=True, eq=False)
class CatsNetworkResult:
    """Outcome of a network call: a CatsNetworkResponse plus the evidence behind it.

    Equality and hashing are category-only, against another result or a bare
    CatsNetworkResponse: `result == CatsNetworkResponse.TIMEOUT` is the assertion
    to write. `reason`, `http_status`, and `correlation_id` are diagnostics: they
    are excluded from equality so error strings never become load-bearing, and
    they are printed by the repr so a failing assertion is self-diagnosing.

    Comparing to a plain string is refused (`result == "TIMEOUT"` is False): the
    enum is the contract.

    Attributes:
        response: The classified outcome.
        reason: str(exception) from the probe for non-SUCCESS/SENT outcomes.
        http_status: The HTTP status code, only for HTTP/HTTPS SUCCESS.
        correlation_id: The UDP receipt ID that was sent or attempted, only for UDP.
    """

    response: CatsNetworkResponse
    reason: str | None = None
    http_status: int | None = None
    correlation_id: str | None = None

    def __eq__(self, other: object) -> bool:
        if isinstance(other, CatsNetworkResult):
            return self.response is other.response
        if isinstance(other, CatsNetworkResponse):
            return self.response is other
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.response)

    def __repr__(self) -> str:
        fields: list[str] = []
        if self.reason is not None:
            fields.append(repr(self.reason))
        if self.http_status is not None:
            fields.append(f"http_status={self.http_status}")
        if self.correlation_id is not None:
            fields.append(f"correlation_id={self.correlation_id!r}")
        return f"{self.response.name}({', '.join(fields)})"

    @property
    def value(self) -> str:
        """The response category as a string, for CLI/JSON consumers."""
        return self.response.value


class ShellResult(BaseModel):
    """Result of NetworkHandler.shell().

    ``stdout`` and ``stderr`` are stripped of trailing whitespace. ``stdout_truncated`` and
    ``stderr_truncated`` are set when SSM returned exactly its maximum for that stream (24,000
    characters of stdout, 8,000 of stderr, before stripping): SSM keeps the first N characters
    and gives no other signal, so an output of exactly N characters reads as truncated and the
    remainder is not recoverable through CATS. Write large output to S3 from the command instead.
    """

    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    stdout_truncated: bool
    stderr_truncated: bool


# The CloudFormation output-key scheme (a covered surface, docs/concepts/versioning.md). Three fixed
# keys per stack, six per network target written as f"{key}{target}" with no separator, since
# logical ids are alphanumeric. The rule a future key must keep, pinned by
# tests/unit_tests/test_output_key_scheme.py: no per-target key may extend another (Ec2DnsNameV6
# would collide with target V6...), and no base key may start with a per-target key.
BASE_OUTPUT_KEYS: tuple[str, ...] = ("TestRoleArn", "ResponseBucketName", "UdpReceiptToken")
PER_TARGET_OUTPUT_KEYS: tuple[str, ...] = ("Ec2DnsName", "Ec2InstanceId", "VpcId", "SubnetIds", "EnabledHandlers", "ProbeLambdaName")

# What an EC2 listener's user data writes to its serial console as its verdict, and what
# `cats infra deploy` reads back with ec2:GetConsoleOutput before it calls the stack deployed
# (cats/cfn/templates/userdata.sh.j2 writes them; cats/cfn/readiness.py reads them). The ready
# line is the script's last line; the failed line names the ports still unbound. Neither string
# may occur elsewhere in the console output of a boot, so they are wordy on purpose.
LISTENER_READY_MARKER = "cats listeners ready"
LISTENER_FAILED_MARKER = "cats listeners not bound:"


class DeployedEC2Resources(BaseModel):
    """EC2 resources deployed by CATS CloudFormation stack."""

    dns_name: str
    instance_id: str


class DeployedLambdaResources(BaseModel):
    """Lambda resources deployed by CATS CloudFormation stack: one probe function per target."""

    probe_lambda_name: str


class DeployedTargetResources(BaseModel):
    """Resources deployed by CATS CloudFormation stack for a network target."""

    response_bucket_name: str
    vpc_id: str
    subnet_id_list: list[str]
    account_name: str
    target_name: str
    enabled_handlers: list[str]

    ec2: DeployedEC2Resources
    lambda_: DeployedLambdaResources | None = None

    def target_info(self) -> dict[str, object]:
        """The agent-facing view of this target (`cats run account-info`, `cats_account_info`): stable names, no model plumbing.

        Keys: ``vpc_id``, ``subnet_ids``, ``enabled_handlers``, ``ec2`` (``dns_name``, ``instance_id``) and
        ``lambda`` (``probe_lambda_name``, or ``None`` when the handler is not deployed).
        """
        return {
            "vpc_id": self.vpc_id,
            "subnet_ids": list(self.subnet_id_list),
            "enabled_handlers": list(self.enabled_handlers),
            "ec2": {"dns_name": self.ec2.dns_name, "instance_id": self.ec2.instance_id},
            "lambda": None if self.lambda_ is None else {"probe_lambda_name": self.lambda_.probe_lambda_name},
        }


def get_account_fixture_names() -> list[str]:
    """Get fixture names for all configured accounts."""
    return [f"{acc}_account" for acc in Settings().accounts_under_test]


def get_accounts_by_tag(key: str, value: str | None = None) -> list[str]:
    """Return fixture names for accounts with this tag.

    Args:
        key: Tag key to match
        value: If provided, tag must have this value. If None, tag just needs to exist.

    Returns:
        List of fixture names (e.g., ["prod_account", "dev_account"])
    """
    results: list[str] = []
    for name, config in Settings().accounts_under_test.items():
        if key in config.tags:
            if value is None or config.tags[key] == value:
                results.append(f"{name}_account")
    return results


def get_target_tcp_ports(account_name: str, target_name: str) -> list[int]:
    """Get TCP listen ports for a specific account/target.

    Reads cats.yaml, so it works at collection time (pytest.mark.parametrize) before any account is constructed.
    """
    account = Settings().accounts_under_test[account_name]
    return list(account.account_network_under_test[target_name].network_settings.tcp_listen_ports)


def get_target_udp_ports(account_name: str, target_name: str) -> list[int]:
    """Get UDP listen ports for a specific account/target.

    Reads cats.yaml, so it works at collection time (pytest.mark.parametrize) before any account is constructed.
    """
    account = Settings().accounts_under_test[account_name]
    return list(account.account_network_under_test[target_name].network_settings.udp_listen_ports)
