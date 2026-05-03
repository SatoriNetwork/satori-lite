# Nostr Relay Reward Migration

**Date:** 2026-04-18
**Status:** Code complete (all repos on `nostr-address` branch), pending deployment


## Summary

Migrate relay rewards from the current neuron-claimed model to a system where
relays are rewarded directly at Evrmore addresses derived from their Nostr
public keys. This eliminates the requirement for a relay to be associated with
a neuron, decentralizes relay discovery via NIP-65, and enables any Nostr relay
operator to earn SATORI without running a full neuron.


## Current State

Today, relay rewards work like this:

1. A neuron operator claims a relay through the neuron UI.
2. The neuron reports its claimed relay to the central server.
3. The central server aggregates a relay list from these reports.
4. Rewards are sent to the neuron operator's Evrmore wallet.

This creates a hard dependency: a relay can only earn rewards if a neuron
operator claims it. The relay operator may not be the neuron operator.
Unclaimed relays carrying Satori traffic receive nothing.


## Target State

1. Relays are rewarded directly — no neuron claim required.
2. The reward address is derived from the relay's own Nostr public key.
3. Relay discovery is decentralized via NIP-65 (neurons publish their relay
   lists as Nostr events).
4. The central server monitors discovered relays, measures Satori traffic,
   and pays accordingly.
5. Relay operators import their Nostr private key into an Evrmore wallet
   (with even-y normalization) to spend their rewards.


## Key Concepts

### Nostr-to-Evrmore Address Derivation

Nostr and Evrmore both use the secp256k1 curve. A Nostr public key is a
32-byte x-only coordinate. An Evrmore compressed public key is 33 bytes:
a `02` (even y) or `03` (odd y) prefix followed by the 32-byte x-coordinate.

For any x-coordinate on the curve, two valid points exist — one with even y,
one with odd y. They produce different Evrmore addresses. Since the Nostr
public key does not reveal which y-parity the underlying private key produces,
we adopt a convention:

**Always derive the even-y (0x02) address.**

```
evrmore_pubkey = 0x02 || nostr_pubkey_hex
evrmore_address = base58check(version=0x21, hash160(evrmore_pubkey))
```

This is deterministic and one-way from the Nostr pubkey. The server (or
anyone) can compute the reward address from the relay's Nostr pubkey alone.

### Even-Y Normalization for the Relay Operator

About half of all secp256k1 private keys produce a point with odd y. If a
relay operator's Nostr private key `d` produces a point where y is odd, the
Evrmore wallet would derive the `03` address — different from the `02` address
the server sent rewards to.

The fix is simple: the operator uses `n - d` (where `n` is the secp256k1
curve order) instead of `d`. This negated key produces the same x-coordinate
but with even y, so the Evrmore address matches.

```
n = FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141

Given private key d:
  Compute P = d * G
  If P.y is odd:
    Use (n - d) as the Evrmore private key instead
  The resulting address matches the server's 0x02 derivation
```

Both `d` and `n - d` are valid private keys. The operator does not lose any
security. This is consistent with BIP-340 (Schnorr), which Nostr already uses
and which defines the public key as the even-y point.

### Key Normalization Tool

A tool must be provided (likely in the neuron UI, or as a standalone utility)
that allows relay operators to:

1. Input their Nostr private key (hex or nsec format).
2. Automatically check if the key produces an odd-y public key.
3. If odd, compute the normalized key `n - d`.
4. Output the normalized private key in Evrmore WIF format, ready for import
   into any Evrmore-compatible wallet.
5. Display the resulting Evrmore address so the operator can verify it matches
   the address the server would derive from their Nostr pubkey.

This could live in:
- The neuron web UI (a "Relay Rewards" settings page)
- A standalone CLI tool
- The Satori app (Mantra)


## Migration Steps

### 1. Implement NIP-65 Relay List Publishing in Neurons

Neurons already know which relays they connect to. Add a step where, after
establishing relay connections, the neuron publishes a kind 10002 (NIP-65)
event listing its relays.

```
Event kind: 10002
Content: (empty)
Tags:
  ["r", "wss://relay-one.example.com", "read"]
  ["r", "wss://relay-one.example.com", "write"]
  ["r", "wss://relay-two.example.com", "read"]
  ...
```

This event is a replaceable event (NIP-16) — each new publication overwrites
the previous one. The neuron publishes an updated list whenever its relay
connections change (e.g., during the reconciliation loop).

**Where to publish:** The neuron publishes its NIP-65 event to all relays it
is currently connected to. This bootstraps discoverability — any relay
carrying Satori traffic will also carry the relay list events that reference
it.

**Neuron UI addition:** Add a section in the relay settings page where
operators can manually add relay URLs to connect to. Currently the neuron
discovers relays from the central server's list. This gives operators the
ability to add relays the server doesn't know about yet, which then get
published via NIP-65 and eventually discovered by the server.

### 2. Central Server Subscribes to NIP-65 Events

The central server subscribes to kind 10002 events from all known neuron
Nostr pubkeys. It already maintains the list of registered neuron pubkeys.

The server aggregates relay URLs from these events to build its relay
directory. This replaces the current mechanism where neurons report claimed
relays via API calls.

As new neurons register and publish NIP-65 events, new relays appear in the
directory automatically. As neurons go offline or stop using a relay, the
relay disappears from NIP-65 events and eventually from the directory.

### 3. Remove the Neuron-Relay Claim Requirement

Once NIP-65-based discovery is working:

- Remove the relay claim flow from the neuron UI.
- Remove the relay claim API from the central server.
- The server no longer requires a neuron to vouch for a relay.
- The relay list endpoint (`getRelays`) now returns the NIP-65-aggregated
  list instead of the claim-aggregated list.

### 4. Traffic Monitoring and Verification

The central server monitors each discovered relay to measure Satori traffic:

1. Subscribe to Satori event kinds (34600-34608) on each relay.
2. For each event received, verify the event signature is from a known
   registered neuron Nostr pubkey.
3. Only count events from verified pubkeys as legitimate Satori traffic.
   Anyone can publish events with Satori event kinds, but only events signed
   by registered neurons are real.
4. Track traffic volume per relay over a reward period.

This gives the server a verified traffic count per relay without trusting the
relay operator or requiring any self-reporting.

### 5. Reward Distribution

At each reward interval:

1. For each relay in the directory, look up its Nostr public key (available
   from the relay's NIP-11 information document, or from the relay's event
   signatures).
2. Derive the Evrmore address: `base58check(0x21, hash160(0x02 || pubkey))`.
3. Distribute SATORI to each relay's derived address proportional to its
   verified traffic volume.

The relay operator can claim their rewards at any time by importing their
(possibly normalized) Nostr private key into an Evrmore wallet.

### 6. Build the Key Normalization Tool

Provide the tool described above so relay operators can convert their Nostr
private key to an Evrmore-compatible private key that controls the even-y
address. This is the only step the relay operator needs to take to access
their rewards.


## Discovery Flow (End State)

```
Neuron connects to relays
    |
    v
Neuron publishes NIP-65 event (kind 10002) listing its relays
    |
    v
Central server subscribes to kind 10002 from known neuron pubkeys
    |
    v
Server builds relay directory from aggregated NIP-65 events
    |
    v
Server monitors each relay for Satori traffic (kinds 34600-34608)
    |
    v
Server verifies traffic signatures against known neuron pubkeys
    |
    v
Server derives Evrmore address from relay's Nostr pubkey (0x02 + x)
    |
    v
Server sends SATORI rewards proportional to verified traffic
    |
    v
Relay operator imports normalized Nostr key into Evrmore wallet to spend
```


## Implementation Summary

All code is on the `nostr-address` branch across three repos.

### neuron (satori-lite)
- NIP-65 relay list publishing (kind 10002) in reconciliation loop
- Custom relay management UI/API (Settings > External Relay Connections)
- Nostr-derived Evrmore address display (Settings)
- Key normalization tool (Settings)
- Removed old relay claim UI, endpoint, and JS

### satorilib
- `publish_relay_list()` method on SatoriNostr client
- Removed `registerRelay()` and `relayUrl` from SatoriServerClient

### central (central-lite)
- `discovered_relays` table and DiscoveredRelay model
- NIP-65 subscriber (`relay_watcher/nip65.py`) — queries neuron pubkeys,
  aggregates relay URLs, fetches NIP-11 for relay identity
- Traffic signature verification — only counts observations from registered
  neuron pubkeys
- Nostr-to-Evrmore address derivation (`src/utils/nostr_evrmore.py`)
- Reward distribution uses derived addresses from relay Nostr pubkeys
- Removed `POST /api/v1/peer/relay`, `relay_url` from Peer model and all
  response schemas
- Migration SQL with rollback (`migrations/add_discovered_relays.sql`)
- Tests: address derivation, traffic verification, NIP-65 parsing, models

### satori-relay
- Already configured correctly: NIP-11 document includes `pubkey` and `self`
  fields from the `NOSTR_PUBKEY` environment variable. No changes needed.


## Deployment Checklist

- [ ] Run `migrations/add_discovered_relays.sql` on the production database.
      This creates the `discovered_relays` table, migrates existing relay data
      from `peers.relay_url`, updates FKs on `relay_daily_scores` and
      `relays_audit`, and drops `peers.relay_url`. Back up the database first.
      Rollback script: `migrations/rollback_discovered_relays.sql`.

- [ ] Deploy central server (`nostr-address` branch). The relay watcher will
      begin NIP-65 discovery and traffic verification on next restart.

- [ ] Deploy satorilib (`nostr-address` branch). Neurons will pick up the
      updated server client (no more `registerRelay` or `relayUrl`).

- [ ] Deploy neuron (`nostr-address` branch). Neurons will begin publishing
      NIP-65 relay lists and expose the new Settings UI.

- [ ] Verify end-to-end: a neuron publishes NIP-65 → server discovers the
      relay → relay watcher connects and counts verified traffic → daily
      distribution pays the relay's derived Evrmore address.

- [ ] Verify relay operators can use the key normalization tool to derive
      their Evrmore private key and import it into a wallet to spend rewards.


## Open Questions

- **Reward formula:** Currently relays split the pool equally if they meet
  the 20-hour uptime threshold. Could weight by verified message count,
  unique neuron pubkeys served, or some combination.

- **Minimum traffic threshold:** Should there be a floor below which a relay
  does not receive rewards, to avoid dust payments.

- **Relays without NIP-11 pubkey:** These get discovered and monitored but
  cannot receive rewards (no pubkey → no derived address). Operators must
  configure their relay's NIP-11 `pubkey` field.
