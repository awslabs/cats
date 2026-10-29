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

import os
from pathlib import Path

from cats.exceptions import CatsConfigError

DEFAULT_FILEPATH = Path("./cats.yaml")
_override_filepath: Path | None = None


def require_config_file(path: Path) -> None:
    """Raise CatsConfigError unless ``path`` is a file, naming a directory as such: 'not found' would send the user after a typo."""
    if path.is_dir() is True:
        raise CatsConfigError(f"Config path is a directory, expected a cats.yaml file: {path}")
    if path.is_file() is False:
        raise CatsConfigError(f"Config file not found: {path}")


def set_cats_config_filepath(path: Path | None) -> None:
    """Set config path programmatically. Takes precedence over env var. None clears it.

    Raises:
        CatsConfigError: ``path`` is not a file. Loading an absent file would yield an empty
            configuration and silently wrong output (no accounts, empty types, no fixtures).
    """
    global _override_filepath
    if path is not None:
        require_config_file(path)
    _override_filepath = path


def get_cats_config_override() -> Path | None:
    """The raw programmatic override, without the env var / default fallback."""
    return _override_filepath


def get_cats_config_filepath() -> Path:
    """The programmatic override, else ``CATS_CONFIG`` (with ``~`` expanded, for a value the shell quoted), else ./cats.yaml."""
    if _override_filepath is not None:
        return _override_filepath
    alt_filepath = os.environ.get("CATS_CONFIG", None)
    return Path(alt_filepath).expanduser() if alt_filepath else DEFAULT_FILEPATH


def get_default_output_dir() -> Path:
    """Return the directory containing cats.yaml (default for all file outputs)."""
    return get_cats_config_filepath().parent
