# Satori Nostr Integrations

**Production-ready reliability layer for Nostr datastreams**

This module provides higher-level abstractions that handle the complexity of reliable datastream consumption in production environments.

---

## The Problem

When using Nostr datastreams in production, you need to handle:

1. **Multi-relay coordination** - Connect to multiple relays, deduplicate events
2. **Stream health monitoring** - Continuously verify streams are publishing
3. **Connection reliability** - Detect failures, auto-reconnect, failover
4. **Stream discovery** - Find streams across multiple relays

The basic `SatoriNostr` client provides the primitives, but doesn't handle these patterns automatically.

---

## The Solution

This integration layer provides three components:

### 1. MultiRelayManager

Manages connections to multiple Nostr relays with deduplication and failover.

**Features:**
- Connect to multiple relays simultaneously
- Deduplicate events by event ID across relays
- Track relay health (connection status, error counts)
- Circuit breaker pattern for failing relays
- Automatic relay selection

**Usage:**
```python
from satori_nostr.integrations import MultiRelayManager

manager = MultiRelayManager(
    relay_urls=["wss://relay1.com", "wss://relay2.com"],
    min_active_relays=1
)

await manager.start()

# Check if event is duplicate
if not manager.is_duplicate(event_id):
    process_event(event)
    manager.add_event(event_id)

# Get healthy relays
healthy = manager.get_healthy_relays()

# Get best relay to use
best = manager.get_best_relay()
```

### 2. StreamHealthMonitor

Continuously monitors datastream health over time.

**Features:**
- Periodic health checks for subscribed streams
- Track stream state (active, stale, dead, unknown)
- Alert callbacks when health changes
- Detect stream revival after being stale
- Cadence-based staleness detection

**Usage:**
```python
from satori_nostr.integrations import StreamHealthMonitor

async def on_stale(stream_name: str):
    print(f"Stream {stream_name} went stale!")

async def on_active(stream_name: str):
    print(f"Stream {stream_name} is active again!")

monitor = StreamHealthMonitor(
    client=nostr_client,
    check_interval=60,
    on_stream_stale=on_stale,
    on_stream_active=on_active
)

await monitor.start()

# Add streams to monitor
await monitor.add_stream(metadata)

# Get current health
status = monitor.get_stream_status("bitcoin-price")
print(f"Health: {status.health}")  # ACTIVE, STALE, DEAD, UNKNOWN
```

### 3. ReliableSubscriber

**All-in-one production-ready subscriber** - combines multi-relay, health monitoring, and auto-reconnection.

**Features:**
- Multi-relay coordination with deduplication
- Continuous stream health monitoring
- Auto-reconnection on failures
- Stream discovery across relays
- Automatic payment handling
- Alert callbacks for state changes

**Usage:**
```python
from satori_nostr.integrations import ReliableSubscriber

subscriber = ReliableSubscriber(
    keys="nsec1...",
    relay_urls=[
        "wss://relay.damus.io",
        "wss://nostr.wine",
        "wss://relay.primal.net"
    ],
    min_active_relays=2,
    health_check_interval=60,
    on_stream_stale=lambda name: print(f"{name} went stale!"),
    on_stream_active=lambda name: print(f"{name} is active!")
)

await subscriber.start()

# Discover active streams
streams = await subscriber.discover_streams(tags=["bitcoin"], active_only=True)

# Subscribe with auto-payment
await subscriber.subscribe(
    stream_name="bitcoin-price",
    provider_pubkey=provider_pubkey,
    auto_pay=True
)

# Receive observations (deduplicated)
async for obs in subscriber.observations():
    print(obs.observation.value)

await subscriber.stop()
```

---

## Architecture

```
Application Layer
    ↓
ReliableSubscriber (All-in-one)
    ↓
┌─────────────────┬─────────────────┬──────────────────┐
│ MultiRelay      │ StreamHealth    │ SatoriNostr      │
│ Manager         │ Monitor         │ Client           │
│                 │                 │                  │
│ • Dedup events  │ • Check health  │ • Publish events │
│ • Track relays  │ • Alert changes │ • Subscribe      │
│ • Failover      │ • Monitor state │ • Query relays   │
└─────────────────┴─────────────────┴──────────────────┘
    ↓                   ↓                   ↓
┌───────────────────────────────────────────────────────┐
│              Nostr Relay Network                      │
│   relay1.com    relay2.com    relay3.com              │
└───────────────────────────────────────────────────────┘
```

---

## When to Use Each Component

### Use `MultiRelayManager` when:
- You need multi-relay coordination
- You want to control deduplication logic
- You're building custom reliability patterns

### Use `StreamHealthMonitor` when:
- You need to track stream health over time
- You want alerts when streams go stale
- You're monitoring specific streams

### Use `ReliableSubscriber` when:
- You want production-ready reliability out of the box
- You need all features (multi-relay + health + auto-reconnect)
- You prefer high-level abstractions over manual control

**Recommendation:** Most applications should use `ReliableSubscriber`.

---

## Configuration

### MultiRelayManager

```python
MultiRelayManager(
    relay_urls=["wss://..."],       # Relays to connect to
    min_active_relays=1,            # Minimum healthy relays
    max_error_count=5,              # Errors before marking unhealthy
    reconnect_delay=30              # Seconds before reconnect attempt
)
```

### StreamHealthMonitor

```python
StreamHealthMonitor(
    client=nostr_client,            # SatoriNostr client
    check_interval=60,              # Seconds between health checks
    on_stream_stale=callback,       # Alert when stream goes stale
    on_stream_active=callback,      # Alert when stream becomes active
    on_stream_dead=callback         # Alert when stream is dead
)
```

### ReliableSubscriber

```python
ReliableSubscriber(
    keys="nsec1...",                # Nostr private key
    relay_urls=["wss://..."],       # Relays to connect to
    min_active_relays=2,            # Minimum healthy relays
    health_check_interval=60,       # Seconds between health checks
    on_stream_stale=callback,       # Stream health alerts
    on_stream_active=callback,
    on_connection_lost=callback,    # Relay connection alerts
    on_connection_restored=callback
)
```

---

## Stream Health States

Streams can be in one of four states:

| State | Meaning | Criteria |
|-------|---------|----------|
| **ACTIVE** | Publishing normally | Within 2x expected cadence |
| **STALE** | Behind schedule | Within 5x expected cadence |
| **DEAD** | Likely stopped | Beyond 5x expected cadence |
| **UNKNOWN** | No observations | Never seen any observations |

For irregular streams (cadence=None):
- **ACTIVE**: Updated within 24 hours
- **STALE**: Updated within 5 days
- **DEAD**: No update in 5+ days

---

## Examples

See `examples/reliable_subscriber_example.py` for a complete production-ready example.

---

## Best Practices

### 1. Always Use Multiple Relays

```python
# Good - multiple relays for redundancy
relay_urls=[
    "wss://relay.damus.io",
    "wss://nostr.wine",
    "wss://relay.primal.net"
]

# Bad - single relay (no failover)
relay_urls=["wss://relay.damus.io"]
```

### 2. Set Appropriate `min_active_relays`

```python
# For critical applications
min_active_relays=2  # Always maintain 2+ relays

# For less critical
min_active_relays=1  # At least one relay
```

### 3. Use Health Callbacks for Monitoring

```python
async def on_stale(stream_name: str):
    # Alert ops team
    send_alert(f"Stream {stream_name} is stale!")
    # Log to monitoring system
    log_metric("stream_health", "stale", stream_name)

subscriber = ReliableSubscriber(
    on_stream_stale=on_stale,
    # ...
)
```

### 4. Adjust Health Check Interval Based on Cadence

```python
# For hourly streams
health_check_interval=60  # Check every minute

# For daily streams
health_check_interval=3600  # Check every hour
```

### 5. Monitor Statistics

```python
stats = subscriber.get_statistics()

# Track key metrics
print(f"Observations: {stats['observations_received']}")
print(f"Duplicates: {stats['observations_deduplicated']}")
print(f"Healthy relays: {stats['relay']['healthy_relays']}")
print(f"Active streams: {stats['health']['active_streams']}")
```

---

## Troubleshooting

### Problem: Too many duplicate events

**Cause:** Receiving same event from multiple relays

**Solution:** This is normal and expected. The deduplication system handles it automatically.

```python
# Duplicates are automatically filtered
stats = subscriber.get_statistics()
print(f"Filtered {stats['observations_deduplicated']} duplicates")
```

### Problem: Streams marked as stale but are publishing

**Cause:** Health check interval too short for stream cadence

**Solution:** Increase `health_check_interval` or adjust cadence expectations

```python
# For streams with 1-hour cadence
health_check_interval=300  # Check every 5 minutes
```

### Problem: Frequent reconnections

**Cause:** Relay is unstable or network issues

**Solution:** Add more relays or increase `reconnect_delay`

```python
# Add more relays
relay_urls=[
    "wss://relay1.com",
    "wss://relay2.com",
    "wss://relay3.com",  # More options
    "wss://relay4.com"
]

# Increase reconnect delay
reconnect_delay=60  # Wait longer before retry
```

---

## Performance

### Memory Usage

- **MultiRelayManager**: ~1KB per tracked relay + dedup cache (configurable)
- **StreamHealthMonitor**: ~1KB per monitored stream
- **ReliableSubscriber**: Combines both + client overhead

### CPU Usage

- Health checks run every `check_interval` seconds (default 60s)
- Deduplication is O(1) lookup per event
- Minimal overhead for typical workloads

### Network Usage

- Health checks query relay for last observation timestamp (small query)
- No extra bandwidth for deduplication (happens client-side)
- Multiple relay connections use more bandwidth proportionally

---

## Future Enhancements

Potential additions to this integration layer:

- **Adaptive health checking** - Adjust check interval based on stream cadence
- **Relay quality scoring** - Prefer faster, more reliable relays
- **Stream caching** - Cache metadata to reduce relay queries
- **Subscription pools** - Share subscriptions across multiple consumers
- **Advanced failover** - Sophisticated relay selection algorithms

---

## Summary

The integration layer provides production-ready reliability for Nostr datastreams:

✅ **Multi-relay coordination** - No single point of failure
✅ **Event deduplication** - Clean data flow across relays
✅ **Health monitoring** - Know when streams are live
✅ **Auto-reconnection** - Handle network issues gracefully
✅ **Stream discovery** - Find streams across all relays

**Use `ReliableSubscriber` for most applications** - it combines all features into an easy-to-use interface.
