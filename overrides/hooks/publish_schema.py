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

"""mkdocs hook: publish the cats.yaml JSON schema at the URL its `$id` names.

`cats validate` writes `.schema.json` with `$id` set to
`https://awslabs.github.io/cats/schema/v{version}.json`. An `$id` is a resolvable
identifier by convention, so the docs build writes the same document to
`site/schema/v{version}.json`, plus `site/schema/latest.json` for editors that want
to track the current release rather than pin one.
"""

from pathlib import Path
from typing import Any

from cats.config import generate_schema
from cats.version import __version__


def on_post_build(config: Any, **kwargs: Any) -> None:  # noqa: ANN401 - mkdocs passes its own config object
    schema_dir = Path(config["site_dir"]) / "schema"
    schema_dir.mkdir(parents=True, exist_ok=True)
    written = generate_schema(output_dir=schema_dir)
    versioned = schema_dir / f"v{__version__}.json"
    written.replace(versioned)
    (schema_dir / "latest.json").write_bytes(versioned.read_bytes())
