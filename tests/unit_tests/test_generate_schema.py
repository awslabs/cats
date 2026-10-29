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

"""Tests for cats.config.generate_schema."""

import json
from pathlib import Path

from cats.config import generate_schema
from cats.version import __version__


def read_schema(tmp_path: Path) -> dict[str, object]:
    output_path = generate_schema(output_dir=tmp_path)
    with output_path.open() as f:
        loaded: dict[str, object] = json.load(f)
    return loaded


def test_schema_written_to_output_dir(tmp_path: Path) -> None:
    # act
    output_path = generate_schema(output_dir=tmp_path)

    # assert
    expected = tmp_path / ".schema.json"
    assert output_path == expected


def test_schema_id_carries_package_version(tmp_path: Path) -> None:
    # arrange
    expected_id = f"https://awslabs.github.io/cats/schema/v{__version__}.json"

    # act
    schema = read_schema(tmp_path)

    # assert
    assert schema["$id"] == expected_id


def test_schema_declares_2020_12_dialect(tmp_path: Path) -> None:
    # act
    schema = read_schema(tmp_path)

    # assert
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
