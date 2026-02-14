# Satori Nostr Examples

Comprehensive usage examples for common datastream patterns.

---

## Basic Provider

Announce a stream and publish observations:

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

async def basic_provider():
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
    print(f"Stream announced: {metadata.stream_name}")

    # Publish observations
    for seq in range(1, 100):
        observation = DatastreamObservation(
            stream_name="bitcoin-price",
            timestamp=int(time.time()),
            value={"price": 45000.0, "volume": 123.45},
            seq_num=seq
        )

        event_ids = await client.publish_observation(observation, metadata)
        print(f"Observation {seq} sent to {len(event_ids)} subscribers")

        await asyncio.sleep(3600)  # Hourly

    await client.stop()

asyncio.run(basic_provider())
```

---

## Basic Subscriber

Discover streams, subscribe, and receive observations:

```python
import asyncio
from satori_nostr import SatoriNostr, SatoriNostrConfig

async def basic_subscriber():
    config = SatoriNostrConfig(
        keys="nsec1...",
        relay_urls=["wss://relay.damus.io"]
    )

    client = SatoriNostr(config)
    await client.start()

    # Discover streams
    streams = await client.discover_datastreams(tags=["bitcoin"])
    print(f"Found {len(streams)} Bitcoin streams")

    # Subscribe to first stream
    if streams:
        stream = streams[0]
        await client.subscribe_datastream(
            stream.stream_name,
            stream.nostr_pubkey
        )
        print(f"Subscribed to {stream.name}")

        # Receive observations
        async for inbound in client.observations():
            obs = inbound.observation
            print(f"Observation {obs.seq_num}: {obs.value}")

            # Send payment for next observation
            if stream.price_per_obs > 0:
                await client.send_payment(
                    provider_pubkey=stream.nostr_pubkey,
                    stream_name=stream.stream_name,
                    seq_num=obs.seq_num + 1,
                    amount_sats=stream.price_per_obs
                )

    await client.stop()

asyncio.run(basic_subscriber())
```

---

## Free Stream (No Payments)

```python
metadata = DatastreamMetadata(
    stream_name="weather-nyc",
    nostr_pubkey=client.pubkey(),
    name="NYC Weather",
    description="Temperature and humidity from Manhattan",
    encrypted=False,  # Free streams can be unencrypted
    price_per_obs=0,  # Free!
    created_at=int(time.time()),
    cadence_seconds=600,  # Every 10 minutes
    tags=["weather", "nyc", "free"]
)

await client.announce_datastream(metadata)

# Publish to all subscribers (no payment check)
observation = DatastreamObservation(
    stream_name="weather-nyc",
    timestamp=int(time.time()),
    value={"temp_f": 72, "humidity": 65},
    seq_num=1
)

await client.publish_observation(observation, metadata)
```

---

## Stream with Source Metadata

Track where data comes from:

```python
metadata = DatastreamMetadata(
    stream_name="btc-coinbase",
    nostr_pubkey=client.pubkey(),
    name="Bitcoin Coinbase",
    description="BTC/USD from Coinbase API",
    encrypted=True,
    price_per_obs=5,
    created_at=int(time.time()),
    cadence_seconds=60,
    tags=["bitcoin", "coinbase", "api"],
    metadata={
        "version": "1.0",
        "source": {
            "type": "rest_api",
            "provider": "Coinbase",
            "url": "https://api.coinbase.com/v2/prices/BTC-USD/spot",
            "method": "GET",
            "response_path": "data.amount"
        },
        "wallet_pubkey": "xyz789..."  # Lightning wallet for payments
    }
)
```

---

## Stream with Data Lineage

Document transformations and processing:

```python
metadata = DatastreamMetadata(
    stream_name="btc-cleaned",
    nostr_pubkey=client.pubkey(),
    name="Bitcoin Price (Cleaned)",
    description="BTC/USD with outliers removed and smoothed",
    encrypted=True,
    price_per_obs=10,
    created_at=int(time.time()),
    cadence_seconds=300,
    tags=["bitcoin", "processed", "cleaned"],
    metadata={
        "version": "1.0",
        "lineage": {
            "source_stream": "btc-coinbase",
            "source_provider": "abc123...",
            "transformations": [
                {
                    "type": "outlier_removal",
                    "method": "IQR",
                    "threshold": 3.0
                },
                {
                    "type": "smoothing",
                    "method": "moving_average",
                    "window": 10
                }
            ],
            "quality_score": 0.95
        }
    }
)
```

---

## ML Prediction Stream

Document model and predictions:

```python
metadata = DatastreamMetadata(
    stream_name="btc-lstm-24h",
    nostr_pubkey=client.pubkey(),
    name="Bitcoin LSTM 24h Forecast",
    description="24-hour Bitcoin price prediction",
    encrypted=True,
    price_per_obs=50,
    created_at=int(time.time()),
    cadence_seconds=3600,
    tags=["bitcoin", "prediction", "lstm", "24h"],
    metadata={
        "version": "1.0",
        "model": {
            "type": "LSTM",
            "architecture": "2-layer-128-units",
            "framework": "tensorflow",
            "trained_samples": 175200,
            "performance": {
                "mse": 0.0023,
                "r2_score": 0.85
            }
        },
        "prediction": {
            "horizon": "24h",
            "target_stream": "btc-price",
            "confidence_interval": 0.95
        }
    }
)
```

---

## IoT Sensor Stream

Physical device data with location and calibration:

```python
metadata = DatastreamMetadata(
    stream_name="weather-nyc-001",
    nostr_pubkey=client.pubkey(),
    name="NYC Weather Station #001",
    description="Rooftop weather sensor in Manhattan",
    encrypted=False,
    price_per_obs=0,
    created_at=int(time.time()),
    cadence_seconds=600,
    tags=["weather", "nyc", "iot", "sensor"],
    metadata={
        "version": "1.0",
        "source": {
            "type": "iot_sensor",
            "manufacturer": "WeatherTech",
            "model": "WS-3000",
            "serial": "WT-NYC-001-2024",
            "firmware": "3.2.1"
        },
        "location": {
            "latitude": 40.7128,
            "longitude": -74.0060,
            "elevation_m": 15,
            "address": "Manhattan, NYC"
        },
        "sensors": [
            {"type": "temperature", "unit": "celsius", "accuracy": 0.1},
            {"type": "humidity", "unit": "percent", "accuracy": 2.0},
            {"type": "pressure", "unit": "hPa", "accuracy": 0.5}
        ],
        "calibration": {
            "last_calibrated": 1704067200,
            "next_due": 1719619200
        }
    }
)
```

---

## Discovering Active Streams

Find streams that are currently publishing:

```python
async def discover_active():
    client = SatoriNostr(config)
    await client.start()

    # Find all active Bitcoin streams
    active_streams = await client.discover_active_datastreams(
        tags=["bitcoin"],
        max_staleness_multiplier=2.0  # 2x cadence tolerance
    )

    for stream in active_streams:
        last_time = await client.get_last_observation_time(stream.stream_name)
        age = time.time() - last_time
        print(f"{stream.name}")
        print(f"  Last observation: {age:.0f} seconds ago")
        print(f"  Cadence: {stream.cadence_seconds}s")
        print(f"  Price: {stream.price_per_obs} sats/obs")
        print()

    await client.stop()
```

---

## Provider Monitoring Payments

Track incoming payments and update subscriber access:

```python
async def provider_with_payments():
    client = SatoriNostr(config)
    await client.start()

    # Announce stream
    metadata = DatastreamMetadata(...)
    await client.announce_datastream(metadata)

    # Monitor payments in background
    async def handle_payments():
        async for payment in client.payments():
            print(f"Payment received:")
            print(f"  From: {payment.payment.from_pubkey[:16]}...")
            print(f"  Amount: {payment.payment.amount_sats} sats")
            print(f"  Stream: {payment.payment.stream_name}")
            print(f"  Seq: {payment.payment.seq_num}")

            # Payment automatically updates subscriber state
            # Next observation will be sent to this subscriber

    payment_task = asyncio.create_task(handle_payments())

    # Publish observations
    seq = 0
    while True:
        seq += 1
        observation = DatastreamObservation(
            stream_name=metadata.stream_name,
            timestamp=int(time.time()),
            value={"data": f"observation_{seq}"},
            seq_num=seq
        )

        event_ids = await client.publish_observation(observation, metadata)
        print(f"Published obs {seq} to {len(event_ids)} paid subscribers")

        await asyncio.sleep(60)

    await client.stop()
```

---

## Multi-Stream Subscriber

Subscribe to multiple streams and handle them all:

```python
async def multi_stream_subscriber():
    client = SatoriNostr(config)
    await client.start()

    # Discover and subscribe to multiple streams
    bitcoin_streams = await client.discover_datastreams(tags=["bitcoin"])
    weather_streams = await client.discover_datastreams(tags=["weather"])

    subscriptions = {}  # stream_name -> metadata

    for stream in bitcoin_streams[:3] + weather_streams[:2]:
        await client.subscribe_datastream(
            stream.stream_name,
            stream.nostr_pubkey
        )
        subscriptions[stream.stream_name] = stream
        print(f"Subscribed to {stream.name}")

        # Pay for first observation if needed
        if stream.price_per_obs > 0:
            await client.send_payment(
                provider_pubkey=stream.nostr_pubkey,
                stream_name=stream.stream_name,
                seq_num=1,
                amount_sats=stream.price_per_obs
            )

    # Receive from all streams
    async for inbound in client.observations():
        obs = inbound.observation
        stream = subscriptions.get(obs.stream_name)

        if stream:
            print(f"{stream.name} #{obs.seq_num}: {obs.value}")

            # Pay for next if needed
            if stream.price_per_obs > 0:
                await client.send_payment(
                    provider_pubkey=stream.nostr_pubkey,
                    stream_name=stream.stream_name,
                    seq_num=obs.seq_num + 1,
                    amount_sats=stream.price_per_obs
                )

    await client.stop()
```

---

## Stream Health Checking

Check if streams are still actively publishing:

```python
async def check_stream_health():
    client = SatoriNostr(config)
    await client.start()

    # Get stream metadata
    stream = await client.get_datastream("bitcoin-price")

    if stream:
        # Get last observation time from relay
        last_time = await client.get_last_observation_time(stream.stream_name)

        if last_time:
            age = time.time() - last_time
            print(f"Stream: {stream.name}")
            print(f"Last observation: {age:.0f} seconds ago")

            # Check if likely still active
            if stream.is_likely_active(last_time):
                print("Status: ACTIVE ✓")
            else:
                print("Status: STALE (may be inactive)")

            # Show staleness threshold
            if stream.cadence_seconds:
                max_delay = stream.cadence_seconds * 2.0
                print(f"Expected cadence: {stream.cadence_seconds}s")
                print(f"Staleness threshold: {max_delay}s")
        else:
            print("No observations found")

    await client.stop()
```

---

## Event-Driven Stream (Irregular Cadence)

For news, alerts, or sporadic events:

```python
metadata = DatastreamMetadata(
    stream_name="crypto-alerts",
    nostr_pubkey=client.pubkey(),
    name="Crypto Price Alerts",
    description="Alerts when BTC moves >5%",
    encrypted=True,
    price_per_obs=1,
    created_at=int(time.time()),
    cadence_seconds=None,  # Irregular/event-driven
    tags=["crypto", "alerts", "bitcoin", "events"]
)

# Publish only when event occurs
if price_change > 0.05:
    observation = DatastreamObservation(
        stream_name="crypto-alerts",
        timestamp=int(time.time()),
        value={
            "alert": "BTC up 5.2% in 1 hour",
            "price": 47340,
            "change_pct": 5.2
        },
        seq_num=next_seq
    )
    await client.publish_observation(observation, metadata)
```

---

## Subscriber with Quality Filtering

Only consume high-quality streams:

```python
async def quality_filtered_subscriber():
    client = SatoriNostr(config)
    await client.start()

    # Discover streams
    streams = await client.discover_datastreams(tags=["bitcoin"])

    # Filter by quality metadata
    quality_streams = []
    for stream in streams:
        if stream.metadata:
            quality = stream.metadata.get("quality_score", 0)
            if quality >= 0.9:
                quality_streams.append(stream)

    print(f"Found {len(quality_streams)} high-quality streams")

    # Subscribe only to quality streams
    for stream in quality_streams:
        await client.subscribe_datastream(
            stream.stream_name,
            stream.nostr_pubkey
        )

    # ... consume observations ...

    await client.stop()
```

---

## Statistics Tracking

Monitor client activity:

```python
async def provider_with_stats():
    client = SatoriNostr(config)
    await client.start()

    # ... announce and publish ...

    # Periodically check statistics
    while True:
        await asyncio.sleep(300)  # Every 5 minutes

        stats = client.get_statistics()
        print("Statistics:")
        print(f"  Observations sent: {stats['observations_sent']}")
        print(f"  Payments received: {stats['payments_received']}")
        print(f"  Subscriptions: {stats['subscriptions_announced']}")

        # Check subscribers for each stream
        for stream_name in client.list_announced_streams():
            subscribers = client.get_subscribers(stream_name.stream_name)
            print(f"  {stream_name.name}: {len(subscribers)} subscribers")

    await client.stop()
```

---

## Unsubscribing from Streams

```python
async def unsubscribe_example():
    client = SatoriNostr(config)
    await client.start()

    # Subscribe
    stream = await client.get_datastream("bitcoin-price")
    await client.subscribe_datastream(
        stream.stream_name,
        stream.nostr_pubkey
    )

    # ... receive observations ...

    # Unsubscribe
    await client.unsubscribe_datastream(
        stream.stream_name,
        stream.nostr_pubkey
    )
    print("Unsubscribed")

    await client.stop()
```

---

## Using Stream UUID

For database or API integration:

```python
# Get stream
stream = await client.get_datastream("bitcoin-price")

# Use deterministic UUID
print(f"Stream UUID: {stream.uuid}")

# Store in database
db.execute(
    "INSERT INTO streams (uuid, name, provider, price) VALUES (?, ?, ?, ?)",
    (stream.uuid, stream.stream_name, stream.nostr_pubkey, stream.price_per_obs)
)

# Query by UUID
stream_uuid = "550e8400-e29b-41d4-a716-446655440000"
record = db.query("SELECT * FROM streams WHERE uuid = ?", (stream_uuid,))

# UUID is deterministic - same stream always has same UUID
stream2 = await client.get_datastream("bitcoin-price")
assert stream.uuid == stream2.uuid  # Always true
```

---

## Best Practices

### Provider Best Practices

1. **Use appropriate cadence**
   ```python
   # High-frequency trading
   cadence_seconds=1

   # Real-time monitoring
   cadence_seconds=60

   # Hourly updates (recommended for most)
   cadence_seconds=3600

   # Daily summaries
   cadence_seconds=86400

   # Event-driven/irregular
   cadence_seconds=None
   ```

2. **Include metadata for context**
   ```python
   metadata={
       "version": "1.0",
       "source": {...},
       "quality_score": 0.95,
       "wallet_pubkey": "..."
   }
   ```

3. **Monitor subscriber state**
   ```python
   # Check who's subscribed
   subscribers = client.get_subscribers(stream_name)

   # Check specific subscriber
   sub_info = client.get_subscriber_info(stream_name, subscriber_pubkey)
   if sub_info.last_paid_seq:
       print(f"Subscriber paid through #{sub_info.last_paid_seq}")
   ```

### Subscriber Best Practices

1. **Discover active streams**
   ```python
   # Don't just discover - check if active
   active = await client.discover_active_datastreams(tags=["bitcoin"])
   ```

2. **Pay promptly**
   ```python
   # Pay for NEXT observation immediately after receiving current
   async for inbound in client.observations():
       obs = inbound.observation
       # Process observation...

       # Pay for next
       await client.send_payment(..., seq_num=obs.seq_num + 1, ...)
   ```

3. **Handle multiple streams efficiently**
   ```python
   # Single client can handle many streams
   subscriptions = {}  # Track by stream_name
   async for inbound in client.observations():
       stream = subscriptions[inbound.observation.stream_name]
       # Handle based on stream type
   ```

### General Best Practices

1. **Use multiple relays**
   ```python
   relay_urls=[
       "wss://relay.damus.io",
       "wss://nostr.wine",
       "wss://relay.primal.net"
   ]
   ```

2. **Handle errors gracefully**
   ```python
   try:
       await client.send_payment(...)
   except Exception as e:
       print(f"Payment failed: {e}")
       # Retry or skip
   ```

3. **Clean shutdown**
   ```python
   try:
       # ... client operations ...
   finally:
       await client.stop()
   ```
