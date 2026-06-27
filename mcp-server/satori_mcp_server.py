#!/usr/bin/env python3
"""
Satori Neuron — MCP server.

A thin Model Context Protocol wrapper over a running neuron's REST API
(default http://localhost:24601). It lets an MCP client (Claude Code, Claude
Desktop, ...) discover what the neuron can do and drive the community
self-improvement loop natively — the same capabilities as the /api/skill,
/api/index, and /api/improve/* HTTP endpoints, exposed as MCP tools, a
resource, and a prompt.

Run over stdio (the default — for Claude Code / Desktop):
    pip install -r requirements.txt
    python satori_mcp_server.py

Run as a streamable-HTTP service instead:
    SATORI_MCP_TRANSPORT=http SATORI_MCP_PORT=24611 python satori_mcp_server.py

Config (environment):
    SATORI_NEURON_URL     neuron base URL (default http://localhost:24601)
    SATORI_MCP_TRANSPORT  stdio (default) | http
    SATORI_MCP_HOST/PORT  bind for http transport (default 0.0.0.0:24611)
    SATORI_MCP_TIMEOUT    per-request timeout seconds (default 30)

IMPORTANT: under stdio transport, stdout carries the JSON-RPC protocol — never
print() to stdout from this process. All logging goes to stderr.
"""
import os
import sys
import logging
from typing import Any, Dict, List, Optional

import requests
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="satori-mcp: %(levelname)s %(message)s")
logger = logging.getLogger("satori-mcp")

NEURON_URL = os.environ.get("SATORI_NEURON_URL", "http://localhost:24601").rstrip("/")
TIMEOUT = float(os.environ.get("SATORI_MCP_TIMEOUT", "30"))

mcp = FastMCP("satori-neuron")


def _url(path: str) -> str:
    return NEURON_URL + (path if path.startswith("/") else "/" + path)


def _request(method: str, path: str,
             body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Call the neuron API; return {ok, status, json|text} or {ok: False, error}."""
    method = (method or "GET").upper()
    try:
        resp = requests.request(
            method, _url(path),
            json=body if body is not None else None,
            timeout=TIMEOUT)
    except requests.RequestException as e:
        logger.warning("request %s %s failed: %s", method, path, e)
        return {"ok": False, "error": f"could not reach neuron at {NEURON_URL}: {e}"}
    out: Dict[str, Any] = {"ok": resp.ok, "status": resp.status_code}
    try:
        out["json"] = resp.json()
    except ValueError:
        out["text"] = resp.text
    return out


def _text(path: str) -> str:
    try:
        return requests.get(_url(path), timeout=TIMEOUT).text
    except requests.RequestException as e:
        return f"(could not reach neuron at {NEURON_URL}: {e})"


@mcp.tool()
def get_api_index() -> Dict[str, Any]:
    """List every REST endpoint this neuron exposes (grouped by category) plus
    its self-improvement metadata (repo, community branch, build commit). Start
    here to discover what the neuron can do."""
    return _request("GET", "/api/index")


@mcp.tool()
def call_endpoint(path: str, method: str = "GET",
                  body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Call any of the neuron's REST endpoints (discover them with
    get_api_index) to answer an operator's question or perform an action. `path`
    is like '/api/engine/streams'; `method` is GET/POST/DELETE; `body` is the
    JSON payload for writes."""
    return _request(method, path, body)


@mcp.tool()
def preview_improvement_diff(files: Optional[List[str]] = None) -> Dict[str, Any]:
    """Preview the repo-relative diff the neuron will submit for your live edits,
    plus the base commit. Optionally scope to specific container file paths."""
    return _request("POST", "/api/improve/diff", {"files": files} if files else {})


@mcp.tool()
def submit_improvement(title: str, description: str = "",
                       files: Optional[List[str]] = None,
                       diff: str = "") -> Dict[str, Any]:
    """Submit an improvement upstream. After editing the neuron's live code, call
    this with a title and description; the neuron builds the repo-relative diff
    and forwards it to central, which opens a pull request — no GitHub account
    needed. Pass `files` (container paths) to scope the auto-diff, or `diff` to
    supply your own unified diff."""
    payload: Dict[str, Any] = {"title": title, "description": description}
    if files:
        payload["files"] = files
    if diff:
        payload["diff"] = diff
    return _request("POST", "/api/improve/submit", payload)


@mcp.tool()
def improvement_status(submission_id: str) -> Dict[str, Any]:
    """Check the status (and pull-request URL, once open) of a submitted
    improvement by its id."""
    return _request("GET", f"/api/improve/status/{submission_id}")


@mcp.resource("satori://skill")
def skill_resource() -> str:
    """The Satori self-improvement skill (Markdown) — the workflow to follow."""
    return _text("/api/skill")


@mcp.prompt()
def self_improve() -> str:
    """Load the Satori neuron self-improvement workflow."""
    return _text("/api/skill")


def main() -> None:
    transport = os.environ.get("SATORI_MCP_TRANSPORT", "stdio").lower()
    logger.info("starting MCP server (transport=%s, neuron=%s)", transport, NEURON_URL)
    if transport in ("http", "streamable-http", "streamable_http"):
        try:
            mcp.settings.host = os.environ.get("SATORI_MCP_HOST", "0.0.0.0")
            mcp.settings.port = int(os.environ.get("SATORI_MCP_PORT", "24611"))
        except Exception as e:  # pragma: no cover - settings shape varies by SDK
            logger.warning("could not set host/port on settings: %s", e)
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
