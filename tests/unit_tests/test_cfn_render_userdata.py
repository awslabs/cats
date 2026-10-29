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

"""Tests for the EC2 listener's user data: ``render_userdata`` and the per-listener prefix.

Every listener boots with the same script, ``cats/cfn/templates/userdata.sh.j2`` rendered with the four
files it installs (``ListenerCode``). The stack template embeds that once, in ``Mappings``, and gives
each listener a prefix of shell variables ending in ``${Userdata}``, which Fn::Sub replaces with the
script. These tests render the prefix through the stack template, perform that substitution the way
CloudFormation does, and check the script the instance actually runs: as text, with ``bash -n``, and
with shellcheck.

Regression tests: the license header was once prepended above the shebang in
tcpserver.py/udpserver.py, so systemd exec of the installed scripts failed (Exec format
error, status=203/EXEC) and no listener bound.
"""

import re
import shlex
import shutil
import subprocess
from pathlib import Path

import boto3
import pytest

from cats.cfn.ec2_server import tcpserver, udpserver
from cats.cfn.render import ListenerCode, render_template, render_userdata
from cats.cfn.resolve import resolve_account, resolve_account_settings
from cats.config import CatsConfig
from cats.constants import LISTENER_FAILED_MARKER, LISTENER_READY_MARKER
from tests.utils import load_cfn_template, make_cats_config

EXPECTED_SHEBANG = "#!/usr/bin/python3"
EC2_SERVER_DIR = Path(__file__).parents[2] / "cats" / "cfn" / "ec2_server"
ACCOUNT_NAME = "testaccount"
TARGET_NAME = "privateSubnet"


def shellcheck_binary() -> str:
    """The shellcheck executable the dev dependency shellcheck-py installs into the environment."""
    path = shutil.which("shellcheck")
    if path is None:
        raise FileNotFoundError("shellcheck is not on PATH: it is the dev dependency shellcheck-py, run the suite with `uv run pytest`")
    return path


def make_userdata_lines() -> list[str]:
    """The shared script as every listener receives it, split into lines."""
    return render_userdata(ListenerCode.load()).splitlines()


def render_prefix(mock_aws_infra: dict[str, str], tcp_ports: list[int], udp_ports: list[int]) -> str:
    """Render one listener through the stack template and return its Fn::Sub prefix, the text before ``${Userdata}`` is resolved."""
    config: CatsConfig = make_cats_config(
        network_targets={
            TARGET_NAME: {
                "vpc_id": mock_aws_infra["VPC"],
                "subnet_ids": [mock_aws_infra["SUBNET_A"]],
                "network_settings": {"tcp_listen_ports": tcp_ports, "udp_listen_ports": udp_ports},
            }
        },
        enabled_network_handlers=["ec2"],
    )
    account = config.accounts_under_test[ACCOUNT_NAME]
    resolved = resolve_account(ACCOUNT_NAME, account, boto3.Session(region_name="us-east-1"))
    rendered = render_template(resolved, config.cats_deployment_settings, resolve_account_settings(account), securityhub_region=None)
    template = load_cfn_template(rendered)
    prefix: str = template["Resources"][f"Ec2Listener{TARGET_NAME}"]["Properties"]["UserData"]["Fn::Base64"]["Fn::Sub"][0]
    return prefix


def assemble_userdata(prefix: str) -> str:
    """Resolve the prefix's Fn::Sub variables as CloudFormation would: the script from the mapping, sample values for the two Refs."""
    assembled = prefix.replace("${Userdata}", render_userdata(ListenerCode.load()))
    return assembled.replace("${CatsResponseBucket}", "cats-responses-bucket").replace("${CatsUdpReceiptToken}", "f" * 32)


def first_embedded_script_line(lines: list[str], heredoc_start: str) -> str:
    """Return the first line of the script embedded after the given heredoc marker."""
    return lines[lines.index(heredoc_start) + 1]


# =============================================================================
# The shared script: render_userdata
# =============================================================================


def test_tcpserver_script_first_line_is_shebang() -> None:
    # arrange
    lines = make_userdata_lines()

    # act
    first_line = first_embedded_script_line(lines, "cat > /usr/local/bin/tcpserver << 'TCPEOF'")

    # assert
    assert first_line == EXPECTED_SHEBANG


def test_udpserver_script_first_line_is_shebang() -> None:
    # arrange
    lines = make_userdata_lines()

    # act
    first_line = first_embedded_script_line(lines, "cat > /usr/local/bin/udpserver << 'UDPEOF'")

    # assert
    assert first_line == EXPECTED_SHEBANG


@pytest.mark.parametrize(
    ("field", "filename", "terminator"),
    [
        ("udpserver_py", "udpserver.py", "UDPEOF"),
        ("tcpserver_py", "tcpserver.py", "TCPEOF"),
        ("tcpserver_service", "tcpserver.service", "TCPSVCEOF"),
        ("udpserver_service", "udpserver.service", "UDPSVCEOF"),
    ],
)
def test_each_installed_file_is_embedded_verbatim_in_its_heredoc(field: str, filename: str, terminator: str) -> None:
    """The code on the instance is the code in the repo: each file sits whole between its heredoc's open and close."""
    # arrange
    code = ListenerCode.load()
    script = render_userdata(code)

    # act
    heredoc_body = script.split(f"<< '{terminator}'\n", 1)[1].split(f"\n{terminator}\n", 1)[0]

    # assert
    assert heredoc_body == getattr(code, field)


def test_listener_code_loads_the_four_files_without_their_license_headers() -> None:
    # act
    code = ListenerCode.load()

    # assert - each field starts at the file's first line of content
    expected = {
        "udpserver_py": "#!/usr/bin/python3",
        "tcpserver_py": "#!/usr/bin/python3",
        "tcpserver_service": "[Unit]",
        "udpserver_service": "[Unit]",
    }
    assert {field: getattr(code, field).splitlines()[0] for field in expected} == expected


def test_service_units_execstart_invoke_python3_explicitly_with_their_env_file_variables() -> None:
    """systemd exec must not depend on the shebang, and every per-listener value arrives through the env file.

    The receipt token is deliberately absent from the udpserver line: the script reads it from the
    environment systemd exports, so it is not in `ps` output (tidiness, not secrecy: it is a stack output)."""
    # arrange
    lines = make_userdata_lines()
    expected = [
        "ExecStart=/usr/bin/python3 -u /usr/local/bin/tcpserver $CATS_TCP_PORTS",
        "ExecStart=/usr/bin/python3 -u /usr/local/bin/udpserver $CATS_BUCKET $CATS_UDP_PORTS",
    ]

    # act
    execstart_lines = [line for line in lines if line.startswith("ExecStart=")]

    # assert
    assert execstart_lines == expected


def test_service_units_read_their_own_env_file() -> None:
    # arrange
    lines = make_userdata_lines()
    expected = ["EnvironmentFile=/etc/cats/tcpserver.env", "EnvironmentFile=/etc/cats/udpserver.env"]

    # act
    env_file_lines = [line for line in lines if line.startswith("EnvironmentFile=")]

    # assert
    assert env_file_lines == expected


def test_env_files_are_written_root_only_from_the_prefix_variables() -> None:
    """The receipt token lands in a 0600 file under a 0700 directory, read by PID 1 before the unit drops privileges."""
    # arrange
    lines = make_userdata_lines()
    expected = [
        "install -d -m 700 /etc/cats",
        "printf 'CATS_TCP_PORTS=%s\\n' \"$CATS_TCP_PORTS\" > /etc/cats/tcpserver.env",
        "printf 'CATS_BUCKET=%s\\nCATS_UDP_RECEIPT_TOKEN=%s\\nCATS_UDP_PORTS=%s\\n' "
        + '"$CATS_BUCKET" "$CATS_UDP_RECEIPT_TOKEN" "$CATS_UDP_PORTS" > /etc/cats/udpserver.env',
        "chmod 600 /etc/cats/tcpserver.env /etc/cats/udpserver.env",
    ]

    # act
    env_lines = [line for line in lines if "/etc/cats" in line and not line.startswith(("EnvironmentFile=", "#"))]

    # assert
    assert env_lines == expected


def test_service_units_restart_a_dead_listener_forever() -> None:
    """A listener crash must never take out a test run: systemd restarts it, with no start rate limit."""
    # arrange
    lines = make_userdata_lines()
    expected = [
        "StartLimitIntervalSec=0",
        "Restart=always",
        "RestartSec=1",
        "StartLimitIntervalSec=0",
        "Restart=always",
        "RestartSec=1",
    ]

    # act
    restart_lines = [line for line in lines if line.startswith(("Restart", "StartLimit"))]

    # assert
    assert restart_lines == expected


def _hardening(name: str) -> list[str]:
    return [
        "DynamicUser=yes",
        f"StateDirectory=cats-{name}",
        f"Environment=HOME=/var/lib/cats-{name}",
        "AmbientCapabilities=CAP_NET_BIND_SERVICE",
        "CapabilityBoundingSet=CAP_NET_BIND_SERVICE",
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
    ]


HARDENING_KEYS = {line.split("=")[0] for line in _hardening("x")}


def test_service_units_run_as_a_transient_unprivileged_user() -> None:
    """Untrusted bytes are parsed by a throwaway user with a read-only filesystem; only the bind capability survives.

    The dynamic user has no passwd entry, and the AWS CLI dies at import without a resolvable
    home, so each unit owns a state directory and points HOME at it.
    """
    # arrange
    lines = make_userdata_lines()

    # act
    hardening_lines = [line for line in lines if line.split("=")[0] in HARDENING_KEYS]

    # assert - once per unit, tcp then udp
    assert hardening_lines == _hardening("tcpserver") + _hardening("udpserver")


def test_both_units_are_installed_and_enabled_only_when_their_port_list_is_non_empty() -> None:
    """The script is the same on every listener; the prefix's variables decide which units run."""
    # act
    lines = [line for line in make_userdata_lines() if line.startswith(("cat > /etc/systemd/system/", 'if [ -n "$CATS_'))]

    # assert
    expected = [
        "cat > /etc/systemd/system/tcpserver.service << 'TCPSVCEOF'",
        "cat > /etc/systemd/system/udpserver.service << 'UDPSVCEOF'",
        'if [ -n "$CATS_TCP_PORTS" ]; then systemctl enable --now tcpserver.service; fi',
        'if [ -n "$CATS_UDP_PORTS" ]; then systemctl enable --now udpserver.service; fi',
    ]
    assert lines == expected


def test_script_disables_ssh_before_waiting_for_the_ports() -> None:
    # act
    lines = make_userdata_lines()
    stop = lines.index("systemctl stop sshd")

    # assert - sshd goes first, so the readiness wait never counts it as a listener still to come
    assert lines[stop : stop + 2] == ["systemctl stop sshd", "systemctl disable sshd"]


def test_script_waits_for_every_configured_port_to_be_bound() -> None:
    """`systemctl enable --now` returns before Python binds; the script polls `ss` for each port for up to 20s (40 x 0.5s)."""
    # act
    lines = make_userdata_lines()

    # assert
    expected = [
        "unbound() {",
        '  for p in $(echo "$CATS_TCP_PORTS" | tr , \' \'); do ss -Hltn "sport = :$p" | grep -q . || printf \' tcp/%s\' "$p"; done',
        '  for p in $(echo "$CATS_UDP_PORTS" | tr , \' \'); do ss -Hlun "sport = :$p" | grep -q . || printf \' udp/%s\' "$p"; done',
        "}",
        'for _ in $(seq 40); do missing=$(unbound); [ -z "$missing" ] && break; sleep 0.5; done',
    ]
    start = lines.index("unbound() {")
    assert lines[start : start + 5] == expected


def test_script_ends_with_the_verdict_the_deploy_reads_from_the_console() -> None:
    """The last line is the ready marker; the failed marker names the unbound ports and exits non-zero before it (cats/cfn/readiness.py)."""
    # act
    lines = make_userdata_lines()

    # assert
    expected_failure = (
        f'if [ -n "$missing" ]; then echo "{LISTENER_FAILED_MARKER}$missing"; systemctl status tcpserver udpserver --no-pager -n 3; exit 1; fi'
    )
    assert lines[-2:] == [expected_failure, f"echo '{LISTENER_READY_MARKER}'"]


# =============================================================================
# The per-listener prefix, as the stack template renders it
# =============================================================================


def test_prefix_with_both_port_lists_sets_every_variable(mock_aws_infra: dict[str, str]) -> None:
    # act
    prefix = render_prefix(mock_aws_infra, tcp_ports=[8080, 443], udp_ports=[5000])

    # assert
    expected = "\n".join(
        [
            "#!/bin/bash",
            "CATS_TCP_PORTS=8080,443",
            "CATS_UDP_PORTS=5000",
            "CATS_BUCKET=${CatsResponseBucket}",
            "CATS_UDP_RECEIPT_TOKEN=${CatsUdpReceiptToken}",
            "${Userdata}",
            "",
        ]
    )
    assert prefix == expected


def test_prefix_without_udp_ports_never_holds_the_receipt_token(mock_aws_infra: dict[str, str]) -> None:
    """A TCP-only listener has no use for the token, so its user data does not carry it."""
    # act
    prefix = render_prefix(mock_aws_infra, tcp_ports=[8080], udp_ports=[])

    # assert
    expected = "#!/bin/bash\nCATS_TCP_PORTS=8080\nCATS_UDP_PORTS=\n${Userdata}\n"
    assert prefix == expected


def test_prefix_without_tcp_ports_leaves_the_tcp_variable_empty(mock_aws_infra: dict[str, str]) -> None:
    """tcp_listen_ports: [] is a documented config; the empty variable is what keeps the tcp unit disabled."""
    # act
    prefix = render_prefix(mock_aws_infra, tcp_ports=[], udp_ports=[5000])

    # assert
    expected = "\n".join(
        [
            "#!/bin/bash",
            "CATS_TCP_PORTS=",
            "CATS_UDP_PORTS=5000",
            "CATS_BUCKET=${CatsResponseBucket}",
            "CATS_UDP_RECEIPT_TOKEN=${CatsUdpReceiptToken}",
            "${Userdata}",
            "",
        ]
    )
    assert prefix == expected


def test_variables_the_script_reads_are_exactly_the_variables_the_prefix_sets(mock_aws_infra: dict[str, str]) -> None:
    """The seam between the two halves: a variable read by the script or a unit file and set by no prefix would expand to nothing on the box."""
    # arrange
    prefix = render_prefix(mock_aws_infra, tcp_ports=[8080], udp_ports=[5000])
    script = render_userdata(ListenerCode.load())

    # act
    read_by_script = set(re.findall(r"\$(CATS_[A-Z_]+)", script))
    set_by_prefix = set(re.findall(r"^(CATS_[A-Z_]+)=", prefix, flags=re.MULTILINE))

    # assert
    expected = {"CATS_BUCKET", "CATS_TCP_PORTS", "CATS_UDP_PORTS", "CATS_UDP_RECEIPT_TOKEN"}
    assert (read_by_script, set_by_prefix) == (expected, expected)


def test_port_lists_survive_the_journey_from_prefix_to_server(mock_aws_infra: dict[str, str]) -> None:
    """The prefix joins each port list with a comma; the unit's ExecStart expands the variable unbraced, so systemd
    splits it on whitespace; each server splits its one argument on the comma. Nothing states this agreement,
    so this test walks the whole path: the value is one word, and the server reads back the configured ports.
    """
    # arrange
    tcp_ports, udp_ports = [8080, 443, 8443], [5000, 5001]
    prefix = render_prefix(mock_aws_infra, tcp_ports=tcp_ports, udp_ports=udp_ports)
    assigned = dict(re.findall(r"^(CATS_[A-Z_]+)=(.*)$", prefix, flags=re.MULTILINE))

    # act - systemd's whitespace split of the unbraced $VAR, then each server's parse
    tcp_words = shlex.split(assigned["CATS_TCP_PORTS"])
    udp_words = shlex.split(assigned["CATS_UDP_PORTS"])
    result = (tcp_words, udp_words, tcpserver.parse_ports(tcp_words[0]), udpserver.parse_ports(udp_words[0]))

    # assert
    assert result == (["8080,443,8443"], ["5000,5001"], tcp_ports, udp_ports)


# =============================================================================
# The assembled user data is valid bash
# =============================================================================


@pytest.mark.parametrize(("tcp_ports", "udp_ports"), [([8080], []), ([8080, 443], [5000])], ids=["tcp only", "tcp and udp"])
def test_assembled_userdata_parses_under_bash(mock_aws_infra: dict[str, str], tcp_ports: list[int], udp_ports: list[int]) -> None:
    """An unbalanced `if` or a bad quote would otherwise surface as a silent TIMEOUT after a five-minute deploy.

    A heredoc whose terminator never comes is a warning bash 3.2 (macOS) does not emit under -n; the
    shellcheck test below reports it as an error on every platform.
    """
    # arrange
    userdata = assemble_userdata(render_prefix(mock_aws_infra, tcp_ports, udp_ports))

    # act
    completed = subprocess.run(["bash", "-n"], input=userdata, capture_output=True, text=True, check=False)  # noqa: S607

    # assert
    assert (completed.returncode, completed.stderr) == (0, "")


@pytest.mark.parametrize(("tcp_ports", "udp_ports"), [([8080], []), ([8080, 443], [5000])], ids=["tcp only", "tcp and udp"])
def test_assembled_userdata_passes_shellcheck(mock_aws_infra: dict[str, str], tcp_ports: list[int], udp_ports: list[int]) -> None:
    """The dev dependency shellcheck-py puts the binary on PATH under `uv run pytest`; a missing binary is a broken environment, not a skip."""
    # arrange
    userdata = assemble_userdata(render_prefix(mock_aws_infra, tcp_ports, udp_ports))

    # act
    completed = subprocess.run([shellcheck_binary(), "--shell=bash", "-"], input=userdata, capture_output=True, text=True, check=False)  # noqa: S603

    # assert
    assert (completed.returncode, completed.stdout) == (0, "")
