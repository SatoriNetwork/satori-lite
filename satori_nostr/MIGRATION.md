# Migration Guide

Guide for migrating from current Satori Neuron/Central systems to Satori Nostr.

---

## Overview

Current Satori systems use:
- **Central Server** - PostgreSQL database with centralized stream registry
- **Neuron** - Complex 4-part hierarchical stream identifiers
- **Streamr** - External data source

Satori Nostr replaces this with:
- **Nostr Relays** - Decentralized event distribution
- **Simple Identifiers** - `(nostr_pubkey, stream_name)` pairs
- **Direct Publishing** - Providers publish directly to subscribers

---

## Identifier Migration

### Current System (Neuron)

```python
StreamId(
    source="Streamr",                    # Where data comes from
    author="02a85fb71485c6d7c...",      # Wallet pubkey
    stream="DATAUSD/binance/ticker",    # Hierarchical path
    target="Close"                       # Target field
)

# Generated UUID from all 4 components
uuid = stream.uuid  # Derived from (source, author, stream, target)
```

### Nostr System

```python
DatastreamMetadata(
    stream_name="DATAUSD-binance-ticker-Close",  # Flatten hierarchy
    nostr_pubkey="abc123...",                     # Nostr signing key
    # ...
    metadata={
        "legacy": {
            "source": "Streamr",
            "stream": "DATAUSD/binance/ticker",
            "target": "Close"
        },
        "wallet_pubkey": "02a85fb71485c6d7c..."  # Original author field
    }
)

# Deterministic UUID from (nostr_pubkey, stream_name)
uuid = metadata.uuid
```

### Mapping Strategy

1. **Flatten hierarchy into stream_name:**
   ```python
   # Old: stream="DATAUSD/binance/ticker", target="Close"
   # New: stream_name="DATAUSD-binance-ticker-Close"

   def migrate_stream_name(old_stream: StreamId) -> str:
       parts = [
           old_stream.stream.replace("/", "-"),
           old_stream.target
       ]
       return "-".join(parts)
   ```

2. **Store legacy identifiers in metadata:**
   ```python
   metadata = {
       "legacy": {
           "source": old_stream.source,
           "stream": old_stream.stream,
           "target": old_stream.target,
           "original_uuid": str(old_stream.uuid)
       }
   }
   ```

3. **Maintain UUID mapping table:**
   ```sql
   CREATE TABLE uuid_migration (
       old_uuid UUID,
       new_uuid UUID,
       old_source TEXT,
       old_stream TEXT,
       old_target TEXT,
       new_stream_name TEXT,
       migrated_at TIMESTAMP
   );
   ```

---

## Database Migration

### Current Central Schema

```sql
CREATE TABLE streams (
    id SERIAL PRIMARY KEY,
    uuid UUID UNIQUE,
    name VARCHAR(255) NOT NULL,
    author VARCHAR(130),        -- Wallet pubkey
    secondary VARCHAR(255),
    target VARCHAR(255),
    meta VARCHAR(255)
);
```

### Nostr-Compatible Schema

```sql
CREATE TABLE streams (
    -- Use deterministic UUID as primary key
    uuid UUID PRIMARY KEY,

    -- Core identification
    stream_name VARCHAR(255) NOT NULL,
    nostr_pubkey VARCHAR(64) NOT NULL,

    -- Stream metadata
    display_name VARCHAR(255),
    description TEXT,
    price_per_obs INTEGER,
    cadence_seconds INTEGER,
    created_at BIGINT,

    -- Optional metadata JSON
    metadata JSONB,

    -- Indexing
    UNIQUE(nostr_pubkey, stream_name)
);

CREATE INDEX idx_streams_nostr_pubkey ON streams(nostr_pubkey);
CREATE INDEX idx_streams_name ON streams(stream_name);
CREATE INDEX idx_streams_metadata ON streams USING GIN(metadata);
```

### Migration Script

```python
import asyncio
from satorilib.concepts.structs import Stream as OldStream
from satori_nostr import DatastreamMetadata, SatoriNostr

async def migrate_streams(old_streams: list[OldStream]):
    """Migrate old streams to Nostr format."""

    for old in old_streams:
        # Flatten stream name
        stream_name = f"{old.id.stream.replace('/', '-')}-{old.id.target}"

        # Create Nostr metadata
        new_metadata = DatastreamMetadata(
            stream_name=stream_name,
            nostr_pubkey=generate_nostr_key(),  # Generate new Nostr key
            name=old.id.stream,
            description=f"Migrated from {old.id.source}",
            encrypted=True,
            price_per_obs=0,  # Start free, adjust later
            created_at=int(time.time()),
            cadence_seconds=3600,  # Default hourly
            tags=extract_tags(old),
            metadata={
                "version": "1.0",
                "legacy": {
                    "source": old.id.source,
                    "author": old.id.author,
                    "stream": old.id.stream,
                    "target": old.id.target,
                    "original_uuid": str(old.id.uuid)
                },
                "wallet_pubkey": old.id.author
            }
        )

        # Store UUID mapping
        store_uuid_mapping(
            old_uuid=old.id.uuid,
            new_uuid=new_metadata.uuid,
            stream_name=stream_name
        )

        # Announce on Nostr
        await client.announce_datastream(new_metadata)

        print(f"Migrated: {old.id.stream} -> {stream_name}")
        print(f"  Old UUID: {old.id.uuid}")
        print(f"  New UUID: {new_metadata.uuid}")

def extract_tags(old_stream: OldStream) -> list[str]:
    """Extract tags from old stream structure."""
    tags = []

    # Parse stream name for tags
    parts = old_stream.id.stream.split("/")
    tags.extend([p.lower() for p in parts if p])

    # Add target as tag
    if old_stream.id.target:
        tags.append(old_stream.id.target.lower())

    # Add source
    if old_stream.id.source:
        tags.append(old_stream.id.source.lower())

    return list(set(tags))  # Deduplicate
```

---

## Key Generation

### Nostr Key Generation

```python
from nostr_sdk import Keys

# Generate new Nostr keypair
keys = Keys.generate()
nostr_secret = keys.secret_key().to_hex()  # Store securely!
nostr_pubkey = keys.public_key().to_hex()

# Or derive from existing wallet key (not recommended)
# Better to keep them separate
```

### Key Storage

```python
# Old: wallet key in plaintext
wallet_pubkey = "02a85fb71485c6d7c..."

# New: separate storage
config = {
    "nostr_secret_key": "nsec1...",  # Encrypted storage
    "wallet_pubkey": "02a85fb71485c6d7c...",  # Wallet key
}
```

---

## Observation Data Migration

### Current Format

```python
# StreamId-based observation
observation = {
    "stream_id": stream_id_tuple,
    "timestamp": 1234567890,
    "value": 45000.0,
}
```

### Nostr Format

```python
from satori_nostr import DatastreamObservation

observation = DatastreamObservation(
    stream_name="bitcoin-price",
    timestamp=1234567890,
    value=45000.0,  # Can be any JSON-serializable type
    seq_num=42
)
```

### Publishing Migration

```python
# Old: publish to Central server
old_publisher.publish(stream_id, value)

# New: publish to Nostr subscribers
await client.publish_observation(observation, metadata)
```

---

## Subscription Migration

### Current Central System

```python
# Subscribe via Central API
response = requests.post(
    "https://central.satori.com/subscribe",
    json={"stream_uuid": stream.uuid}
)
```

### Nostr System

```python
# Subscribe via Nostr events
await client.subscribe_datastream(
    stream_name="bitcoin-price",
    nostr_pubkey=provider_pubkey
)
```

---

## Metadata Migration

### Source Tracking

```python
# Old: source in StreamId
stream_id = StreamId(
    source="Streamr",  # External source
    # ...
)

# New: source in metadata
metadata = DatastreamMetadata(
    # ...
    metadata={
        "source": {
            "type": "rest_api",
            "provider": "Streamr",
            "url": "wss://streamr.network/..."
        }
    }
)
```

### Target Field

```python
# Old: target in StreamId
stream_id = StreamId(
    stream="DATAUSD/binance/ticker",
    target="Close"  # Which field to extract
)

# New: flatten into name or store in metadata
stream_name = "DATAUSD-binance-ticker-Close"  # Include in name

# Or store in metadata
metadata = {
    "extraction": {
        "field": "Close",
        "path": "ticker.Close"
    }
}
```

---

## API Compatibility Layer

### For Gradual Migration

```python
class CompatibilityLayer:
    """Provides old API on top of Nostr."""

    def __init__(self, nostr_client: SatoriNostr):
        self.client = nostr_client
        self.uuid_map = load_uuid_mapping()

    async def get_stream_by_old_uuid(self, old_uuid: str) -> DatastreamMetadata:
        """Lookup stream by old UUID."""
        mapping = self.uuid_map.get(old_uuid)
        if mapping:
            return await self.client.get_datastream(mapping["stream_name"])
        return None

    async def subscribe_by_old_uuid(self, old_uuid: str):
        """Subscribe using old UUID."""
        stream = await self.get_stream_by_old_uuid(old_uuid)
        if stream:
            await self.client.subscribe_datastream(
                stream.stream_name,
                stream.nostr_pubkey
            )

# Usage
compat = CompatibilityLayer(nostr_client)
await compat.subscribe_by_old_uuid("550e8400-e29b-41d4-a716-446655440000")
```

---

## Migration Checklist

### For Providers

- [ ] Generate Nostr keypair
- [ ] Map old StreamIds to new stream_names
- [ ] Create DatastreamMetadata for each stream
- [ ] Store legacy identifiers in metadata
- [ ] Announce streams on Nostr
- [ ] Migrate observation publishing
- [ ] Update UUID mapping table
- [ ] Test with sample subscribers

### For Subscribers

- [ ] Generate Nostr keypair
- [ ] Discover streams on Nostr
- [ ] Map old subscriptions to new streams
- [ ] Update subscription logic
- [ ] Test observation reception
- [ ] Implement payment flow

### For Central Database

- [ ] Backup current database
- [ ] Create UUID mapping table
- [ ] Run migration script
- [ ] Validate all streams migrated
- [ ] Update application queries
- [ ] Deploy compatibility layer
- [ ] Monitor for issues

---

## Gradual Migration Strategy

### Phase 1: Dual Operation

Run both systems simultaneously:

```python
# Publish to both Central and Nostr
async def publish_dual(observation_data):
    # Old system
    old_client.publish(stream_id, observation_data)

    # New system
    nostr_obs = DatastreamObservation(
        stream_name=migrated_stream_name,
        timestamp=int(time.time()),
        value=observation_data,
        seq_num=get_next_seq()
    )
    await nostr_client.publish_observation(nostr_obs, metadata)
```

### Phase 2: Read from Nostr, Write to Both

```python
# Subscribers use Nostr
async for obs in nostr_client.observations():
    process_observation(obs)

# Providers still publish to both (for legacy subscribers)
```

### Phase 3: Nostr Only

```python
# All clients use Nostr exclusively
# Decommission Central server
```

---

## Common Issues

### Issue: UUIDs Don't Match

**Problem:** Migrated UUIDs differ from original

**Solution:** Use mapping table
```python
# Store mapping during migration
uuid_mapping[old_uuid] = new_metadata.uuid

# Lookup when needed
new_uuid = uuid_mapping.get(old_uuid)
```

### Issue: Stream Name Collisions

**Problem:** Multiple streams map to same name after flattening

**Solution:** Include disambiguator
```python
# Collision: both map to "bitcoin-price"
old1 = StreamId(source="Coinbase", stream="bitcoin", target="price")
old2 = StreamId(source="Binance", stream="bitcoin", target="price")

# Solution: include source
stream_name1 = "coinbase-bitcoin-price"
stream_name2 = "binance-bitcoin-price"
```

### Issue: Lost Hierarchical Context

**Problem:** Old hierarchical paths are flattened

**Solution:** Store in metadata
```python
metadata = {
    "legacy": {
        "hierarchy": ["DATAUSD", "binance", "ticker"],
        "target": "Close"
    }
}

# Reconstruct if needed
path = "/".join(metadata["legacy"]["hierarchy"])
```

### Issue: Nostr Key Management

**Problem:** Managing many Nostr keys for many streams

**Solution:** One Nostr key per provider (not per stream)
```python
# Single Nostr keypair for provider
provider_nostr_key = load_provider_key()

# Use for all streams
for stream in provider_streams:
    metadata = DatastreamMetadata(
        stream_name=stream.name,
        nostr_pubkey=provider_nostr_key.public_key().to_hex(),
        # ...
    )
```

---

## Testing Migration

### Validation Script

```python
async def validate_migration():
    """Verify migration was successful."""

    # Load old streams
    old_streams = load_old_streams()

    # Check each migrated stream
    for old in old_streams:
        # Find migrated stream
        mapping = get_uuid_mapping(old.uuid)
        new_stream = await client.get_datastream(mapping["stream_name"])

        # Validate
        assert new_stream is not None, f"Stream not found: {old.uuid}"

        # Check legacy metadata preserved
        legacy = new_stream.metadata.get("legacy", {})
        assert legacy["source"] == old.id.source
        assert legacy["stream"] == old.id.stream
        assert legacy["target"] == old.id.target

        print(f"✓ Validated: {old.id.stream} -> {new_stream.stream_name}")

    print(f"All {len(old_streams)} streams validated!")
```

---

## Rollback Plan

If migration fails:

1. **Keep old system running** during migration
2. **Maintain dual-write** until confident
3. **Have UUID mapping** for reverting
4. **Backup all data** before migration
5. **Test rollback procedure** before production

```python
async def rollback():
    """Rollback to old system."""

    # Disable Nostr publishing
    nostr_client.stop()

    # Re-enable Central publishing
    central_client.start()

    # Notify subscribers of rollback
    send_notification("Rolled back to Central system")

    print("Rollback complete")
```

---

## Summary

**Key Changes:**
- StreamId → `(nostr_pubkey, stream_name)`
- Central server → Nostr relays
- 4-part hierarchy → flattened names
- Separate Nostr keys from wallet keys

**Migration Path:**
1. Map old identifiers to new format
2. Store legacy data in metadata
3. Maintain UUID mapping table
4. Run dual systems during transition
5. Gradually migrate subscribers
6. Decommission old system

**Benefits:**
- Decentralized infrastructure
- No central point of failure
- Built-in encryption
- Public accountability
- Simpler identifiers
