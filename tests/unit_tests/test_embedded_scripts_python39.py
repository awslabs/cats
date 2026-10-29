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

"""The scripts embedded into the EC2 listener run on AL2023's system Python 3.9.

`ast.parse(..., feature_version=(3, 9))` rejects syntax newer than 3.9 (match,
parenthesized context managers, ...). PEP 604 `X | Y` annotations are legal on 3.9
only because every file defers them with `from __future__ import annotations`.
"""

import ast
from pathlib import Path

import pytest

EC2_SERVER_DIR = Path(__file__).parents[2] / "cats" / "cfn" / "ec2_server"
EMBEDDED_SCRIPTS = ["probe.py", "udpserver.py", "tcpserver.py"]


@pytest.mark.parametrize("filename", EMBEDDED_SCRIPTS)
def test_embedded_script_parses_as_python_39(filename: str) -> None:
    # arrange
    source = (EC2_SERVER_DIR / filename).read_text()

    # act & assert - raises SyntaxError on 3.10+ syntax
    ast.parse(source, filename=filename, feature_version=(3, 9))


@pytest.mark.parametrize("filename", EMBEDDED_SCRIPTS)
def test_embedded_script_defers_annotations(filename: str) -> None:
    # arrange
    tree = ast.parse((EC2_SERVER_DIR / filename).read_text())
    first_statement = tree.body[0]
    expected = ("__future__", "annotations")

    # act
    result = (
        (first_statement.module, first_statement.names[0].name) if isinstance(first_statement, ast.ImportFrom) else type(first_statement).__name__
    )

    # assert
    assert result == expected


def _function_named(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"no function named {name}")


def _dotted_name(node: ast.expr) -> str:
    if isinstance(node, ast.Attribute):
        return f"{_dotted_name(node.value)}.{node.attr}"
    assert isinstance(node, ast.Name)
    return node.id


def test_attempt_http_catches_the_python_39_read_timeout_type() -> None:
    """On 3.9 a read timeout is socket.timeout, a plain OSError subclass; TimeoutError alone would let it classify as TIMEOUT.

    On 3.10+ the two names are one class, so no runtime test on the test interpreter can tell
    them apart: the except clause has to be pinned by its text.
    """
    # arrange
    attempt_http = _function_named(ast.parse((EC2_SERVER_DIR / "probe.py").read_text()), "_attempt_http")
    expected = {"TimeoutError", "socket.timeout"}

    # act
    caught: set[str] = set()
    for node in ast.walk(attempt_http):
        if isinstance(node, ast.ExceptHandler) and isinstance(node.type, ast.Tuple):
            caught.update(_dotted_name(element) for element in node.type.elts)

    # assert
    assert caught == expected
