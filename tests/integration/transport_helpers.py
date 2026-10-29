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

"""Shared assertions that drive the CLI and MCP transports over a deployed account.

Each integration test file that already deploys a stack can call these to prove the
same network handlers are reachable through CLI and MCP, without deploying more stacks.
"""

import json

from typer.testing import CliRunner

from cats.cli import cats_cli
from cats.cli.mcp import account_info, http_call, list_accounts, receive_s3, udp_call
from cats.resources.account_under_test import AccountUnderTest


def _host_for(account: AccountUnderTest, target: str, host: str | None) -> str:
    """Default URL to the account's own EC2 DNS; override for cross-account calls."""
    return host if host is not None else getattr(account, target).ec2_dns_name


def assert_cli_transport(account: AccountUnderTest, target: str, open_port: int, host: str | None = None) -> None:
    """Exercise `cats run` subcommands against a deployed stack.

    Asserts: account-info reflects deployed DNS, http-call SUCCESS to host:open_port with the
    listener's 200 and a null reason, udp-call + receive-s3 roundtrip (roundtrip only against
    the account's own EC2).
    """
    runner = CliRunner(env={"COLUMNS": "200", "NO_COLOR": "1"})
    call_host = _host_for(account, target, host)

    info = runner.invoke(cats_cli, ["run", "account-info", "--account", account.name])
    assert info.exit_code == 0
    assert json.loads(info.stdout)["targets"][target]["ec2"]["dns_name"] == getattr(account, target).ec2_dns_name

    http = runner.invoke(cats_cli, ["run", "http-call", "--account", account.name, "--target", target, "--host", call_host, "--port", str(open_port)])
    assert http.exit_code == 0
    assert json.loads(http.stdout) == {"result": "SUCCESS", "reason": None, "http_status": 200}

    if host is None:
        # The receiving account is the sender itself here; naming it exercises the same path a cross-account call takes.
        udp = runner.invoke(
            cats_cli,
            [
                "run",
                "udp-call",
                "--account",
                account.name,
                "--target",
                target,
                "--host",
                call_host,
                "--port",
                "5000",
                "--receiving-account",
                account.name,
            ],
        )
        assert udp.exit_code == 0
        udp_output = json.loads(udp.stdout)
        correlation_id = udp_output["correlation_id"]
        assert udp_output == {"result": "SENT", "reason": None, "correlation_id": correlation_id}
        s3 = runner.invoke(cats_cli, ["run", "receive-s3", "--account", account.name, "--target", target, "--correlation-id", correlation_id])
        assert s3.exit_code == 0
        assert json.loads(s3.stdout) == {"received": True}


def assert_mcp_transport(account: AccountUnderTest, target: str, open_port: int, host: str | None = None) -> None:
    """Exercise MCP tool functions against a deployed stack.

    Asserts: list_accounts includes account, account_info reflects deployed DNS,
    http_call SUCCESS to host:open_port with the listener's 200, udp_call + receive_s3
    roundtrip (roundtrip only against the account's own EC2).
    """
    call_host = _host_for(account, target, host)

    names = [a["name"] for a in list_accounts()["accounts"]]
    assert account.name in names

    info = account_info(account=account.name)
    targets = info["targets"]
    assert isinstance(targets, dict)
    assert targets[target]["ec2"]["dns_name"] == getattr(account, target).ec2_dns_name

    assert http_call(account=account.name, target=target, host=call_host, port=open_port) == {"result": "SUCCESS", "reason": None, "http_status": 200}

    if host is None:
        udp = udp_call(account=account.name, target=target, host=call_host, port=5000, receiving_account=account.name)
        correlation_id = udp["correlation_id"]
        assert correlation_id is not None
        assert udp == {"result": "SENT", "reason": None, "correlation_id": correlation_id}
        assert receive_s3(account=account.name, target=target, correlation_id=correlation_id) == {"received": True}
