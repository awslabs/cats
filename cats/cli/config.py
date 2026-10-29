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

import importlib.metadata
import os
from pathlib import Path
from typing import Annotated

import typer
from rich.text import Text

from cats.cli.base import cats_cli
from cats.cli.console import fail_marker, ok_marker, sparkles_marker, stderr_console, stdout_console
from cats.cli.generate_files import create_example_test, pin_pytest_rootdir
from cats.cli.generate_types import generate_types
from cats.cli.validate import validate_config
from cats.config import (
    INIT_PLACEHOLDER_DEPLOY_ROLE_NAME,
    INIT_PLACEHOLDER_PARENT_ACCOUNT_ID,
    Settings,
    click_is_valid_aws_account_id,
    create_default_yaml,
    default_yaml_config,
    generate_schema,
    init_placeholders_left,
    override_config_path,
)
from cats.exceptions import CatsConfigError
from cats.filepath import get_default_output_dir, set_cats_config_filepath


@cats_cli.command("init", rich_help_panel="Config")
def init_repo(
    parent_account_id: Annotated[
        str | None,
        typer.Option(
            callback=click_is_valid_aws_account_id,
            help="Sets cats_deployment_settings.parent_account_id. "
            + "The 12-digit AWS account ID trusted to assume test roles, "
            + "where CATS will execute tests from. "
            + "CATS creates test roles in declared target accounts under test that trust this account. "
            + "Written as the placeholder 123456789012 when omitted.",
        ),
    ] = None,
    deploy_role_name: Annotated[
        str | None,
        typer.Option(
            help="Sets cats_deployment_settings.deploy_role_name. "
            + "The name of an IAM role that already exists in your target accounts, "
            + "which 'cats infra' commands will assume to deploy test infrastructure. "
            + "Common examples: AWSControlTowerExecution, OrganizationAccountAccessRole. "
            + "Written as the placeholder replace-me when omitted.",
        ),
    ] = None,
    aws_region: Annotated[
        str | None,
        typer.Option(
            help="Sets cats_deployment_settings.aws_region. "
            + "The Region CATS deploys its stacks into and connects its test sessions to. "
            + "When omitted, the AWS_REGION environment variable is written if set, else the placeholder us-east-1.",
        ),
    ] = None,
    output_dir: Annotated[
        Path | None,
        typer.Option(
            "--output-dir",
            "-o",
            help="Directory for generated files. Created if missing. Defaults to config file's directory.",
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite an existing cats.yaml."),
    ] = False,
) -> None:
    """Bootstrap a new cats.yaml configuration with other basic files required.

    Safe to re-run: when cats.yaml already exists this is a no-op unless --force is given.
    """
    target_dir = output_dir or get_default_output_dir()
    config_path = target_dir / "cats.yaml"
    overwriting = config_path.exists() is True
    if overwriting is True and force is False:
        stdout_console.print(f"{config_path} already exists, nothing written. Use --force to overwrite.", markup=False)
        return

    # Validate before the first write, mkdir included: a failed init writes nothing.
    config = default_yaml_config(
        parent_account_id=parent_account_id or INIT_PLACEHOLDER_PARENT_ACCOUNT_ID,
        deploy_role_name=deploy_role_name or INIT_PLACEHOLDER_DEPLOY_ROLE_NAME,
        aws_region=aws_region or os.environ.get("AWS_REGION", "us-east-1"),
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    pin_pytest_rootdir(output_dir)
    schema_existed = (target_dir / ".schema.json").exists() is True
    schema_path = generate_schema(output_dir)
    # markup=False: a path is text, and one holding [...] would otherwise be read as Rich markup.
    stdout_console.print(f"{ok_marker(stdout_console)} {_written_verb(schema_existed)} {schema_path}", markup=False)
    written_config = create_default_yaml(config, output_dir)
    stdout_console.print(f"{ok_marker(stdout_console)} {_written_verb(overwriting)} {written_config}", markup=False)
    create_example_test("myaccount", output_dir)
    stdout_console.print(
        f"\n{sparkles_marker()}CATS init complete!\n\n"
        + "Next steps:\n"
        + "  1. Edit cats.yaml to configure your accounts (or run [bold]cats prompts setup[/bold] for AI help)\n"
        + "  2. Run [bold]cats generate-types[/bold] after adding accounts for typed fixtures\n"
        + "  3. Deploy with [bold]cats infra deploy <account>[/bold]\n\n"
        + "Tip: Install a YAML LSP extension (e.g., 'YAML' for VSCode) for schema validation\n"
    )


def _written_verb(existed: bool) -> str:
    """The status line's verb for a file init wrote: a file that was there before the run was replaced, not created."""
    return "Overwrote" if existed is True else "Created"


@cats_cli.command("validate", rich_help_panel="Config")
def validate_cmd(
    config_file: Annotated[
        Path | None,
        typer.Option(
            "--config",
            "-c",
            help="Provide a path to cats.yaml config file. Defaults to CATS_CONFIG env var then ./cats.yaml",
        ),
    ] = None,
    output_dir: Annotated[
        Path | None,
        typer.Option(
            "--output-dir",
            "-o",
            help="Directory for .schema.json output. Defaults to config file's directory.",
        ),
    ] = None,
) -> None:
    """Validate cats.yaml syntax and schema, and measure each account's template against the CloudFormation size limit. Regenerates .schema.json.

    No AWS credentials are used: templates are measured from placeholder IDs, and the measured
    size is the deploy-time size for literal IDs and an upper bound for SSM parameter paths.
    Exit 0 when the file is valid, 2 when it is missing or invalid, or when .schema.json cannot be
    written (the report is on stderr).
    A valid file that still carries a `cats init` placeholder gets a WARN line per placeholder
    on stderr: it validates, and deploy fails at assume_role.
    """
    try:
        with override_config_path(config_file):
            valid, message, config_path = validate_config(config_file)
            if config_path.exists() is False:
                _print_failure(message)
                raise typer.Exit(2)
            try:
                schema_path = generate_schema(output_dir)
            except OSError as e:
                _print_failure(f"Could not write the schema file: {e}")
                raise typer.Exit(2) from None
            placeholders = init_placeholders_left(Settings()) if valid is True else []
    except CatsConfigError as e:
        _print_failure(str(e))
        raise typer.Exit(2) from None
    if valid is True:
        stdout_console.print(f"[green]{ok_marker()}[/green] {config_path} is valid, {schema_path} regenerated", soft_wrap=True)
        for placeholder in placeholders:
            stderr_console.print(Text.assemble(("WARN", "yellow"), " ", placeholder), soft_wrap=True)
    else:
        _print_failure(f"{config_path} is invalid, {schema_path} regenerated\n\n{message}")
        raise typer.Exit(2)


def _print_failure(message: str) -> None:
    """Diagnostics go to stderr, as text: pydantic's report carries [type=...] brackets Rich would read as markup."""
    stderr_console.print(Text.assemble((fail_marker(stderr_console), "red"), " ", message), soft_wrap=True)


@cats_cli.command("generate-types", rich_help_panel="Config")
def generate_types_cmd(
    output_dir: Annotated[
        Path | None,
        typer.Option(
            "--output-dir",
            "-o",
            help="Directory for cats_types.py output. Defaults to config file's directory.",
        ),
    ] = None,
    config_file: Annotated[
        Path | None,
        typer.Option(
            "--config",
            "-c",
            help="Provide a path to cats.yaml config file. Defaults to CATS_CONFIG env var then ./cats.yaml",
        ),
    ] = None,
) -> None:
    """Generate cats_types.py with Protocol classes for typed fixtures.
    Import these types to annotate test parameters for IDE autocomplete and mypy checking.
    """
    if config_file is not None:
        set_cats_config_filepath(config_file)
    output_path = generate_types(output_dir)
    stdout_console.print(f"Generated {output_path}")
    if _boto3_stubs_installed() is False:
        stdout_console.print(
            "For typed boto3 clients (`.client('sts')` autocomplete and mypy), install the stubs extra: pip install 'aws-cats[types]'",
            markup=False,
        )


def _boto3_stubs_installed() -> bool:
    try:
        importlib.metadata.version("boto3-stubs")
    except importlib.metadata.PackageNotFoundError:
        return False
    return True
