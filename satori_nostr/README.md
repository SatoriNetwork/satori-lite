# Satori Nostr

Decentralized datastream pub/sub with micropayments over Nostr.

---

## Overview

Satori Nostr enables decentralized data publishing and subscription with built-in micropayments. Providers publish observations to paying subscribers via encrypted Nostr DMs, while maintaining public visibility of subscriptions for accountability.

**Key Features:**
- **Decentralized** - No central server, uses Nostr relay network
- **Encrypted** - NIP-04 encryption for paid observations
- **Micropayments** - Pay-per-observation model with satoshis
- **Discovery** - Find streams by tags and topics
- **Accountability** - Public subscription activity
- **Stream Health** - Built-in staleness detection

---

## Quick Start

### Installation

```bash
cd /code/Satori/neuron
pip install nostr-sdk
```

### Simple Provider

```python
import asyncio
import time
from satori_nostr import (
    SatoriNostr,
    SatoriNostrConfig,
    DatastreamMetadata,
    DatastreamObservation,
    CADENCE_HOURLY,
)

async def main():
    # Configure client
    config = SatoriNostrConfig(
        keys="nsec1...",  # Your Nostr private key
        relay_urls=["wss://relay.damus.io", "wss://nostr.wine"]
    )

    client = SatoriNostr(config)
    await client.start()

    # Announce datastream
    metadata = DatastreamMetadata(
        stream_name="bitcoin-price",
        nostr_pubkey=client.pubkey(),
        name="Bitcoin Price (USD)",
        description="Real-time BTC/USD from Coinbase",
        encrypted=True,
        price_per_obs=10,  # 10 sats per observation
        created_at=int(time.time()),
        cadence_seconds=CADENCE_HOURLY,
        tags=["bitcoin", "price", "usd"]
    )

    await client.announce_datastream(metadata)

    # Publish observations
    for seq in range(1, 100):
        observation = DatastreamObservation(
            stream_name="bitcoin-price",
            timestamp=int(time.time()),
            value={"price": 45000.0, "volume": 123.45},
            seq_num=seq
        )

        event_ids = await client.publish_observation(observation, metadata)
        print(f"Published to {len(event_ids)} subscribers")

        await asyncio.sleep(3600)  # Hourly

    await client.stop()

asyncio.run(main())
```

### Simple Subscriber

```python
import asyncio
from satori_nostr import SatoriNostr, SatoriNostrConfig

async def main():
    config = SatoriNostrConfig(
        keys="nsec1...",
        relay_urls=["wss://relay.damus.io"]
    )

    client = SatoriNostr(config)
    await client.start()

    # Discover streams
    streams = await client.discover_datastreams(tags=["bitcoin"])

    # Subscribe to first stream
    if streams:
        stream = streams[0]
        await client.subscribe_datastream(
            stream.stream_name,
            stream.nostr_pubkey
        )

        # Receive observations
        async for inbound in client.observations():
            obs = inbound.observation
            print(f"Received: {obs.value}")

            # Send payment for next observation
            await client.send_payment(
                provider_pubkey=stream.nostr_pubkey,
                stream_name=stream.stream_name,
                seq_num=obs.seq_num + 1,
                amount_sats=stream.price_per_obs
            )

    await client.stop()

asyncio.run(main())
```

---

## Core Concepts

### Stream Identity

Every stream is uniquely identified by:
- **nostr_pubkey** - Provider's Nostr public key (for signing events)
- **stream_name** - Human-readable name (e.g., "bitcoin-price")

```python
# Each provider can have multiple streams
stream1 = DatastreamMetadata(
    stream_name="bitcoin-price",
    nostr_pubkey="abc123...",
    # ...
)

stream2 = DatastreamMetadata(
    stream_name="ethereum-price",
    nostr_pubkey="abc123...",  # Same provider, different stream
    # ...
)
```

Streams also have a **deterministic UUID** for database/API compatibility:
```python
uuid = stream.uuid  # Computed from (nostr_pubkey, stream_name)
```

### Event Types

| Kind | Name | Visibility | Purpose |
|------|------|-----------|----------|
| 30100 | Stream Announcement | Public | Metadata and discovery |
| 30101 | Observation | Encrypted | Data delivery |
| 30102 | Subscription | Public | Subscription announcements |
| 30103 | Payment | Encrypted | Payment notifications |

### Payment Flow

1. Subscriber discovers stream (query kind 30100)
2. Subscriber announces subscription (kind 30102, public)
3. Provider sees subscription, adds to list
4. Provider sends first observation FREE
5. For each subsequent observation:
   - Subscriber sends payment (kind 30103)
   - Provider receives payment, updates access
   - Provider sends observation (kind 30101)
   - Repeat

### Stream Health

Check if a stream is actively publishing:

```python
# Get last observation time (public timestamp on relay)
last_time = await client.get_last_observation_time("bitcoin-price")

# Check if stream is active based on cadence
if metadata.is_likely_active(last_time):
    print("Stream is active")
else:
    print("Stream may be stale")
```

---

## Data Models

### DatastreamMetadata

Public stream announcement:

```python
metadata = DatastreamMetadata(
    stream_name="bitcoin-price",      # Human-readable name
    nostr_pubkey="abc123...",         # Provider's Nostr pubkey
    name="Bitcoin Price (USD)",       # Display name
    description="BTC/USD from Coinbase",
    encrypted=True,                   # Encrypt observations?
    price_per_obs=10,                 # Sats per observation
    created_at=1234567890,            # Unix timestamp
    cadence_seconds=3600,             # Expected update frequency
    tags=["bitcoin", "price", "usd"], # Discovery tags

    # Optional metadata for extensions
    metadata={
        "source": {"type": "api", "url": "..."},
        "wallet_pubkey": "xyz789..."  # For Lightning payments
    }
)
```

### DatastreamObservation

Single data point:

```python
observation = DatastreamObservation(
    stream_name="bitcoin-price",
    timestamp=1234567890,
    value={"price": 45000.0, "volume": 123.45},  # Any JSON type
    seq_num=42
)
```

### Configuration

```python
config = SatoriNostrConfig(
    keys="nsec1...",  # Nostr private key (nsec or hex)
    relay_urls=[      # Nostr relays to connect to
        "wss://relay.damus.io",
        "wss://nostr.wine",
        "wss://relay.primal.net"
    ],
    active_relay_timeout_ms=8000,  # Optional: relay failover timeout
    dedupe_db_path=None            # Optional: SQLite path for deduplication
)
```

---

## API Reference

### Provider APIs

```python
# Announce a datastream
await client.announce_datastream(metadata)

# Publish observation to paid subscribers
await client.publish_observation(observation, metadata)

# Monitor incoming payments
async for payment in client.payments():
    print(f"Received {payment.payment.amount_sats} sats")

# Check subscribers
subscribers = client.get_subscribers(stream_name)
```

### Subscriber APIs

```python
# Discover datastreams by tags
streams = await client.discover_datastreams(tags=["bitcoin"])

# Discover only active streams
active = await client.discover_active_datastreams(tags=["bitcoin"])

# Subscribe to a stream
await client.subscribe_datastream(stream_name, provider_pubkey)

# Send payment
await client.send_payment(
    provider_pubkey=provider_pubkey,
    stream_name=stream_name,
    seq_num=42,
    amount_sats=10
)

# Receive observations
async for inbound in client.observations():
    print(inbound.observation.value)

# Unsubscribe
await client.unsubscribe_datastream(stream_name, provider_pubkey)
```

### Utility APIs

```python
# Get specific stream
stream = await client.get_datastream(stream_name)

# Check last observation time
last_time = await client.get_last_observation_time(stream_name)

# Get client statistics
stats = client.get_statistics()

# List announced streams (if provider)
streams = client.list_announced_streams()

# Get subscriber info (if provider)
info = client.get_subscriber_info(stream_name, subscriber_pubkey)
all_subs = client.get_all_subscribers_info(stream_name)
```

---

## Cadence Constants

Standard cadence values (in seconds):

```python
from satori_nostr import (
    CADENCE_REALTIME,   # 1 second
    CADENCE_MINUTE,     # 60 seconds
    CADENCE_5MIN,       # 300 seconds
    CADENCE_HOURLY,     # 3600 seconds (recommended)
    CADENCE_DAILY,      # 86400 seconds
    CADENCE_WEEKLY,     # 604800 seconds
    CADENCE_IRREGULAR,  # None (event-driven)
)
```

---

## Examples

See comprehensive examples in:
- **`examples/simple_provider.py`** - Basic provider
- **`examples/simple_subscriber.py`** - Basic subscriber
- **`examples/datastream_marketplace.py`** - Full marketplace simulation
- **`EXAMPLES.md`** - Additional usage patterns

---

## Documentation

- **`DESIGN.md`** - Architecture and design decisions
- **`EXAMPLES.md`** - Comprehensive usage examples
- **`MIGRATION.md`** - Migration from legacy systems

---

## Testing

Run unit tests:
```bash
pytest tests/test_satori_nostr_models.py -v
```

Run integration tests:
```bash
pytest tests/test_satori_nostr_integration.py -v
```

All tests:
```bash
pytest tests/test_satori_nostr*.py -v
```

---

## Architecture

### Decentralized Infrastructure

- Uses **Nostr relay network** (no central server)
- Providers publish directly to subscribers
- Multiple relays for redundancy
- Censorship-resistant

### Encryption

- **NIP-04** encrypted DMs for observations and payments
- Public announcements for discovery and accountability
- Separate Nostr keypair from wallet keypair

### Scalability

- Providers: ~100s of subscribers per stream
- Subscribers: ~100s of stream subscriptions per client
- Async/await for efficient concurrent operations

---

## Security

### Keypair Separation

Each peer has **two keypairs**:

1. **Nostr Keypair** - For signing events and encryption
   - Used for stream identity
   - Used for encrypting observations

2. **Wallet Keypair** - For Lightning/Bitcoin payments
   - Stored in stream `metadata` field
   - Separate from Nostr identity

### Encryption

- Observations encrypted with NIP-04
- Only subscriber and provider can decrypt
- Relay cannot read observation content
- Event timestamps are public (for stream health)

### Access Control

- Providers track paid subscribers
- Only send observations to paid subscribers (or all for free streams)
- First observation always free (welcome/verification)

---

## License

Part of the Satori Network project.

---

## Support

For issues or questions:
- GitHub: [SatoriNetwork/satori-lite](https://github.com/SatoriNetwork/satori-lite)
- Examples: See `examples/` directory
- Documentation: See `DESIGN.md` and `EXAMPLES.md`
