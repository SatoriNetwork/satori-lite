# Satori Shadow

Idea: integrate an LLM into the Satori Neuron so it has an intelligent agent attached to it — a "shadow" of the neuron that can reason about it, act on it, and talk to its operator.

## Stage 1 — LLM controls the Neuron

The neuron exposes itself to an LLM that can:

- **Monitor** — read status, logs, predictions, stream health, wallet/stake state, peer connections.
- **Configure** — read and modify the neuron's config.
- **Update / modify** — apply changes to the running neuron (restart components, switch adapters, adjust settings, etc.).

### LLM provider options

The operator chooses how the LLM is wired up:

1. **Bring-your-own API key** — paste a Claude / OpenAI / etc. API key into the neuron UI. The neuron calls out to the hosted model on the user's behalf.
2. **Local model** — for users without an API key or who want to keep it all in-house, route to a local model (Ollama, llama.cpp, LM Studio, etc.).

### Alternative / complementary: MCP server

Instead of (or alongside) the neuron embedding its own LLM client, expose the neuron's capabilities as an **MCP server**. That way *any* MCP-compatible AI — local or remote, Claude Desktop, Claude Code, a custom agent — can drive the neuron through a standard protocol.

This is potentially the cleaner primitive: the neuron just publishes tools (read config, write config, restart, query streams, etc.) and lets the operator point whatever AI they want at it.

Open question: is MCP-only enough, or do we also want a built-in LLM loop so the neuron can act autonomously without an external client driving it?

## Stage 2 — Neuron-initiated communication

Flip the direction: the LLM reaches out to the user when something happens.

- Alerts (stream went stale, prediction quality dropped, wallet event, stake at risk, peer issues, etc.).
- Proactive suggestions ("your ETS adapter is outperforming XGBoost on stream X, want to switch?").
- Channels to be decided — email, push, Nostr DM, Telegram, in-app, etc.

This stage needs the Stage 1 monitoring surface as its substrate.

## To discuss

- Scope of tools the LLM is allowed to call — read-only vs. mutate-config vs. restart vs. wallet/financial actions. Permissions/confirmation model.
- MCP-first vs. embedded-LLM-first vs. both in parallel.
- Local model story — which runtime do we recommend / bundle / detect?
- Where the LLM-driven UI lives — sidebar in the existing web UI, separate page, external client only?
- Cost / rate-limit handling for hosted-API users.
- Security: API keys at rest, scoping of remote MCP access, auth.
- Stage 2 channel(s) and how the user configures them.
