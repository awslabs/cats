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

"""Integration tests: network config with EC2 handler.

One deploy covers the http/tcp/udp handlers and, because `network_config` sets
`account_settings.allow_shell: true`, the shell() happy paths as well.
The Python guard (allow_shell: false) is covered by unit tests.
"""

import json
import uuid
from collections.abc import Generator
from contextlib import closing
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from cats.constants import CatsNetworkResponse, CatsNetworkResult
from cats.report_run import ResultCounts
from cats.resources.account_under_test import AccountUnderTest
from tests.integration.constants import ACCOUNT_ID, REGION, skip_wrong_account
from tests.integration.reporting_helpers import run_nested_cats_session
from tests.integration.transport_helpers import assert_cli_transport, assert_mcp_transport
from tests.utils import deploy_and_destroy

ACCOUNT_KEYNAME = "ec2handler"


@pytest.fixture(scope="module")
def deployed_ec2_stack(
    tmp_path_factory: pytest.TempPathFactory,
    network_config: dict[str, Any],
) -> Generator[AccountUnderTest, None, None]:
    """Deploy EC2 handler stack once for all tests in this module."""
    # closing() destroys the stack the moment this fixture tears down, even if the body raises
    with closing(deploy_and_destroy(tmp_path_factory, network_config, ACCOUNT_KEYNAME, REGION, "ec2handler")) as deployed:
        next(deployed)
        account = AccountUnderTest(ACCOUNT_KEYNAME)
        print(f"[fixture] EC2 DNS: {account.private.ec2_dns_name}")
        yield account


@skip_wrong_account
def test_tcp_call_to_open_port_succeeds(deployed_ec2_stack: AccountUnderTest) -> None:
    """TCP call to open port returns SUCCESS."""
    # act
    print(f"[test] ec2_tcp_call to {deployed_ec2_stack.private.ec2_dns_name}:8080")
    result = deployed_ec2_stack.private.ec2_tcp_call(deployed_ec2_stack.private.ec2_dns_name, 8080)
    print(f"[test] Result: {result}")

    # assert
    assert result == CatsNetworkResponse.SUCCESS


@skip_wrong_account
def test_tcp_call_to_the_second_listen_port_succeeds(deployed_ec2_stack: AccountUnderTest) -> None:
    """The second TCP port is listening too: the comma-joined port list reached the server as one argument and was split there."""
    # act
    print(f"[test] ec2_tcp_call to {deployed_ec2_stack.private.ec2_dns_name}:8081")
    result = deployed_ec2_stack.private.ec2_tcp_call(deployed_ec2_stack.private.ec2_dns_name, 8081)
    print(f"[test] Result: {result}")

    # assert
    assert result == CatsNetworkResponse.SUCCESS


@skip_wrong_account
def test_http_call_to_open_port_succeeds(deployed_ec2_stack: AccountUnderTest) -> None:
    """HTTP call to open port returns SUCCESS with the listener's 200 on the result."""
    # act
    print(f"[test] ec2_http_call to {deployed_ec2_stack.private.ec2_dns_name}:8080")
    result = deployed_ec2_stack.private.ec2_http_call(deployed_ec2_stack.private.ec2_dns_name, 8080)
    print(f"[test] Result: {result!r}")

    # assert - tcpserver answers any bytes with `HTTP/1.1 200 OK`, so http_status is populated end to end
    assert result == CatsNetworkResponse.SUCCESS
    assert result.http_status == 200


@skip_wrong_account
def test_https_call_to_plaintext_listener_is_tls_failure(deployed_ec2_stack: AccountUnderTest) -> None:
    """HTTPS to the plain-HTTP listener: TCP connects, the handshake fails deterministically."""
    # act
    result = deployed_ec2_stack.private.ec2_https_call(deployed_ec2_stack.private.ec2_dns_name, 8080)
    print(f"[test] Result: {result!r}")

    # assert
    assert result == CatsNetworkResponse.TLS_FAILURE


@skip_wrong_account
def test_tcp_call_to_nonexistent_name_is_dns_failure(deployed_ec2_stack: AccountUnderTest) -> None:
    """A name under the RFC 2606 reserved .invalid TLD is guaranteed NXDOMAIN: no packet is sent."""
    # act
    result = deployed_ec2_stack.private.ec2_tcp_call("cats-nonexistent.invalid", 8080)
    print(f"[test] Result: {result!r}")

    # assert
    assert result == CatsNetworkResponse.DNS_FAILURE


@skip_wrong_account
def test_http_call_to_nonexistent_name_is_dns_failure(deployed_ec2_stack: AccountUnderTest) -> None:
    """HTTP resolves the same way as TCP, so the same target classifies the same."""
    # act
    result = deployed_ec2_stack.private.ec2_http_call("cats-nonexistent.invalid", 8080)
    print(f"[test] Result: {result!r}")

    # assert
    assert result == CatsNetworkResponse.DNS_FAILURE


@skip_wrong_account
def test_udp_call_receipt_arrives(deployed_ec2_stack: AccountUnderTest) -> None:
    """UDP call creates S3 receipt."""
    # act
    print(f"[test] ec2_udp_call to {deployed_ec2_stack.private.ec2_dns_name}:5000")
    sent = deployed_ec2_stack.private.ec2_udp_call(deployed_ec2_stack.private.ec2_dns_name, 5000)
    print(f"[test] Sent: {sent!r}")
    arrived = deployed_ec2_stack.private.receive_s3(sent, timeout=15)
    print(f"[test] Arrived: {arrived}")

    # assert
    assert sent == CatsNetworkResponse.SENT
    assert arrived is True


@skip_wrong_account
def test_udp_call_to_the_second_listen_port_leaves_a_receipt(deployed_ec2_stack: AccountUnderTest) -> None:
    """The second UDP port is listening too, with the same bucket and token: the comma-joined list was split on the server."""
    # act
    print(f"[test] ec2_udp_call to {deployed_ec2_stack.private.ec2_dns_name}:5001")
    sent = deployed_ec2_stack.private.ec2_udp_call(deployed_ec2_stack.private.ec2_dns_name, 5001)
    print(f"[test] Sent: {sent!r}")
    arrived = deployed_ec2_stack.private.receive_s3(sent, timeout=15)
    print(f"[test] Arrived: {arrived}")

    # assert
    assert sent == CatsNetworkResponse.SENT
    assert arrived is True


@skip_wrong_account
def test_udp_call_to_nonexistent_name_is_dns_failure(deployed_ec2_stack: AccountUnderTest) -> None:
    """UDP classifies resolver failures too; receive_s3 refuses a datagram that never left."""
    # act
    sent = deployed_ec2_stack.private.ec2_udp_call("cats-nonexistent.invalid", 5000)
    print(f"[test] Sent: {sent!r}")

    # assert
    assert sent == CatsNetworkResponse.DNS_FAILURE
    with pytest.raises(ValueError, match="the datagram never left the host"):
        deployed_ec2_stack.private.receive_s3(sent, timeout=1)


@skip_wrong_account
def test_tcp_call_to_closed_port_refused(deployed_ec2_stack: AccountUnderTest) -> None:
    """TCP self-call to a closed port returns REFUSED - path open, nothing listening.

    Self-calls (the listener probing its own address) never leave the host, so the
    scoped security group ingress does not apply. A probe from any other host
    to an unlisted port gets TIMEOUT - covered in test_lambda_handler.py.
    """
    # act
    print(f"[test] ec2_tcp_call to {deployed_ec2_stack.private.ec2_dns_name}:9999 (closed)")
    result = deployed_ec2_stack.private.ec2_tcp_call(deployed_ec2_stack.private.ec2_dns_name, 9999)
    print(f"[test] Result: {result}")

    # assert
    assert result == CatsNetworkResponse.REFUSED


@skip_wrong_account
def test_tcp_call_to_blackhole_times_out(deployed_ec2_stack: AccountUnderTest) -> None:
    """TCP call to an unroutable address returns TIMEOUT - packets dropped."""
    # act - 192.0.2.1 is TEST-NET-1 (RFC 5737): never routable, packets are dropped
    result = deployed_ec2_stack.private.ec2_tcp_call("192.0.2.1", 8080)
    print(f"[test] Result: {result}")

    # assert
    assert result == CatsNetworkResponse.TIMEOUT


@skip_wrong_account
def test_http_call_to_closed_port_refused(deployed_ec2_stack: AccountUnderTest) -> None:
    """HTTP self-call to a closed port returns REFUSED - path open, nothing listening.

    Self-calls bypass security groups (traffic to the instance's own address never
    leaves the host), so the scoped ingress does not apply here.
    """
    # act
    print(f"[test] ec2_http_call to {deployed_ec2_stack.private.ec2_dns_name}:9999 (closed)")
    result = deployed_ec2_stack.private.ec2_http_call(deployed_ec2_stack.private.ec2_dns_name, 9999)
    print(f"[test] Result: {result}")

    # assert
    assert result == CatsNetworkResponse.REFUSED


@skip_wrong_account
def test_http_call_to_blackhole_times_out(deployed_ec2_stack: AccountUnderTest) -> None:
    """HTTP call to an unroutable address returns TIMEOUT - packets dropped."""
    # act - 192.0.2.1 is TEST-NET-1 (RFC 5737): never routable, packets are dropped
    result = deployed_ec2_stack.private.ec2_http_call("192.0.2.1", 8080)
    print(f"[test] Result: {result}")

    # assert
    assert result == CatsNetworkResponse.TIMEOUT


@skip_wrong_account
def test_udp_call_to_closed_port_no_receipt(deployed_ec2_stack: AccountUnderTest) -> None:
    """UDP call to closed port is SENT (the datagram left) but has no S3 receipt."""
    # act
    print(f"[test] ec2_udp_call to {deployed_ec2_stack.private.ec2_dns_name}:9999 (closed)")
    sent = deployed_ec2_stack.private.ec2_udp_call(deployed_ec2_stack.private.ec2_dns_name, 9999)
    print(f"[test] Sent: {sent!r}")
    arrived = deployed_ec2_stack.private.receive_s3(sent, timeout=5)
    print(f"[test] Arrived: {arrived}")

    # assert
    assert sent == CatsNetworkResponse.SENT
    assert arrived is False


@skip_wrong_account
def test_deployed_assets_has_network_target(deployed_ec2_stack: AccountUnderTest) -> None:
    """AccountUnderTest exposes configured network targets."""
    # act
    targets = deployed_ec2_stack.available_network_targets

    # assert
    assert "private" in targets
    handler = deployed_ec2_stack.private
    assert handler._deployed.account_name == ACCOUNT_KEYNAME
    assert handler._deployed.ec2.dns_name != ""
    assert handler._deployed.ec2.instance_id.startswith("i-")
    assert handler._deployed.response_bucket_name != ""


@skip_wrong_account
def test_cli_run_transport_reaches_ec2_handler(deployed_ec2_stack: AccountUnderTest) -> None:
    """`cats run` subcommands round-trip against the deployed EC2 handler."""
    assert_cli_transport(deployed_ec2_stack, target="private", open_port=8080)


@skip_wrong_account
def test_mcp_tools_reach_ec2_handler(deployed_ec2_stack: AccountUnderTest) -> None:
    """MCP tool functions round-trip against the deployed EC2 handler."""
    assert_mcp_transport(deployed_ec2_stack, target="private", open_port=8080)


@skip_wrong_account
def test_shell_echo_returns_stdout(deployed_ec2_stack: AccountUnderTest) -> None:
    """shell('echo hello') returns exit_code=0 and stripped stdout='hello'."""
    # act
    result = deployed_ec2_stack.private.shell("echo hello")

    # assert
    assert result.exit_code == 0
    assert result.stdout == "hello"


@skip_wrong_account
def test_shell_nonzero_exit(deployed_ec2_stack: AccountUnderTest) -> None:
    """shell('exit 42') returns exit_code=42 without raising."""
    # act
    result = deployed_ec2_stack.private.shell("exit 42")

    # assert
    assert result.exit_code == 42


@skip_wrong_account
def test_shell_multiline(deployed_ec2_stack: AccountUnderTest) -> None:
    """shell(['echo a', 'echo b']) runs both lines; stdout is 'a\\nb'."""
    # act
    result = deployed_ec2_stack.private.shell(["echo a", "echo b"])

    # assert
    assert result.exit_code == 0
    assert result.stdout == "a\nb"


def _send_raw_udp(account: AccountUnderTest, payload: str, correlation_id: str, port: int = 5000) -> CatsNetworkResult:
    """Send a raw UDP datagram to the account's own listener via shell(), bypassing udp_call.

    payload is interpolated into a quoted shell command and must contain no quote characters.
    Returns the SENT result an external sender would construct for receive_s3 (see
    "External senders" in docs/concepts/network-testing.md).
    """
    dns = account.private.ec2_dns_name
    command = f"python3 -c \"import socket; socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto('{payload}'.encode(), ('{dns}', {port}))\""
    result = account.private.shell(command)
    assert result.exit_code == 0, f"raw UDP send failed: {result.stderr}"
    return CatsNetworkResult(response=CatsNetworkResponse.SENT, correlation_id=correlation_id)


@skip_wrong_account
def test_raw_untokened_udp_packet_produces_no_receipt(deployed_ec2_stack: AccountUnderTest) -> None:
    """A datagram without the '{token}:' prefix is dropped - no S3 write."""
    # arrange
    correlation_id = f"untokened-{uuid.uuid4()}"

    # act
    sent = _send_raw_udp(deployed_ec2_stack, correlation_id, correlation_id)
    arrived = deployed_ec2_stack.private.receive_s3(sent, timeout=10)

    # assert
    assert arrived is False


@skip_wrong_account
def test_raw_tokened_udp_packet_lands_in_s3(deployed_ec2_stack: AccountUnderTest) -> None:
    """An external sender prefixing the udp_receipt_token gets its receipt recorded."""
    # arrange
    correlation_id = f"tokened-{uuid.uuid4()}"
    token = deployed_ec2_stack.udp_receipt_token
    assert token is not None

    # act
    sent = _send_raw_udp(deployed_ec2_stack, f"{token}:{correlation_id}", correlation_id)
    arrived = deployed_ec2_stack.private.receive_s3(sent, timeout=15)

    # assert
    assert arrived is True


_OSCAL_SCHEMA_PATH = Path(__file__).parent.parent / "unit_tests" / "schemas" / "oscal_assessment-results_schema_1.2.1.json"


def _oscal_schema() -> dict[str, Any]:
    """NIST's schema with its ECMA `\\p{L}`/`\\p{N}` classes rewritten for Python's `re`, as the unit tests do."""

    def pythonize(value: Any) -> Any:  # noqa: ANN401 - recursive JSON transform
        if isinstance(value, dict):
            return {
                key: (val.replace(r"\p{L}", r"[^\W\d_]").replace(r"\p{N}", r"\d") if key == "pattern" and isinstance(val, str) else pythonize(val))
                for key, val in value.items()
            }
        if isinstance(value, list):
            return [pythonize(item) for item in value]
        return value

    schema: dict[str, Any] = pythonize(json.loads(_OSCAL_SCHEMA_PATH.read_text()))
    return schema


@skip_wrong_account
def test_reporting_oscal_from_a_real_probe_result(deployed_ec2_stack: AccountUnderTest, network_config: dict[str, Any], tmp_path: Path) -> None:
    """A nested session probes the listener; the registry maps its standard to controls, and the OSCAL findings carry them, schema-valid."""
    # arrange
    conftest_source = """
from cats import StandardMetadata


def pytest_cats_standards():
    return {"ACME-G-DCP-1": StandardMetadata(control_ids=["sc-7", "sc-7.5"], control_group="Network")}
"""
    test_source = """
from cats import AccountUnderTest, CatsNetworkResponse, cats


@cats(standard_name="ACME-G-DCP-1")
def test_listener_answers_on_its_open_port(ec2handler_account: AccountUnderTest) -> None:
    target = ec2handler_account.private
    assert target.ec2_tcp_call(target.ec2_dns_name, 8080) == CatsNetworkResponse.SUCCESS
"""

    # act
    run = run_nested_cats_session(network_config, ["cats_json", "oscal_ar"], test_source, tmp_path, conftest_source=conftest_source)
    assert run.returncode == 0, run.summary()
    report = run.report()
    run_id = report["run"]["run_id"]
    assessment_results = run.assessment_results()
    jsonschema.validate(assessment_results, _oscal_schema())
    result = assessment_results["assessment-results"]["results"][0]
    metadata_props = {prop["name"]: prop["value"] for prop in assessment_results["assessment-results"]["metadata"]["props"]}

    # assert
    expected_counts: ResultCounts = {"total": 1, "passed": 1, "failed": 0, "error": 0, "skipped": 0, "xfailed": 0, "xpassed": 0, "not_run": 0}
    expected = {
        "counts": expected_counts,
        "accounts": [{"account_name": ACCOUNT_KEYNAME, "account_id": ACCOUNT_ID}],
        "writers": [
            {"format": "cats_json", "file": f"report-{run_id}.json", "status": "ok", "detail": ""},
            {"format": "oscal_ar", "file": f"assessment-results-{run_id}.json", "status": "ok", "detail": ""},
        ],
        "oscal_run_id": run_id,
        "findings": [("sc-7", "satisfied"), ("sc-7.5", "satisfied")],
        "control_group_props": ["Network"],
    }
    assert expected == {
        "counts": report["run"]["counts"],
        "accounts": report["run"]["accounts"],
        "writers": report["run"]["writers"],
        "oscal_run_id": metadata_props["cats-run-id"],
        "findings": sorted((finding["target"]["target-id"], finding["target"]["status"]["state"]) for finding in result["findings"]),
        "control_group_props": [
            prop["value"] for observation in result["observations"] for prop in observation.get("props", []) if prop["name"] == "control-group"
        ],
    }
