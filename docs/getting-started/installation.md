# Installation

## Prerequisites

- **Python 3.11+** - CATS requires Python 3.11 or later
- **AWS credentials** - Access to an AWS account where you can deploy CloudFormation stacks

## Install CATS

CATS is published as `aws-cats` on [PyPI](https://pypi.org/project/aws-cats/).
Add it to your project with uv (recommended). `uv add` needs a `pyproject.toml`; in a
new directory, `uv init` writes one first:

```bash
uv init          # new directory only
uv add aws-cats
```

Or install with pip:

```bash
pip install aws-cats
```

For the CLI alone, without a project (for example to run `cats infra` from a pipeline
image), `uv tool install aws-cats` puts `cats` on the PATH, and `uvx --from aws-cats cats`
runs it without installing.

> The distribution name is `aws-cats`; the import package (`import cats`) and
> the `cats` CLI command are both `cats`.

## Optional extras

```bash
uv add 'aws-cats[mcp]'      # the `cats mcp` MCP server for AI agents
uv add 'aws-cats[types]'    # typed boto3 clients for IDE autocomplete and mypy
```

The core install covers every other command. `[types]` is ~119 MB of type
stubs; install it where you type-check tests or generated `cats_types.py`.

## Verify Installation

```bash
uv run cats --version     # or plain `cats --version` in an activated environment
```

You should see the installed version, for example `1.0.0`.

## Optional: YAML Schema Support

CATS provides a JSON schema for `cats.yaml` validation. To enable autocomplete and validation in your editor:

**VS Code**: Install the [YAML extension](https://marketplace.visualstudio.com/items?itemName=redhat.vscode-yaml), then add this line to the top of your `cats.yaml`:

```yaml
# yaml-language-server: $schema=./.schema.json
```

Run `cats validate` to generate the schema file.

## Next Steps

Continue to the [Quickstart](quickstart.md) to run your first test.
