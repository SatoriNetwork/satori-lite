# Satori Shadow

An LLM-driven companion for the Satori Neuron — a "shadow" that can read, configure, and act on the neuron, with optional outbound communication to the operator.

## Guiding principle: API-first, MCP-as-pointer

The neuron already exposes a substantial HTTP API (`/api/*`, ~90 routes). That stays the **single source of truth** for every capability we want AIs to use.

The MCP server is a **thin pointer layer** over that API:

- It enumerates / describes the available API endpoints as MCP tools.
- It returns enough information (URLs, auth requirements, schemas, examples) that an AI can call the API directly afterward.
- It only does work itself when it provides something MCP can do that raw HTTP cannot (subscriptions, prompts, capability discovery, sampling, etc.).

Why: AIs that don't speak MCP (mobile apps, scripts, browser extensions, simple cron jobs) still get full functionality through the HTTP API. AIs that do speak MCP get **discovery + the MCP-only extras** on top of the same underlying surface. One canonical surface, two ways to consume it.

## Stages

### Stage 1 — MCP server + AI-callable API

- Audit existing `/api/*` routes, categorize, document machine-readably (OpenAPI-style is the obvious target).
- Add an **auth scheme for non-cookie clients** (long-lived API token, scoped). Today most routes assume a logged-in browser session.
- Fill gaps in the API surface that LLM control needs but doesn't exist yet (see "API gaps" below).
- Build the MCP server. Tools mirror API endpoints 1:1 where useful and provide MCP-only extras where they help.
- Operator wires up an LLM client of choice: BYOK Claude, local Ollama with MCP, Claude Desktop pointing at the neuron, etc.

### Stage 2 — Neuron-initiated communication

Out of scope for Stage 1 but anchored to it: a small in-neuron watchdog detects events and reaches *out* to the operator (alerts, suggestions, anomalies). Watchdog uses the same API it exposes to external AIs. Channels TBD — email, Nostr DM, push, in-app.

## API surface (categorized — from existing routes)

Existing surface, grouped as the AI would see it:

- **Status & monitoring**
  - `GET /api/engine/status`, `GET /api/engine/streams`, `GET /api/system/stats`, `GET /health`
- **Engine config / control**
  - `GET/POST /api/settings/adapter`, `GET/POST /api/engine/training-delay`
- **Wallet (read)**
  - `GET /api/wallet/address`, `GET /api/wallet/balance/direct`, `GET /api/wallet/balance/wallet-only`, `GET /api/balance/get`, `GET /api/wallet/qr/<address>`
- **Wallet (sensitive — gate carefully)**
  - `GET /api/wallet/private-key`, `GET /api/wallet/identity-private-key`, `POST /api/wallet/send`, `POST /api/wallet/send-from-wallet`, `GET /api/wallet/download`
- **Pool / lender**
  - `GET/POST/DELETE /api/pool/...`, `GET/POST/DELETE /api/lender/...`
- **Network / streams**
  - `GET /api/network/subscriptions|observations|publications|classifications|relays`, `GET/POST /api/network/data-source`, `POST /api/network/publish`, `POST/DELETE /api/network/relay`
- **Channels (payment channels)**
  - `GET /api/channels`, `POST /api/channels/open|pay|reclaim|claim`
- **Bounties**
  - `GET /api/bounty/scoring-modules|leaderboard|stats`, `POST /api/bounty|close|leave`, `GET /api/bounties|mine|discover`
- **Access control**
  - `GET /api/access/requests|pending|approved`, `POST /api/access/request|approve|reject|revoke`
- **Nostr / identity**
  - `GET /api/nostr/evr-address`, `POST /api/nostr/normalize-key`
- **Peer**
  - `GET/POST /api/peer/reward-address`

### API gaps to add for Stage 1

These are obvious holes for an LLM operating the neuron:

- `GET /api/config` — read the neuron config (sanitized; secrets redacted).
- `PATCH /api/config` — modify config values with validation.
- `GET /api/logs?since=...&level=...&tail=N` — recent logs, structured.
- `POST /api/restart` (and optionally `POST /api/component/<name>/restart`) — controlled restart.
- `GET /api/version` — neuron version, build, branch.
- `GET /api/events?since=...` — recent significant events (stream stalls, retrains, peer drops). Powers Stage 2 watchdog as well.

### Auth

- Long-lived **API token** generated from the settings UI. Sent as `Authorization: Bearer <token>`.
- Tokens are **scoped** (read-only, config, wallet-sensitive). MCP tool calls inherit the token's scope.
- Existing cookie/session auth continues to work for the browser UI unchanged.

## MCP server design

The MCP server runs locally in/with the neuron (stdio + optionally HTTP/SSE for remote agents). It exposes:

### Tools (mirror API, but smart)

Every API endpoint becomes a tool. For most tools the implementation is "call the underlying HTTP endpoint and return the result," but the tool description includes:

- Purpose, parameter schema, response schema.
- The **direct URL + method** the AI can hit itself for follow-ups (encourages the AI to use the API directly for chatter rather than round-tripping through MCP for every call).
- Required auth scope.

Examples:

- `engine.status()` → wraps `GET /api/engine/status`.
- `engine.set_adapter(name)` → wraps `POST /api/settings/adapter`.
- `streams.list()` → wraps `GET /api/engine/streams`.
- `config.read()` → wraps `GET /api/config`.
- `config.patch(changes)` → wraps `PATCH /api/config`.
- `logs.tail(since, level, n)` → wraps `GET /api/logs`.
- `wallet.balance()` → read-only; available under default scope.
- `wallet.send(...)` → only available if token scope includes wallet-write.

### Resources (MCP-only — live state)

MCP resources let a client subscribe to live data without polling. Candidates:

- `neuron://status` — current health snapshot, updates on change.
- `neuron://logs` — streaming log tail.
- `neuron://events` — significant events as they happen.
- `neuron://streams/{id}` — live per-stream state.

These are the **MCP-only payoff**: things you cannot get cleanly from request/response HTTP.

### Prompts

Pre-built prompt templates the operator can invoke from a client:

- `diagnose` — "what's wrong with my neuron right now"
- `tune-adapter` — "which adapter should I use for stream X"
- `explain-event` — given an event id, walk through what happened

### Discovery endpoint

A single `meta.describe()` tool that returns:

- The full API catalog (endpoint, method, params, scopes, examples).
- The token's current scope.
- Neuron version, available adapters, etc.

So a fresh AI can call one tool and learn the whole surface.

## Sketch — first tools to implement

Concrete, minimum interesting Stage 1 set:

1. `meta.describe`
2. `engine.status`
3. `streams.list`
4. `logs.tail`
5. `config.read`
6. `config.patch`
7. `engine.set_adapter`
8. `events.recent`
9. Resource: `neuron://events`

That's enough to demo "an LLM monitors and tunes the neuron" end to end. Wallet-sensitive tools come later behind explicit scopes.

## Open questions

- Token scopes — how granular? `read`, `config`, `wallet`, `admin` is probably enough.
- Where the MCP server lives — same process as the Flask app, or a sidecar? Sidecar is cleaner for upgrades and crashes; same-process is simpler.
- Transport for remote MCP (operator wants to talk to their home neuron from their laptop) — SSE/HTTP with bearer token? Tunnel via Nostr?
- OpenAPI generation — write the spec by hand, or generate from Flask routes?
- Logging: do we already emit structured logs we can serve from `/api/logs`, or do we need to instrument first?
- Config: is there a single canonical config object, or is config spread across files/env/state? `/api/config` shape depends on the answer.
