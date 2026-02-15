# Satori Nostr Relay

Run a Nostr relay for the Satori Network with NIP-11 identity binding.

## How It Works

This setup runs a [strfry](https://github.com/hoytech/strfry) Nostr relay behind nginx. Nginx serves a NIP-11 document with the `self` field set to your neuron's Nostr public key, binding the relay to your neuron identity.

```
Central Server ──fetch NIP-11──→ nginx ──→ nip11.json (self = your nostr pubkey)
Nostr Clients  ──WebSocket───→ nginx ──→ strfry relay
```

## Setup

1. Copy the example environment file:

```bash
cp .env.example .env
```

2. Set your neuron's Nostr public key in `.env`:

```
NOSTR_PUBKEY=a1b2c3d4...  # 64 lowercase hex characters
```

3. Start the relay:

```bash
docker compose up -d
```

4. Verify NIP-11 is served correctly:

```bash
curl -H "Accept: application/nostr+json" http://localhost:7777
```

You should see:

```json
{
  "name": "Satori Relay",
  "description": "A Satori Network relay",
  "self": "a1b2c3d4...",
  ...
}
```

5. Register your relay URL with the Satori central server via the neuron UI.

## Reward Eligibility

A relay is reward-eligible when:

> The relay's NIP-11 `self` field equals the Nostr public key associated with your neuron.

This binds: **Relay → NIP-11 self → Nostr Public Key → Neuron (via Identity Wallet)**

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `NOSTR_PUBKEY` | Yes | Your neuron's Nostr public key (64 hex chars) |
| `RELAY_NAME` | No | Relay display name (default: "Satori Relay") |
| `RELAY_DESCRIPTION` | No | Relay description |
| `RELAY_CONTACT` | No | Admin contact (email or URL) |
| `RELAY_PORT` | No | Port to expose (default: 7777) |

## Production Deployment

For production, put a reverse proxy (Caddy, nginx, etc.) in front to handle SSL:

```
Internet → Caddy (SSL termination) → this relay (port 7777)
```

Your relay URL for registration would be `wss://your-domain.com`.

## Architecture

```
docker compose
├── init       (generates nip11.json from NOSTR_PUBKEY, runs once)
├── strfry     (Nostr relay, port 7777 internal)
└── nginx      (reverse proxy, serves NIP-11 + proxies WebSocket)
```
