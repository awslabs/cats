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

"""CLI commands for direct interaction with deployed CATS infrastructure."""

import json
import os
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from botocore.exceptions import BotoCoreError, ClientError

from cats.constants import CatsNetworkResult
from cats.exceptions import CatsConfigError, CatsError, CatsUsageError
from cats.resources import get_account, prepare_udp_receiver
from cats.resources.account_under_test import AccountUnderTest
from cats.resources.network_interact import NetworkHandler, _validate_host, _validate_port
from cats.utils.aws_profile import write_cats_profile

run_app = typer.Typer(
    name="run",
    help="Interact directly with deployed CATS infrastructure. Use after `cats infra deploy`.",
    no_args_is_help=True,
    # A traceback here can hold STS credentials in locals - never print them.
    pretty_exceptions_show_locals=False,
)


# Every failure is {"error": message} on stderr. Exit 2 is the caller's own input: a bad
# argument (CatsUsageError, including an unknown --target, which `_network_target` turns into
# one, or an unknown --role, which write_cats_profile refuses before any AWS call), or their
# configuration (CatsConfigError, including a --config path that is not a file). AttributeError
# is deliberately NOT here: one raised by a bug inside a call must surface as a traceback, not
# be reported as the caller's mistake.
# Exit 1 is an operation that failed: CATS infrastructure (CatsError), any AWS API failure
# (ClientError: expired creds, throttling), or the environment's own botocore failures
# (BotoCoreError: no profile, no identity, no endpoint).
_USAGE_ERRORS = (CatsUsageError, CatsConfigError)
_CONTRACT_ERRORS = (CatsError, ClientError, BotoCoreError)

ConfigRunOption = Annotated[
    Path | None,
    typer.Option("--config", "-c", help="Path to cats.yaml. Defaults to the CATS_CONFIG environment variable, then ./cats.yaml."),
]
AccountOption = Annotated[str, typer.Option("--account", help="Account keyname under accounts_under_test in cats.yaml.")]
TargetOption = Annotated[str, typer.Option("--target", help="Network target name under the account's account_network_under_test.")]
HostOption = Annotated[str, typer.Option("--host", help="Hostname or IP literal to probe; not a URL, the command chooses the protocol.")]
PortOption = Annotated[int, typer.Option("--port", help="TCP or UDP port, 1-65535.")]


def _error_exit(message: str, code: int = 1) -> None:
    print(json.dumps({"error": message}), file=sys.stderr)
    raise typer.Exit(code=code)


def _usage_exit(message: str) -> None:
    _error_exit(message, code=2)


def _emit(result: Any) -> None:  # noqa: ANN401 - transport passthrough
    print(json.dumps(result, default=str))


def _validate_host_and_port(host: str, port: int) -> None:
    """The same checks the probe would make, before any AssumeRole: a bad argument is exit 2 without an AWS call."""
    _validate_host(host)
    _validate_port(port)


def _network_target(acc: AccountUnderTest, target: str) -> NetworkHandler:
    """The handler for --target, or a CatsUsageError naming the targets that exist.

    An explicit membership check rather than getattr: the account's ``__getattr__`` raises
    AttributeError for an unknown name, but so does any bug inside the call, and only the
    former is the caller's own input.
    """
    available = acc.available_network_targets
    if target not in available:
        raise CatsUsageError(f"Network target '{target}' not found for account '{acc.name}'. Available targets: {available}")
    handler: NetworkHandler = getattr(acc, target)
    return handler


def _network_result_json(result: CatsNetworkResult) -> dict[str, str | int | None]:
    """The stable output shape for http-call and tcp-call: every key always present."""
    return {"result": result.value, "reason": result.reason, "http_status": result.http_status}


_TAXONOMY = (
    "result is one of SUCCESS (connection made; for HTTP any response, http_status carries the code), "
    + "REFUSED (path open, nothing accepted: RST on connect, or for HTTP a listener that accepted then closed with no response), "
    + "TIMEOUT (TCP connect got no answer; consistent with silent drop), "
    + "DNS_FAILURE (name did not resolve), TLS_FAILURE (TCP connected, TLS handshake failed; https only), "
    + "FAILURE (path not provably open or blocked - includes connect-then-silence, reason starts 'connected; no HTTP reply'; never expected). "
    + "reason is the probe's error text or null."
)

_TCP_TAXONOMY = (
    "result is one of SUCCESS (connection made), REFUSED (path open, nothing accepted: RST on connect), "
    + "TIMEOUT (connect got no answer; consistent with silent drop), DNS_FAILURE (name did not resolve), "
    + "FAILURE (path not provably open or blocked; errno in reason; never expected). "
    + "reason is the probe's error text or null."
)


@run_app.command("account-info")
def account_info(
    account: AccountOption,
    config: ConfigRunOption = None,
) -> None:
    """Runtime metadata from CFN outputs for ACCOUNT: targets, handlers, EC2 DNS, tags, deployed CATS version."""
    try:
        acc = get_account(config, account)
        _emit(
            {
                "name": acc.name,
                "account_id": acc.account_id,
                "tags": acc.tags,
                "deployed_version": acc.deployed_version,
                "targets": {name: h.target_info() for name, h in acc._deployed_network_handlers.items()},
            }
        )
    except _USAGE_ERRORS as e:
        _usage_exit(str(e))
    except _CONTRACT_ERRORS as e:
        _error_exit(str(e))


@run_app.command(
    "http-call",
    help="HTTP call (GET /; any status in reply is SUCCESS) from ACCOUNT's TARGET network segment to HOST:PORT. "
    + f"Emits {{result, reason, http_status}}. {_TAXONOMY}",
)
def http_call(
    account: AccountOption,
    target: TargetOption,
    host: HostOption,
    port: PortOption,
    config: ConfigRunOption = None,
) -> None:
    try:
        _validate_host_and_port(host, port)
        result = _network_target(get_account(config, account), target).http_call(host, port)
        _emit(_network_result_json(result))
    except _USAGE_ERRORS as e:
        _usage_exit(str(e))
    except _CONTRACT_ERRORS as e:
        _error_exit(str(e))


@run_app.command(
    "tcp-call",
    help=f"TCP call from ACCOUNT's TARGET network segment to HOST:PORT. Emits {{result, reason, http_status}} (http_status is null). {_TCP_TAXONOMY}",
)
def tcp_call(
    account: AccountOption,
    target: TargetOption,
    host: HostOption,
    port: PortOption,
    config: ConfigRunOption = None,
) -> None:
    try:
        _validate_host_and_port(host, port)
        result = _network_target(get_account(config, account), target).tcp_call(host, port)
        _emit(_network_result_json(result))
    except _USAGE_ERRORS as e:
        _usage_exit(str(e))
    except _CONTRACT_ERRORS as e:
        _error_exit(str(e))


@run_app.command("udp-call")
def udp_call(
    account: AccountOption,
    target: TargetOption,
    host: HostOption,
    port: PortOption,
    receiving_account: Annotated[
        str | None,
        typer.Option(
            "--receiving-account",
            help="The account whose CATS listener HOST is (its ec2_dns_name). Required for a CATS listener: the datagram then carries that "
            + "account's receipt token, without which the listener drops it and receive-s3 reports false. Omit for a non-CATS target.",
        ),
    ] = None,
    config: ConfigRunOption = None,
) -> None:
    """UDP send from ACCOUNT's TARGET to HOST:PORT. Emits {result, reason, correlation_id}.

    result is SENT (datagram left the host; pass correlation_id to receive-s3 on the receiving
    account), DNS_FAILURE (name did not resolve), or FAILURE (unclassified; errno in reason).
    REFUSED and TIMEOUT cannot occur for UDP.
    """
    try:
        _validate_host_and_port(host, port)
        if receiving_account is not None:
            prepare_udp_receiver(config, receiving_account, host)
        result = _network_target(get_account(config, account), target).udp_call(host, port)
        _emit({"result": result.value, "reason": result.reason, "correlation_id": result.correlation_id})
    except _USAGE_ERRORS as e:
        _usage_exit(str(e))
    except _CONTRACT_ERRORS as e:
        _error_exit(str(e))


@run_app.command("shell")
def shell_cmd(
    account: AccountOption,
    target: TargetOption,
    command: Annotated[list[str], typer.Option("--command", help="Shell command line. Repeat for multi-line scripts.")],
    config: ConfigRunOption = None,
) -> None:
    """Run shell command(s) on ACCOUNT's TARGET EC2 instance via SSM. Requires account_settings.allow_shell: true at deploy time."""
    if any(c == "" for c in command):
        _usage_exit("--command must be non-empty. Repeat for multi-line scripts.")
        return
    try:
        result = _network_target(get_account(config, account), target).shell(command)
        _emit(result.model_dump())
    except _USAGE_ERRORS as e:
        _usage_exit(str(e))
    except _CONTRACT_ERRORS as e:
        _error_exit(str(e))


@run_app.command("receive-s3")
def receive_s3(
    account: AccountOption,
    target: TargetOption,
    correlation_id: Annotated[str, typer.Option("--correlation-id", help="The correlation_id a prior udp-call emitted.")],
    config: ConfigRunOption = None,
) -> None:
    """Poll the receiving ACCOUNT's response bucket for a UDP receipt with CORRELATION_ID (from a prior udp-call)."""
    try:
        # A different process ran udp-call, so only the raw ID exists here; receive_s3(sent) needs the result.
        received = _network_target(get_account(config, account), target)._receive_s3_by_id(correlation_id)
        _emit({"received": received})
    except _USAGE_ERRORS as e:
        _usage_exit(str(e))
    except _CONTRACT_ERRORS as e:
        _error_exit(str(e))


@run_app.command("write-profile")
def write_profile(
    account: AccountOption,
    role: Annotated[str, typer.Option("--role", help="Role attribute name. `default` = CATS-managed test role.")] = "default",
    profile_name: Annotated[
        str | None,
        typer.Option("--profile-name", help="Profile name. Must start with 'cats-'. Default: cats-{account}-{role}."),
    ] = None,
    profile_file: Annotated[
        Path | None,
        typer.Option(
            "--profile-file",
            help="AWS config file to write (must be an AWS config file, or not exist yet; a file with other section headers is refused). "
            + "Default: AWS_CONFIG_FILE or ~/.aws/config.",
        ),
    ] = None,
    config: ConfigRunOption = None,
) -> None:
    """Write an AWS profile that assumes ACCOUNT's ROLE from your current identity. Prints where it was written, never a credential."""
    try:
        resolved_file = (
            profile_file if profile_file is not None else (Path(os.environ["AWS_CONFIG_FILE"]) if "AWS_CONFIG_FILE" in os.environ else None)
        )
        _emit(write_cats_profile(account, role, profile_name, resolved_file, config))
    except _USAGE_ERRORS as e:
        _usage_exit(str(e))
    except _CONTRACT_ERRORS as e:
        _error_exit(str(e))
