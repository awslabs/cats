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

"""Every MCP tool declares annotations, so a host can prompt for the right ones.

cats_shell runs arbitrary commands as root on a listener and cats_write_profile writes a
stanza into the user's AWS config file; a host that auto-approves read-only tools must be
able to tell those two apart from the probes and the config reads.
"""

import asyncio

from mcp.types import ToolAnnotations

from cats.cli.mcp import mcp


def _annotations() -> dict[str, ToolAnnotations | None]:
    return {tool.name: tool.annotations for tool in asyncio.run(mcp.list_tools())}


def test_every_tool_carries_the_expected_annotations() -> None:
    # act
    hints = {name: (a.readOnlyHint, a.destructiveHint, a.idempotentHint, a.openWorldHint) for name, a in _annotations().items() if a is not None}

    # assert - each value reads readOnly, destructive, idempotent, openWorld
    expected = {
        "cats_list_accounts": (True, False, True, False),
        "cats_account_info": (True, False, True, False),
        "cats_validate": (True, False, True, False),
        "cats_infra_status": (True, False, True, False),
        "cats_receive_s3": (True, False, True, False),
        "cats_http_call": (True, False, True, True),
        "cats_tcp_call": (True, False, True, True),
        "cats_udp_call": (False, False, False, True),
        "cats_shell": (False, True, False, True),
        "cats_write_profile": (False, True, True, False),
    }
    assert hints == expected


def test_no_tool_is_left_unannotated() -> None:
    # act
    unannotated = sorted(name for name, a in _annotations().items() if a is None)

    # assert
    assert unannotated == []
