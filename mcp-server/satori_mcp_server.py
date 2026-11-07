#!/usr/bin/env python3
"""
Satori Neuron — MCP server.

A Model Context Protocol interface to a running neuron's REST API (default
http://localhost:24601). It lets an MCP client (Claude Code, Claude Desktop,
...) navigate and drive the neuron's ENTIRE functionality — balances, streams,
predictions, bounties, pools, lending, relays, staking, settings — as well as
the community self-improvement loop, exposed as MCP tools, a resource, and a
prompt. It discovers endpoints from /api/index and directs the agent to the
right one for each task.

Auth: most neuron endpoints require an unlocked session. The operator unlocks
the neuron in their browser (where the wallet is decrypted) and passes the
session cookie via SATORI_NEURON_COOKIE — this server never sees or handles the
vault password.

Security: endpoints that reveal private key material (wallet / vault /
identity-nostr private keys, the wallet-file download) are hard-blocked here and
hidden from the index, even with a valid session. We relay everything else.

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
import json
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="satori-mcp: %(levelname)s %(message)s")
logger = logging.getLogger("satori-mcp")

NEURON_URL = os.environ.get("SATORI_NEURON_URL", "http://localhost:24601").rstrip("/")
TIMEOUT = float(os.environ.get("SATORI_MCP_TIMEOUT", "30"))

# Sensitive paths the MCP layer must NEVER expose, even to an authenticated
# session — they reveal key material (wallet / vault / identity-nostr private
# keys, the wallet file). The operator can still view these directly in the
# neuron UI after unlocking; we just refuse to relay them to an AI. Extend with
# SATORI_MCP_BLOCK (comma-separated substrings).
_BLOCK_PATTERNS = [
    "private-key", "private_key", "privatekey", "privkey",
    "identity-private", "mnemonic", "/wallet/download",
]
_BLOCK_PATTERNS += [p.strip().lower() for p in
                    os.environ.get("SATORI_MCP_BLOCK", "").split(",") if p.strip()]


def _build_session() -> requests.Session:
    """HTTP session carrying any operator-provided neuron auth.

    Most neuron endpoints require an unlocked session. The operator unlocks the
    neuron in their browser (decrypting the wallet there) and passes the session
    cookie via SATORI_NEURON_COOKIE — this MCP server never sees or handles the
    vault password.
    """
    s = requests.Session()
    cookie = os.environ.get("SATORI_NEURON_COOKIE", "").strip()
    if cookie:
        s.headers["Cookie"] = cookie
    extra = os.environ.get("SATORI_NEURON_HEADERS", "").strip()
    if extra:
        try:
            s.headers.update(json.loads(extra))
        except ValueError:
            logger.warning("SATORI_NEURON_HEADERS is not valid JSON; ignoring")
    return s


_http = _build_session()

mcp = FastMCP("satori-neuron")


def _url(path: str) -> str:
    return NEURON_URL + (path if path.startswith("/") else "/" + path)


def _blocked(path: str) -> Optional[str]:
    low = (path or "").lower()
    for pat in _BLOCK_PATTERNS:
        if pat and pat in low:
            return pat
    return None


def _request(method: str, path: str,
             body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Call the neuron API; return {ok, status, json|text} or {ok: False, error}."""
    if _blocked(path):
        return {"ok": False, "blocked": True,
                "error": (f"'{path}' exposes private key material and is blocked "
                          "via MCP for safety. The operator can view it directly "
                          "in the neuron UI.")}
    method = (method or "GET").upper()
    try:
        resp = _http.request(
            method, _url(path),
            json=body if body is not None else None,
            timeout=TIMEOUT, allow_redirects=False)
    except requests.RequestException as e:
        logger.warning("request %s %s failed: %s", method, path, e)
        return {"ok": False, "error": f"could not reach neuron at {NEURON_URL}: {e}"}
    # Session-gated endpoints redirect to /login (or /vault-setup) when the
    # neuron isn't unlocked — surface that clearly instead of returning HTML.
    if resp.status_code in (301, 302, 303, 307, 308):
        loc = resp.headers.get("Location", "")
        if "login" in loc or "vault" in loc:
            return {"ok": False, "status": resp.status_code, "auth_required": True,
                    "error": ("This endpoint needs an unlocked neuron session. "
                              "Unlock the neuron in the browser, then set "
                              "SATORI_NEURON_COOKIE to that session cookie.")}
        return {"ok": False, "status": resp.status_code,
                "error": f"unexpected redirect to {loc}"}
    out: Dict[str, Any] = {"ok": resp.ok, "status": resp.status_code}
    try:
        out["json"] = resp.json()
    except ValueError:
        out["text"] = resp.text
    return out


def _text(path: str) -> str:
    try:
        return _http.get(_url(path), timeout=TIMEOUT, allow_redirects=False).text
    except requests.RequestException as e:
        return f"(could not reach neuron at {NEURON_URL}: {e})"


def _filter_index(index: Dict[str, Any]) -> Dict[str, Any]:
    """Remove sensitive endpoints from an index payload so the agent is never
    pointed at them; record what was hidden for transparency."""
    eps = index.get("endpoints")
    if isinstance(eps, list):
        kept, hidden = [], []
        for e in eps:
            path = e.get("path", "") if isinstance(e, dict) else ""
            if _blocked(path):
                hidden.append(path)
            else:
                kept.append(e)
        index["endpoints"] = kept
        index["endpoint_count"] = len(kept)
        if hidden:
            index["hidden_for_safety"] = hidden
            index["hidden_note"] = ("Endpoints exposing private key material are "
                                    "hidden from MCP; the operator can use them in "
                                    "the neuron UI.")
    cats = index.get("categories")
    if isinstance(cats, dict):
        index["categories"] = {c: [p for p in paths if not _blocked(p)]
                               for c, paths in cats.items()}
    return index


@mcp.tool()
def get_api_index() -> Dict[str, Any]:
    """Map of the neuron's ENTIRE functionality: every REST endpoint grouped by
    category (wallet, engine, network, bounty, pool, lending, relay, ...) with
    descriptions, plus self-improvement metadata (repo, community branch, build
    commit). Start here to discover and navigate what the neuron can do.
    Key-exposing endpoints are hidden for safety."""
    r = _request("GET", "/api/index")
    if r.get("ok") and isinstance(r.get("json"), dict):
        r["json"] = _filter_index(r["json"])
    return r


@mcp.tool()
def find_endpoints(query: str) -> Dict[str, Any]:
    """Find the neuron endpoints relevant to a task or feature — searches paths,
    categories, and descriptions. Use this to navigate the neuron's full
    functionality, e.g. 'balance', 'send', 'bounty', 'stream', 'relay', 'pool',
    'stake', 'delegate'. Returns the matching endpoints to call with
    call_endpoint."""
    r = _request("GET", "/api/index")
    if not r.get("ok") or not isinstance(r.get("json"), dict):
        return r
    q = (query or "").lower()
    matches = []
    for e in r["json"].get("endpoints", []):
        if not isinstance(e, dict) or _blocked(e.get("path", "")):
            continue
        hay = " ".join(str(e.get(k, "")) for k in
                       ("path", "category", "description", "name")).lower()
        if q in hay:
            matches.append(e)
    return {"ok": True, "query": query, "count": len(matches), "endpoints": matches}


@mcp.tool()
def call_endpoint(path: str, method: str = "GET",
                  body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Drive ANY of the neuron's features by calling its REST endpoints (find
    them with get_api_index / find_endpoints): balances, streams, predictions,
    bounties, pools, lending, relays, staking, settings, and more. `path` is like
    '/api/engine/streams'; `method` is GET/POST/DELETE; `body` is the JSON for
    writes. Most endpoints need an unlocked neuron session (see auth note in the
    README); endpoints that reveal private keys are blocked for safety."""
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


@mcp.tool()
def search_improvements(query: str = "", repo: str = "") -> Dict[str, Any]:
    """REUSE BEFORE YOU BUILD. Search existing OPEN proposals (unmerged PRs from
    the community) that may already solve the operator's need, ordered by how many
    operators have adopted them. Always check here before writing a new change.
    Filter by `query` (matches title/description/files) and/or `repo`."""
    params = []
    if query:
        params.append("q=" + quote(query))
    if repo:
        params.append("repo=" + quote(repo))
    path = "/api/improve/open" + ("?" + "&".join(params) if params else "")
    return _request("GET", path)


@mcp.tool()
def get_proposal(proposal_id: str) -> Dict[str, Any]:
    """Full detail of an open proposal — its diff, files, PR url, and adopter
    count — so you can evaluate whether to reuse it instead of building anew."""
    return _request("GET", f"/api/improve/proposal/{proposal_id}")


@mcp.tool()
def adopt_improvement(proposal_id: str) -> Dict[str, Any]:
    """Record that you applied an existing open proposal on this neuron locally.
    Call this after applying the proposal's diff (so the operator gets it now,
    before merge). It bumps the proposal's distinct-adopter count — the signal
    that tells maintainers which unmerged proposals are broadly useful."""
    return _request("POST", f"/api/improve/adopt/{proposal_id}")


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
