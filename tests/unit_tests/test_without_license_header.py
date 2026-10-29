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

"""Tests for cats.cfn.render._without_license_header."""

from cats.cfn.render import _FILE_LICENSE_HEADER, _without_license_header


def test_header_at_top_is_stripped() -> None:
    # arrange
    text = _FILE_LICENSE_HEADER + "\n[Unit]\nDescription=CATS UDP listener\n"

    # act
    result = _without_license_header(text)

    # assert
    expected = "[Unit]\nDescription=CATS UDP listener\n"
    assert expected == result


def test_header_after_shebang_is_stripped() -> None:
    # arrange
    text = "#!/usr/bin/python3\n" + _FILE_LICENSE_HEADER + "\nimport socket\n"

    # act
    result = _without_license_header(text)

    # assert
    expected = "#!/usr/bin/python3\nimport socket\n"
    assert expected == result


def test_text_without_header_is_unchanged() -> None:
    # arrange
    text = "import socket\n\nprint('hello')\n"

    # act
    result = _without_license_header(text)

    # assert
    assert text == result
