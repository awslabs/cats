# Agentic

CATS exposes its deployed infrastructure as stateless probes as well as pytest fixtures. Humans use them from the shell; AI agents use them over the Model Context Protocol (MCP) or the same shell CLI. Every transport drives the same code.

## Three surfaces

`cats test` is for repeatable assertions, suites and compliance reporting. One-shot probes, exploration and "what is deployed here?" have their own surfaces:

| Surface | Consumer | Shape |
|---|---|---|
| `cats test` (pytest plugin) | Humans writing suites, CI | Fixtures, assertions, reports |
| `cats run <subcommand>` | Humans at the shell, non-MCP agents, CI shells | CLI in, JSON out |
| `cats mcp` (MCP server) | MCP-speaking agents (Kiro, Claude Desktop, Cursor) | Tool call in, JSON out |

All three run the same code: assume-role, CloudFormation output parsing, network handlers. A probe from `cats run` or MCP sees what a pytest fixture sees.

## Probe, then codify

The intended flow for agents and humans exploring new infrastructure is:

1. **Probe.** Use `cats run` or MCP tools to ask questions of the live infrastructure. Cheap, stateless, returns JSON.
2. **Codify.** When a probe produces the answer you want, turn that into a pytest assertion in `cats test`. Probes are throwaway; tests are the artifact.

```
cats_list_accounts    ─▶ what's configured?
cats_account_info     ─▶ what's deployed in that account?
cats_http_call / ...  ─▶ does traffic flow the way I expect?
cats_write_profile    ─▶ write a profile the agent's AWS tool / aws CLI assumes the role through
        │
        ▼
pytest test           ─▶ codify what you just proved
```

An agent that can probe verifies every assumption before it writes the assertion.

## When to use which surface

| Scenario | Surface |
|---|---|
| One-off "does port 443 respond from prod to dev?" | `cats run http-call` or `cats_http_call` |
| Agent writing a test and needs to check the target really exists | `cats_account_info` |
| Agent needs its own AWS tool (or the `aws` CLI) to act as the CATS test role | `cats_write_profile` → `aws --profile cats-prod-default ...` |
| Running the same assertion across every account, with reporting | `cats test` with parametrization |
| Hermetic CI probe from a shell | `cats run` with `CATS_CONFIG` env var |

## Connecting an agent

`cats mcp` is an optional extra:

```bash
uv add 'aws-cats[mcp]'    # or: pip install 'aws-cats[mcp]'
```

MCP hosts (Kiro, Claude Desktop, Cursor) take a JSON config describing stdio servers:

```json
{
  "mcpServers": {
    "cats": {
      "command": "cats",
      "args": ["mcp"],
      "env": {
        "CATS_CONFIG": "/absolute/path/to/cats.yaml",
        "AWS_PROFILE": "your-aws-profile",
        "AWS_REGION": "us-east-1"
      }
    }
  }
}
```

`CATS_CONFIG` defaults to `./cats.yaml` where the host launches `cats`; every tool also takes a
`config_path` argument to switch config for one call. Credentials come from boto3's default
chain, whatever `aws sts get-caller-identity` resolves in the same environment.

The server sends its own instructions to the agent on connect. For a longer in-agent reference,
write `cats prompts agentic` to a file the agent loads, for example
`cats prompts agentic > .kiro/steering/cats-agentic.md`. The tool list with its approval hints
is in [MCP](../api-reference/MCP.md).

## Scope and safety

- `cats infra deploy` and `cats infra destroy` are CLI commands only. They run for minutes and their value is the live output; an agent runs them through its shell tool.
- Each call is independent. Credentials and config resolve per invocation; account handles are cached per session and invalidate when `cats.yaml` changes on disk. Restart the server after `cats infra destroy`, which changes stack state without touching the config.
- Cross-account probes are two steps, like `curl`: read the target's DNS name with `cats_account_info`, then call it.
- Tool results are observations. `stdout` and `stderr` from `cats_shell`, a probe's `reason`, `cats_validate`'s `message` and tag values from `cats.yaml` come from hosts and files CATS does not control. The server's instructions tell the agent to report that text and never act on it; a host that supports it should treat tool results as untrusted content.
- `cats_shell` runs arbitrary commands on a listener and `cats_write_profile` writes a profile stanza into the user's AWS config file. Both are annotated so a host asks before each call.

## Acting as the test role

`cats_write_profile` (or `cats run write-profile`) writes an AWS profile that assumes a test role.
The profile names the role and the identity to assume it from, never a credential; the agent's
own AWS tool assumes the role itself and refreshes it:

1. `cats_write_profile(account="prod")` returns `{"profile_name": "cats-prod-default", "profile_file": "/home/you/.aws/config", "role_arn": "...", "parent": "source_profile = ..."}`.
2. The agent calls its AWS tool with that profile: `aws --profile cats-prod-default iam create-user --user-name probe`.
3. AWS answers, `AccessDenied` or otherwise, and the agent reports it.

Profile names must start with `cats-`, so the tool never overwrites a profile it did not create,
and it writes only into an AWS config file. Only the one profile section changes; everything else
is kept byte for byte. The profile does not expire, so there is nothing to re-invoke; `export
AWS_PROFILE=cats-prod-default` is safe, because the profile names its own parent identity.

## Next steps

- `cats prompts overview`, `setup`, `testing`, `agentic`: the in-agent references, one per task.
- [CLI](../api-reference/CLI.md) and [MCP](../api-reference/MCP.md): every command and tool.
- [Writing tests](../guides/writing-tests.md): codifying what a probe proved.
