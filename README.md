# knime-mcp

An [MCP](https://modelcontextprotocol.io) server that lets AI assistants such as
Claude Code read, edit and execute workflows in a local
[KNIME Analytics Platform](https://www.knime.com/knime-analytics-platform).

No KNIME Hub or Business Hub is needed. Changes appear live in the KNIME window.

> This is an unofficial project, not affiliated with or endorsed by KNIME AG.
> The live tools use KNIME's internal UI gateway, which has no stability
> guarantee and can change in any release.

Tested on KNIME Analytics Platform 5.12.0 on Linux only.

## How it works

The server has two groups of tools:

- **File tools** parse saved workflows in your workspace. KNIME can be closed.
- **Live tools** connect to a running KNIME through the Chromium DevTools port
  of its embedded browser, then call the same JSON-RPC gateway as the KNIME UI.

Live tools exist because KNIME no longer offers headless batch execution for
new installations from version 5.9 on.

## Requirements

- [uv](https://docs.astral.sh/uv/), which installs the Python dependencies
- KNIME Analytics Platform, started with the debug port for live tools

## Setup

### 1. Get the server

```bash
git clone https://github.com/mathisbukowski/knime-mcp.git
```

### 2. Register it with your MCP client

For Claude Code:

```bash
claude mcp add --scope user knime -- uv run --script /path/to/knime-mcp/server.py
```

For other clients, use `uv` as the command and
`run --script /path/to/knime-mcp/server.py` as the arguments.

### 3. Start KNIME with the debug port

Read [Security](#security) first. Close KNIME, then start it like this:

```bash
JAVA_TOOL_OPTIONS=-Dchromium.remote_debugging_port=9222 /path/to/knime
```

To make this permanent, add this line after `-vmargs` in `knime.ini`:

```
-Dchromium.remote_debugging_port=9222
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `KNIME_WORKSPACE` | `~/knime-workspace` | Workspace read by the file tools |
| `KNIME_DEBUG_PORT` | `9222` | Debug port used by the live tools |

## Tools

### File tools

| Tool | Purpose |
|---|---|
| `list_workflows` | List workflows in the workspace |
| `get_workflow` | Show the nodes, connections and states of a saved workflow |
| `get_node_settings` | Show the saved configuration of a node |

### Live tools

| Tool | Purpose |
|---|---|
| `live_list_projects` | List open workflows |
| `live_create_workflow` | Create and open a workflow in the local space |
| `live_open_workflow` | Open a workflow by workspace-relative path |
| `live_get_workflow` | Show the current state, including unsaved changes |
| `live_search_nodes` | Find installed nodes and their factory class |
| `live_add_node` | Add a node |
| `live_connect_nodes` | Connect two nodes |
| `live_delete` | Delete nodes or connections |
| `live_get_node_config` | Read node settings and their JSON schema |
| `live_set_node_config` | Change node settings |
| `live_execute` | Execute nodes and wait for the result |
| `live_reset` | Reset nodes |
| `live_read_table` | Read rows from an output table |
| `live_save` | Save the workflow to disk |

## Example prompts

- "List my KNIME workflows and explain what the Iris workflow does."
- "Create a workflow with a Data Generator, set it to 500 rows and execute it."
- "Add a DBSCAN node after the distance node and try epsilon values from 0.2 to 0.8."

## Limitations

- **Older node dialogs:** `live_get_node_config` and `live_set_node_config` only
  work on nodes with a web dialog. Nodes with a Swing dialog, such as Numeric
  Binner, return an error and must be configured in KNIME.
- **Table values:** `live_read_table` returns values as KNIME displays them, so
  numbers can be rounded.
- **Local space only:** the live tools do not open workflows from a Hub.
- **Metanodes and components:** pass the node id as `workflow_id` to work inside
  one. This has not been tested.
- **Saved state:** the file tools see the last save, not unsaved changes.

## Security

The debug port has **no authentication**. KNIME binds it to `127.0.0.1`, but any
process on your computer can connect to it while KNIME runs. That process could
control KNIME, read your data, and run code through nodes such as Python Script.

Only enable the port on a computer you trust, and do not forward or expose it.
Do not use this setup on shared machines.

Your MCP client can also change and overwrite workflows. Keep backups of work
that matters, and review tool calls before you approve them.

## Troubleshooting

| Message | Fix |
|---|---|
| `KNIME is not reachable on port 9222` | Start KNIME with the debug port and check `KNIME_DEBUG_PORT` |
| `KNIME UI page not found` | Wait until KNIME has finished starting |
| `Pass project_id` | Several workflows are open: choose one from the list in the message |
| `Only web dialogs can be read or changed` | Configure this node in KNIME |

## License

[MIT](LICENSE)
