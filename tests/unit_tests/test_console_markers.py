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

"""ok_marker / fail_marker / sparkles_marker: unicode glyphs on terminals, ASCII (or nothing) elsewhere.

Non-UTF-8 stdout (LANG=C CI images) raises UnicodeEncodeError on the glyphs,
killing infra commands after the CloudFormation work already succeeded.
"""

from rich.console import Console

from cats.cli.console import fail_marker, ok_marker, sparkles_marker


def _terminal_console() -> Console:
    return Console(force_terminal=True)


def _plain_console() -> Console:
    return Console(force_terminal=False)


def test_ok_marker_on_terminal_returns_glyph() -> None:
    # act
    result = ok_marker(_terminal_console())

    # assert
    assert result == "✓"


def test_ok_marker_without_terminal_returns_ascii() -> None:
    # act
    result = ok_marker(_plain_console())

    # assert
    assert result == "OK"


def test_fail_marker_on_terminal_returns_glyph() -> None:
    # act
    result = fail_marker(_terminal_console())

    # assert
    assert result == "✗"


def test_fail_marker_without_terminal_returns_ascii() -> None:
    # act
    result = fail_marker(_plain_console())

    # assert
    assert result == "FAIL"


def test_sparkles_marker_on_terminal_returns_glyph() -> None:
    # act
    result = sparkles_marker(_terminal_console())

    # assert
    assert result == "✨ "


def test_sparkles_marker_without_terminal_returns_nothing() -> None:
    # act
    result = sparkles_marker(_plain_console())

    # assert
    assert result == ""
