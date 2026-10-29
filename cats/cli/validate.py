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

"""Config validation for cats.yaml files."""

from pathlib import Path

import yaml
from pydantic import ValidationError

from cats.cfn.render import estimate_template_size, reject_oversized_template
from cats.config import CatsConfig, override_config_path
from cats.exceptions import CatsConfigError
from cats.filepath import get_cats_config_filepath
from cats.utils.yaml_utils import load_yaml

CONFIG_MUST_BE_A_MAPPING = "Config file must be a YAML mapping of top-level sections (cats_deployment_settings, accounts_under_test, ...)"


def validate_config(config_path: Path | None = None, include_input: bool = True) -> tuple[bool, str, Path]:
    """Validate a cats.yaml config file.

    Loads through the Settings source chain under a temporary path override, so the
    result matches exactly what the runtime would load: relative inline_policies paths
    resolve against the file being validated, and no other config merges underneath it.

    A file that loads is then measured: every account's CloudFormation template is rendered
    from placeholders (no AWS) and refused if it would exceed the TemplateBody limit, the
    same refusal `cats infra deploy` makes before it assumes the deploy role.

    ``include_input`` is whether pydantic's report quotes the offending values
    (``input_value=...``). The CLI keeps them: the user is reading their own file. The MCP
    server passes False: its client is an agent acting on untrusted text, and a report that
    echoes values would make the tool a way to read any YAML or JSON file the server can.

    Returns:
        Tuple of (valid, message, config_path)
    """
    path = config_path or get_cats_config_filepath()

    if path.is_dir() is True:
        return False, f"Config path is a directory, expected a cats.yaml file: {path}", path
    if not path.exists():
        return False, f"Config file not found: {path}", path

    try:
        with path.open(encoding="utf-8") as f:
            raw_config = load_yaml(f)
    except yaml.YAMLError as e:
        return False, f"YAML syntax error: {e}", path

    if raw_config is None:
        return False, "Config file is empty", path
    if isinstance(raw_config, dict) is False:
        return False, f"{CONFIG_MUST_BE_A_MAPPING}, got a {type(raw_config).__name__}", path

    try:
        with override_config_path(path):
            config = CatsConfig()
    except ValidationError as e:
        return False, str(e) if include_input is True else _format_without_input(e), path

    try:
        for account_name in config.accounts_under_test:
            reject_oversized_template(account_name, estimate_template_size(config, account_name))
    except CatsConfigError as e:
        return False, str(e), path

    return True, "Valid", path


def _format_without_input(error: ValidationError) -> str:
    """Pydantic's report in its own layout (title, count, one `loc` line and one message line per error) without `input_value`."""
    count = error.error_count()
    lines = [f"{count} validation error{'s' if count != 1 else ''} for {error.title}"]
    for detail in error.errors(include_url=False, include_input=False):
        location = ".".join(str(part) for part in detail["loc"])
        if location != "":
            lines.append(location)
        lines.append(f"  {detail['msg']} [type={detail['type']}]")
    return "\n".join(lines)
