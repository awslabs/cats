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

"""Every example in every rendered prompt must be one an agent can copy and have work.

Every yaml block must be a valid CatsConfig fragment. The AI setup journey once produced
an invalid config: the setup prompt taught cats_test_settings / cats_automation_settings
(keys that never existed) and a network target name the schema rejects.

Every python block that asserts a network result must assert one its probe can return,
and no example may point a plaintext `http_call` at a TLS port on a named host. The
testing prompt once taught `http_call(host="example.com", port=443)` asserting SUCCESS:
a plaintext `GET /` to a TLS port draws a TLS alert or a close, which the probe
classifies FAILURE or REFUSED (cats/cfn/ec2_server/README.md), so the example could
never pass and an agent copying it shipped a test that fails on a healthy path.

Prompts ship inside the wheel and are read verbatim by agents, so drift from the schema
or from the probe's classification rules is a first-run failure.
"""

import ast
import re
from typing import NamedTuple

import pytest
import yaml

from cats.config import CatsConfig
from cats.constants import CatsNetworkResponse
from cats.prompts import PromptName, get_prompt

_YAML_BLOCK = re.compile(r"```yaml\n(.*?)```", re.S)
_PYTHON_BLOCK = re.compile(r"```python\n(.*?)```", re.S)
_BASH_BLOCK = re.compile(r"```bash\n(.*?)```", re.S)
_RUN_HTTP_CALL = re.compile(r"^cats run http-call\s+(.*)$", re.M)
_HOST_OPTION = re.compile(r"--host\s+(\S+)")
_PORT_OPTION = re.compile(r"--port\s+(\d+)")

# What each probe can return, from the ec2_*_call docstrings in cats/resources/network_interact.py
# and the classification rules in cats/cfn/ec2_server/README.md. UDP sends on an unconnected
# socket, so it never learns REFUSED or TIMEOUT; only an https handshake can fail as TLS_FAILURE.
_RESULTS_BY_PROTOCOL: dict[str, frozenset[CatsNetworkResponse]] = {
    "http": frozenset(
        {
            CatsNetworkResponse.SUCCESS,
            CatsNetworkResponse.REFUSED,
            CatsNetworkResponse.TIMEOUT,
            CatsNetworkResponse.DNS_FAILURE,
            CatsNetworkResponse.FAILURE,
        }
    ),
    "https": frozenset(
        {
            CatsNetworkResponse.SUCCESS,
            CatsNetworkResponse.REFUSED,
            CatsNetworkResponse.TIMEOUT,
            CatsNetworkResponse.DNS_FAILURE,
            CatsNetworkResponse.TLS_FAILURE,
            CatsNetworkResponse.FAILURE,
        }
    ),
    "tcp": frozenset(
        {
            CatsNetworkResponse.SUCCESS,
            CatsNetworkResponse.REFUSED,
            CatsNetworkResponse.TIMEOUT,
            CatsNetworkResponse.DNS_FAILURE,
            CatsNetworkResponse.FAILURE,
        }
    ),
    "udp": frozenset({CatsNetworkResponse.SENT, CatsNetworkResponse.DNS_FAILURE, CatsNetworkResponse.FAILURE}),
}
# A plaintext `GET /` to a named host on one of these ports draws a TLS alert or a close, never an
# HTTP reply. A CATS listener speaks plain HTTP on any configured port, and an example names one by
# expression (`.ec2_dns_name`) or placeholder, never by a string literal.
_TLS_PORTS = frozenset({443, 8443})


def _yaml_blocks() -> list[tuple[str, int, str]]:
    blocks: list[tuple[str, int, str]] = []
    for name in PromptName:
        for index, block in enumerate(_YAML_BLOCK.findall(get_prompt(name))):
            blocks.append((name.value, index, block))
    return blocks


@pytest.mark.parametrize(("prompt_name", "index", "block"), _yaml_blocks(), ids=lambda v: str(v))
def test_prompt_yaml_block_is_a_valid_config_fragment(prompt_name: str, index: int, block: str) -> None:
    # act
    data = yaml.safe_load(block)

    # assert - every block is a dict of real top-level config keys that validates
    assert isinstance(data, dict), f"{prompt_name} yaml block {index} is not a mapping"
    unknown_keys = set(data) - set(CatsConfig.model_fields)
    assert unknown_keys == set(), f"{prompt_name} yaml block {index} uses keys that are not in CatsConfig: {sorted(unknown_keys)}"
    CatsConfig.model_validate(data)


def test_prompts_contain_yaml_blocks_at_all() -> None:
    """If the fence syntax changes, the parametrized test must not silently become empty."""
    # act
    count = len(_yaml_blocks())

    # assert
    assert count >= 10


class NetworkAssertion(NamedTuple):
    """One `assert x == CatsNetworkResponse.M` where `x` was bound to a `*_call(...)` in the same function."""

    prompt_name: str
    block: int
    function: str
    method: str
    call: ast.Call
    asserted: CatsNetworkResponse

    def describe(self) -> str:
        return f"{self.prompt_name} python block {self.block}, {self.function}: {self.method}(...) == {self.asserted.name}"


def _probe_calls(function: ast.FunctionDef) -> dict[str, tuple[str, ast.Call]]:
    """`result = account.private.tcp_call(...)` -> {"result": ("tcp_call", <the call>)}."""
    calls: dict[str, tuple[str, ast.Call]] = {}
    for node in ast.walk(function):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            value = node.value
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) and value.func.attr.endswith("_call"):
                calls[node.targets[0].id] = (value.func.attr, value)
    return calls


def _compared_member(test: ast.expr) -> tuple[str, str] | None:
    """`x == CatsNetworkResponse.M` (either way round) -> ("x", "M"); None for any other assertion."""
    if not (isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)):
        return None
    for name, member in ((test.left, test.comparators[0]), (test.comparators[0], test.left)):
        if isinstance(name, ast.Name) and isinstance(member, ast.Attribute):
            if isinstance(member.value, ast.Name) and member.value.id == CatsNetworkResponse.__name__:
                return name.id, member.attr
    return None


def _network_assertions() -> list[NetworkAssertion]:
    found: list[NetworkAssertion] = []
    for name in PromptName:
        for index, block in enumerate(_PYTHON_BLOCK.findall(get_prompt(name))):
            for function in ast.walk(ast.parse(block)):
                if not isinstance(function, ast.FunctionDef):
                    continue
                calls = _probe_calls(function)
                for statement in ast.walk(function):
                    if not isinstance(statement, ast.Assert):
                        continue
                    compared = _compared_member(statement.test)
                    if compared is not None and compared[0] in calls:
                        method, call = calls[compared[0]]
                        found.append(NetworkAssertion(name.value, index, function.name, method, call, CatsNetworkResponse[compared[1]]))
    return found


def _protocol(method: str) -> str:
    """`ec2_https_call` -> `https`: the probe protocol behind a NetworkHandler method."""
    return method.removesuffix("_call").removeprefix("ec2_").removeprefix("lambda_")


def _argument(call: ast.Call, index: int, name: str) -> ast.expr | None:
    """The expression passed for a parameter, by keyword or by position."""
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    if index < len(call.args):
        return call.args[index]
    return None


def _points_plaintext_http_at_a_tls_port(method: str, call: ast.Call) -> bool:
    host = _argument(call, 0, "host")
    port = _argument(call, 1, "port")
    names_a_host = isinstance(host, ast.Constant) and isinstance(host.value, str)
    tls_port = isinstance(port, ast.Constant) and port.value in _TLS_PORTS
    return _protocol(method) == "http" and names_a_host and tls_port


def _reachable_results(method: str, call: ast.Call) -> frozenset[CatsNetworkResponse]:
    """The results this example's probe can return, given its method and what it is pointed at."""
    results = _RESULTS_BY_PROTOCOL[_protocol(method)]
    if _points_plaintext_http_at_a_tls_port(method, call) is True:
        return results - {CatsNetworkResponse.SUCCESS}
    return results


def test_prompt_network_assertions_are_results_the_probe_can_return() -> None:
    """A test an agent copies must be able to pass: the asserted member must be one the method returns."""
    # act
    unreachable = [
        f"{assertion.describe()} can only be {sorted(r.name for r in _reachable_results(assertion.method, assertion.call))}"
        for assertion in _network_assertions()
        if assertion.asserted not in _reachable_results(assertion.method, assertion.call)
    ]

    # assert
    assert unreachable == []


def _run_http_calls() -> list[tuple[str, str | None, int | None]]:
    """(prompt, host, port) for every `cats run http-call` line in a bash block."""
    calls: list[tuple[str, str | None, int | None]] = []
    for name in PromptName:
        for block in _BASH_BLOCK.findall(get_prompt(name)):
            for options in _RUN_HTTP_CALL.findall(block):
                host = _HOST_OPTION.search(options)
                port = _PORT_OPTION.search(options)
                calls.append((name.value, host.group(1) if host is not None else None, int(port.group(1)) if port is not None else None))
    return calls


def test_prompt_cats_run_http_call_is_not_pointed_at_a_tls_port() -> None:
    """The shell twin of the python rule: a `cats run http-call` demo to a named host on a TLS port can only show FAILURE or REFUSED."""
    # act
    tls_demos = [
        f"{prompt_name}: cats run http-call --host {host} --port {port}"
        for prompt_name, host, port in _run_http_calls()
        if host is not None and host.startswith("<") is False and port in _TLS_PORTS
    ]

    # assert
    assert tls_demos == []


def test_prompts_contain_network_examples_at_all() -> None:
    """If the fence syntax or the example shape changes, the guards above must not silently become empty."""
    # act
    counts = (len(_network_assertions()), len(_run_http_calls()))

    # assert
    assert counts >= (3, 1)
