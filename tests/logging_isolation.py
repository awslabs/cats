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

"""Root-logger isolation for tests that run CLI commands in-process.

This module must import nothing from `cats`. It is imported at conftest-import time,
and importing `cats.cli` runs `base.py`'s `logging.basicConfig` while the root logger
is still empty, which installs a stderr `RichHandler` for the whole session - the very
leak this module exists to contain.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def preserve_root_logger() -> Iterator[None]:
    """Restore the root logger's handlers and level on exit, whatever the block did to them.

    `cats.cli.mcp.main()` assigns `logging.getLogger().handlers` wholesale, because stdout
    is the MCP protocol channel and every log line has to be forced to stderr. Run in a
    test, that handler outlives the test: its Rich `Console` resolves `sys.stderr` on each
    write, so it lands inside every later `CliRunner` capture as an `INFO Session used in
    initial account` line and fails the exact-output snapshots.

    Wrap a whole test *protocol* in this, not a single phase: at protocol boundaries none
    of pytest's own per-phase capture handlers are attached, so restoring the snapshot
    cannot re-attach a phase handler pytest has already finished with.
    """
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    try:
        yield
    finally:
        root.handlers = handlers
        root.setLevel(level)
