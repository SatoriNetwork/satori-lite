# Satori Nostr Design

Architecture and design decisions for the Satori Nostr datastream protocol.

---

## Core Architecture

### Event-Driven Pub/Sub

Satori Nostr uses **Nostr relays** as the transport layer for datastream pub/sub with micropayments. This provides:

- **Decentralized infrastructure** - No central server required
- **Censorship resistance** - Multiple relay options
- **Built-in encryption** - NIP-04 for encrypted DMs
- **Public accountability** - Subscription activity is visible
- **Natural deduplication** - Relays handle event uniqueness

### Event Kinds

| Kind | Name | Visibility | Purpose |
|------|------|-----------|----------|
| **30100** | Stream Announcement | Public | Stream metadata and discovery |
| **30101** | Observation Data | Encrypted DM | Actual data delivery to subscribers |
| **30102** | Subscription | Public | Subscription announcements |
| **30103** | Payment | Encrypted DM | Micropayment notifications |

**Why these numbers?**
- 30000-39999 range = Replaceable parameterized events (NIP-01)
- Kind 30100+ keeps them grouped and easy to filter

---

## Stream Identification

### Unique Identity: (nostr_pubkey, stream_name)

Every stream is uniquely identified by the **combination** of:
1. **nostr_pubkey** - Provider's Nostr public key (hex)
2. **stream_name** - Human-readable stream name (e.g., "bitcoin-price")

**Why this works:**
- Nostr relays enforce uniqueness through replaceable events (kind 30100)
- Same provider can publish multiple streams (different names)
- Different providers can have streams with same name (different pubkeys)
- Natural collision prevention without coordination

**Example:**
```python
# Provider A's Bitcoin stream
stream_name="bitcoin-price"
nostr_pubkey="abc123..."

# Provider B's Bitcoin stream (different stream!)
stream_name="bitcoin-price"
nostr_pubkey="xyz789..."
```

### UUID Property

Streams have a computed **deterministic UUID** for database/API compatibility:

```python
uuid = uuid.uuid5(NAMESPACE_DNS, f"{nostr_pubkey}:{stream_name}")
```

**Benefits:**
- Deterministic - same input always produces same UUID
- Computed on-demand - no storage needed
- Compatible with UUID-based systems (databases, APIs)
- Single identifier for convenience

**When to use:**
- Database primary keys
- REST API endpoints (`/streams/{uuid}`)
- Cache keys
- Deduplication

---

## Multiple Keypairs Per Peer

Each peer has **two separate keypairs**:

### 1. Nostr Keypair (Signing)
- **Purpose:** Sign Nostr events, enforce stream uniqueness
- **Used for:** Event signatures, encryption, identity
- **Stored in:** Client configuration
- **Field name:** `nostr_pubkey`

### 2. Wallet Keypair (Payments)
- **Purpose:** Lightning payments, Bitcoin transactions
- **Used for:** Receiving/sending payments
- **Stored in:** Optional `metadata` field in stream announcement
- **Field name:** Can include as `metadata.wallet_pubkey`

**Why separate?**
- Security isolation - compromise of one doesn't affect the other
- Different rotation schedules
- Flexibility - can use existing wallet without changing Nostr identity
- Clear separation of concerns

**Example:**
```python
DatastreamMetadata(
    stream_name="btc-price",
    nostr_pubkey="abc123...",  # Nostr signing key
    # ... other fields ...
    metadata={
        "wallet_pubkey": "xyz789...",  # Lightning/Bitcoin wallet
        "payment_methods": ["lightning", "bitcoin"]
    }
)
```

---

## Stream Health Tracking

### Problem: How do subscribers know if a stream is still active?

**Solution:** Query relay for latest observation timestamp

Every Nostr event has **public metadata** (even when content is encrypted):

```json
{
  "id": "event-id",
  "created_at": 1234567890,  // ← PUBLIC (Unix timestamp)
  "kind": 30101,
  "tags": [
    ["stream", "bitcoin-price"],  // ← PUBLIC
    ["seq", "42"]                  // ← PUBLIC
  ],
  "content": "encrypted..."         // ← ENCRYPTED (only recipient sees)
}
```

**How it works:**

1. **Provider publishes observations** (kind 30101)
   - Event timestamp is public
   - Content is encrypted for subscribers

2. **Anyone can query for last observation:**
   ```python
   last_time = await client.get_last_observation_time("bitcoin-price")
   ```
   - Queries relay for most recent kind 30101 event for this stream
   - Gets event `created_at` timestamp (public)
   - Cannot decrypt content (only subscribers can)

3. **Check if stream is likely active:**
   ```python
   if metadata.is_likely_active(last_time):
       # Stream is publishing within expected cadence
   ```

### Cadence-Based Staleness

Streams declare expected `cadence_seconds`:
- `3600` = hourly updates
- `86400` = daily updates
- `None` = irregular/event-driven

**Staleness check:**
```python
max_delay = cadence_seconds * 2.0  # 2x tolerance
is_active = (time.time() - last_observation_time) < max_delay
```

**Why this design?**
- ✅ No wasted bandwidth (no need to republish metadata)
- ✅ No extra storage (relay already stores event timestamps)
- ✅ Privacy-preserving (timestamps public, content encrypted)
- ✅ Simple to implement (single relay query)

---

## Metadata Extensibility

### Optional `metadata` Field

Streams have an optional `metadata: dict[str, Any]` field for extensibility:

```python
DatastreamMetadata(
    # Core protocol fields
    stream_name="bitcoin-price",
    nostr_pubkey="abc123...",
    name="Bitcoin Price",
    # ... other required fields ...

    # Optional extension point
    metadata={
        "version": "1.0",
        "source": {
            "type": "rest_api",
            "url": "https://api.example.com/btc",
            "provider": "Example Exchange"
        },
        "wallet_pubkey": "xyz789...",
        "quality_score": 0.95
    }
)
```

**Design principles:**

1. **Separation of concerns**
   - Core protocol = simple, well-defined
   - Metadata = domain-specific extensions

2. **Backward compatible**
   - Field is optional (defaults to `None`)
   - Simple streams don't need it

3. **Independently versioned**
   ```python
   metadata = {
       "version": "1.0",  # Schema version
       # ... fields for v1.0
   }
   ```

4. **JSON-serializable only**
   - Can contain: dict, list, str, int, float, bool, None
   - Cannot contain: functions, classes, file handles

**Common use cases:**
- Data source tracking (API endpoints, sensors)
- Data lineage and provenance
- ML model documentation
- Quality metrics
- Wallet/payment information
- Legacy system migration data

---

## Payment Model

### Pay-Per-Observation

Subscribers pay **per observation** they want to receive:

1. **Free streams** (`price_per_obs = 0`)
   - Provider sends to all subscribers
   - No payment required

2. **Paid streams** (`price_per_obs > 0`)
   - Subscriber sends payment for observation N
   - Provider sends observation N to subscriber
   - Repeat for each observation

### Payment Flow

```
┌─────────────┐                                    ┌──────────┐
│ Subscriber  │                                    │ Provider │
└──────┬──────┘                                    └────┬─────┘
       │                                                │
       │  1. Subscribe (kind 30102, public)             │
       ├───────────────────────────────────────────────>│
       │                                                │
       │  2. First observation FREE (kind 30101, enc)   │
       │<───────────────────────────────────────────────┤
       │                                                │
       │  3. Payment for obs #2 (kind 30103, enc)       │
       ├───────────────────────────────────────────────>│
       │                                                │
       │  4. Observation #2 (kind 30101, enc)           │
       │<───────────────────────────────────────────────┤
       │                                                │
       │  5. Payment for obs #3 (kind 30103, enc)       │
       ├───────────────────────────────────────────────>│
       │                                                │
       │  6. Observation #3 (kind 30101, enc)           │
       │<───────────────────────────────────────────────┤
       │                                                │
```

**Why first observation is free:**
- Confirms subscription was accepted
- Lets subscriber verify data quality before committing
- Provider signals willingness to serve this subscriber

### Access Control

Provider tracks subscriber state:
```python
{
    "subscriber_pubkey": "sub123...",
    "stream_name": "bitcoin-price",
    "last_paid_seq": 5,  # Subscriber has paid through observation #5
    "subscribed_at": 1234567890
}
```

Before sending observation N:
```python
if stream.price_per_obs == 0:
    send_to_subscriber()  # Free stream
elif subscriber.last_paid_seq >= N:
    send_to_subscriber()  # Subscriber has paid
else:
    skip()  # Waiting for payment
```

---

## Encryption & Privacy

### What's Public vs Private

**Public (visible to everyone):**
- Stream announcements (metadata, price, description)
- Subscription announcements (who subscribes to what)
- Event timestamps (when observations published)
- Event tags (stream name, sequence numbers)

**Private (encrypted, only for recipient):**
- Observation data (actual values)
- Payment amounts and transaction details

### Why Subscriptions Are Public

Subscriptions (kind 30102) are intentionally **not encrypted**:

**Benefits:**
- **Accountability** - Everyone can see subscription activity
- **Transparency** - No hidden relationships
- **Discovery** - See which streams are popular
- **Auditing** - Track provider reliability

**Privacy note:** If privacy is critical, subscribers can:
- Use temporary Nostr keys for subscriptions
- Rotate keys periodically
- Run private relays

### Encryption Implementation

Uses **NIP-04** (Nostr encrypted direct messages):
- Shared secret via ECDH (Elliptic Curve Diffie-Hellman)
- AES-256-CBC encryption
- Each message encrypted separately
- Sender and recipient can both decrypt

---

## Relay Strategy

### Multi-Relay by Default

Clients connect to **multiple relays** simultaneously:

```python
config = SatoriNostrConfig(
    relay_urls=[
        "wss://relay.damus.io",
        "wss://nostr.wine",
        "wss://relay.primal.net"
    ]
)
```

**Benefits:**
- Redundancy - if one relay fails, others work
- Censorship resistance - hard to block all relays
- Performance - parallel queries across relays
- Discovery - different relays may have different streams

### Event Propagation

Events are published to **all connected relays**:
- Increases availability
- Faster discovery
- Better resilience

Clients use **deduplication** to handle same event from multiple relays.

---

## Scalability Considerations

### Provider Scalability

**Challenge:** How to send encrypted observations to many subscribers?

**Current approach:** Unicast to each subscriber
- Encrypt observation separately for each subscriber
- Send individual DM (kind 30101) to each
- Scales to ~100s of subscribers per stream

**Future optimization (if needed):**
- Group keys for subscriber cohorts
- Rotate shared keys periodically
- Delegate delivery to relay extensions

### Subscriber Scalability

**Challenge:** How to consume many streams efficiently?

**Current approach:** Single client subscribes to multiple streams
- One Nostr connection handles all subscriptions
- Async iteration over incoming observations
- Efficient event filtering by relay

**Scales well:** Client can handle 100s of stream subscriptions with minimal overhead

---

## Design Decisions Summary

### Why Nostr?
✅ Decentralized infrastructure
✅ Built-in encryption (NIP-04)
✅ Existing relay network
✅ Natural event deduplication
✅ Public/private data separation
✅ Censorship resistant

### Why Replaceable Events?
✅ Latest metadata automatically supersedes old versions
✅ No need to manually delete old announcements
✅ Clean stream updates

### Why Pay-Per-Observation?
✅ Simple to implement
✅ Clear value exchange
✅ Flexible (works for any cadence)
✅ No complex subscription periods

### Why Public Subscriptions?
✅ Accountability and transparency
✅ Discover popular streams
✅ Track provider reliability

### Why Separate Keypairs?
✅ Security isolation
✅ Flexibility (existing wallets)
✅ Clear separation of concerns

### Why Deterministic UUIDs?
✅ Database compatibility
✅ No coordination needed
✅ Same stream = same UUID always
✅ No storage overhead

---

## Security Model

### Threat Model

**What we protect against:**
- Unauthorized access to paid observations
- Content tampering
- Replay attacks (via deduplication)

**What we don't protect against:**
- Metadata analysis (subscriptions are public)
- Traffic analysis (relay can see connections)
- DoS on relays (use multiple relays)

### Trust Assumptions

**You trust:**
- Nostr relay operators (to deliver events)
- Your own key management
- Lightning network (for payments)

**You don't trust:**
- Other subscribers (can't decrypt your observations)
- Relays with your content (it's encrypted)
- Providers to keep secrets (subscriptions are public)

---

## Future Extensions

### Possible Enhancements

1. **Stream collections** - Bundle multiple streams
2. **Subscription periods** - Pay for time-based access
3. **Prediction markets** - Built-in betting on predictions
4. **Quality oracles** - Decentralized quality scoring
5. **Group subscriptions** - Shared keys for efficiency
6. **Stream versioning** - Breaking changes with migration
7. **Compressed delivery** - Reduce bandwidth for high-frequency streams

All extensions can use the `metadata` field without protocol changes.
