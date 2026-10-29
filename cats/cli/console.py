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

from rich.console import Console

# Within the CATS cli we need to track state on the terminal - these Console objects
# enable this and can be shared whenever we want to print to an active CLI state
# We don't need to use them in tests or constructs as they're printing into pytest
# rather than directly to our CLI and prefer basic handling.

stdout_console = Console()
stderr_console = Console(stderr=True)


def ok_marker(console: Console = stdout_console) -> str:
    """`✓` on a terminal, ASCII `OK` otherwise - non-UTF-8 stdout (LANG=C CI images)
    raises UnicodeEncodeError on the glyph, killing commands after the work succeeded."""
    return "✓" if console.is_terminal else "OK"


def fail_marker(console: Console = stdout_console) -> str:
    """`✗` on a terminal, ASCII `FAIL` otherwise - see ok_marker."""
    return "✗" if console.is_terminal else "FAIL"


def sparkles_marker(console: Console = stdout_console) -> str:
    """`✨ ` on a terminal, nothing otherwise - see ok_marker. Decoration, so it has no ASCII stand-in."""
    return "✨ " if console.is_terminal else ""
