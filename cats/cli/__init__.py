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

"""CATS CLI.

Command registration works via module imports. Each command module (config, testing, agentic)
decorates functions with @cats_cli.command(). Importing the module executes those
decorators, registering the commands. Sub-apps (infra, prompts) are separate Typer
instances added via add_typer().
"""

# Import command modules to execute their @cats_cli.command() decorators
from cats.cli import (
    agentic,
    config,  # noqa: F401
    infra,
    run,
    testing,  # noqa: F401
)
from cats.cli.base import cats_cli

cats_cli.add_typer(infra.infra_app, name="infra", rich_help_panel="Infrastructure")
cats_cli.add_typer(run.run_app, name="run", rich_help_panel="Run")
cats_cli.add_typer(agentic.prompts_app, name="prompts", rich_help_panel="Agentic AI")

__all__ = ["cats_cli"]
