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

import io
from typing import IO, Any

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from cats.filepath import get_cats_config_filepath


class UniqueKeyLoader(yaml.SafeLoader):
    """A SafeLoader that refuses a mapping with a repeated key.

    PyYAML keeps the last value silently, so a second ``accounts_under_test:`` block would drop
    accounts from every piece of evidence with exit 0. In a security tool the second block is an
    error, not an override.
    """

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate key {key!r}; a repeated key would silently replace the first",
                    key_node.start_mark,
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def load_yaml(source: str | IO[str]) -> Any:  # noqa: ANN401 - YAML documents are arbitrary
    """``yaml.safe_load`` with duplicate keys rejected: the one YAML reader for cats.yaml.

    Pass the open file rather than its text where possible: PyYAML then names the file in its
    error marks instead of ``<unicode string>``.
    """
    return yaml.load(source, Loader=UniqueKeyLoader)  # noqa: S506 - UniqueKeyLoader is a SafeLoader


def dump_yaml(content: dict[str, Any], file: io.TextIOWrapper) -> None:
    yaml.dump(content, file, default_flow_style=False, sort_keys=False)


def load_config() -> dict[str, Any]:
    filepath = get_cats_config_filepath()
    if not filepath.exists():
        raise FileNotFoundError(f"No {filepath} found - run 'cats init' to get started")
    with filepath.open(encoding="utf-8") as f:
        result: dict[str, Any] = load_yaml(f)
    return result
