# MCP

The tools `cats mcp` registers. Each probing tool is the matching [`cats run`](CLI.md#commands)
subcommand over a different transport, and returns the same JSON. The server's own
instructions, sent to the agent on connect, describe the workflow; `cats prompts agentic`
is the longer reference for an agent, and [Agentic](../concepts/agentic.md) is the guide for
the human wiring it up.

## Tools

| Tool | `cats run` | Returns | Hints |
|---|---|---|---|
| `cats_list_accounts` | | every account: `name`, `account_id`, `tags`, declared `targets` | reads config, no AWS call |
| `cats_account_info` | `account-info` | `name`, `account_id`, `tags`, `deployed_version`, `targets` | read-only |
| `cats_http_call` | `http-call` | `result`, `reason`, `http_status` | read-only; sends traffic to the host you name |
| `cats_tcp_call` | `tcp-call` | `result`, `reason`, `http_status` (always `null`) | read-only; sends traffic to the host you name |
| `cats_udp_call` | `udp-call` | `result`, `reason`, `correlation_id` | writes a receipt to S3 when delivered |
| `cats_receive_s3` | `receive-s3` | `received` | read-only |
| `cats_shell` | `shell` | `stdout`, `stderr`, `exit_code`, `duration_ms`, `stdout_truncated`, `stderr_truncated` | destructive: arbitrary commands on a listener; needs `allow_shell: true` at deploy |
| `cats_write_profile` | `write-profile` | `profile_name`, `profile_file`, `role_arn`, `parent` | destructive: rewrites the user's `~/.aws/config` (the only file it writes; the CLI's `--profile-file` has no MCP equivalent) |
| `cats_validate` | `cats validate` | `valid`, `message`, `config_path` | reads config, no AWS call |
| `cats_infra_status` | `cats infra status` | `installed_version`, `accounts` | read-only |

The Hints column is each tool's MCP `ToolAnnotations` in words, so a host can apply its
approval policy. "Sends traffic to the host you name" is the spec's `openWorldHint`: the tool
reaches outside CATS even though it changes no CATS state. `cats_shell` and
`cats_write_profile` are annotated so a host asks before each call.

`result` values for the probes are defined in
[Network Testing](../concepts/network-testing.md#response-values).

## Tool reference

::: cats.cli.mcp
    options:
      show_root_heading: false
      show_root_toc_entry: false
      show_root_full_path: false
      show_docstring_description: false
      show_source: false
      members:
        - list_accounts
        - account_info
        - http_call
        - tcp_call
        - udp_call
        - receive_s3
        - shell
        - write_profile
        - validate_tool
        - infra_status_tool
