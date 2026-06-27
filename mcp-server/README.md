# Satori Neuron — MCP server

A [Model Context Protocol](https://modelcontextprotocol.io) server that exposes
a running Satori neuron's capabilities to an MCP client (Claude Code, Claude
Desktop, …). It's a **thin wrapper over the neuron's REST API** — the same
capabilities as `/api/skill`, `/api/index`, and `/api/improve/*`, but native to
MCP, so an AI can discover the neuron and drive the self-improvement loop
without raw HTTP.

It runs **wherever your AI runs** and talks to the neuron over HTTP (default
`http://localhost:24601`). It holds no credentials.

## What it exposes

**Tools**
- `get_api_index()` — every REST endpoint the neuron exposes, grouped, plus
  self-improvement metadata (repo, community branch, build commit). Start here.
- `call_endpoint(path, method="GET", body=None)` — call any neuron endpoint to
  answer a question or perform an action.
- `preview_improvement_diff(files=None)` — preview the repo-relative diff (and
  base commit) the neuron will submit for your live edits.
- `submit_improvement(title, description="", files=None, diff="")` — submit an
  improvement upstream; the neuron builds the diff and central opens the PR
  (no GitHub account needed).
- `improvement_status(submission_id)` — status + PR url of a submission.

**Resource** `satori://skill` and **prompt** `self_improve` — the full
self-improvement workflow (the same text as `GET /api/skill`).

## Install & run

```bash
pip install -r requirements.txt

# stdio (for Claude Code / Desktop to launch)
python satori_mcp_server.py

# or as a streamable-HTTP service
SATORI_MCP_TRANSPORT=http SATORI_MCP_PORT=24611 python satori_mcp_server.py
```

Config via environment: `SATORI_NEURON_URL` (default `http://localhost:24601`),
`SATORI_MCP_TRANSPORT` (`stdio` | `http`), `SATORI_MCP_HOST`/`SATORI_MCP_PORT`,
`SATORI_MCP_TIMEOUT`.

## Register with Claude Code

```bash
claude mcp add --transport stdio satori-neuron \
  --env SATORI_NEURON_URL=http://localhost:24601 \
  -- python /absolute/path/to/mcp-server/satori_mcp_server.py
```

Or check a project-scoped `.mcp.json` into your repo:

```json
{
  "mcpServers": {
    "satori-neuron": {
      "type": "stdio",
      "command": "python",
      "args": ["/absolute/path/to/mcp-server/satori_mcp_server.py"],
      "env": { "SATORI_NEURON_URL": "http://localhost:24601" }
    }
  }
}
```

## Register with Claude Desktop

In `claude_desktop_config.json` (macOS:
`~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`):

```json
{
  "mcpServers": {
    "satori-neuron": {
      "command": "python",
      "args": ["/absolute/path/to/mcp-server/satori_mcp_server.py"],
      "env": { "SATORI_NEURON_URL": "http://localhost:24601" }
    }
  }
}
```

> The server source ships inside the neuron image at `/Satori/mcp-server/` too;
> copy it out with `docker cp <container>:/Satori/mcp-server ./mcp-server` if you
> don't have the repo checked out.
