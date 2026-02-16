#!/usr/bin/env python3
"""Simple Datastream Provider Example

This example demonstrates how to:
1. Announce a datastream
2. Accept subscriptions
3. Receive payments
4. Publish observations to paid subscribers
"""
import asyncio
import time
import sys
from nostr_sdk import Keys

# Add parent directory to path for imports
sys.path.insert(0, '/code/Satori/neuron')

from satorilib.satori_nostr import (
    SatoriNostr,
    SatoriNostrConfig,
    DatastreamMetadata,
    DatastreamObservation,
)


async def main():
    """Run a simple datastream provider."""
    print("=== Simple Datastream Provider ===\n")

    # Generate or load keys
    # In production, you'd load from secure storage
    keys = Keys.generate()
    print(f"Provider Public Key: {keys.public_key().to_hex()}\n")

    # Configure client
    config = SatoriNostrConfig(
        keys=keys.secret_key().to_hex(),
        relay_urls=[
            "wss://relay.damus.io",
            "wss://nostr.wine",
            "wss://relay.primal.net",
        ]
    )

    # Create and start client
    client = SatoriNostr(config)
    await client.start()
    print("✓ Connected to Nostr relays\n")

    # Announce a datastream
    now = int(time.time())
    metadata = DatastreamMetadata(
        stream_name="demo-bitcoin-price",
        nostr_pubkey=client.pubkey(),
        name="Demo Bitcoin Price (USD)",
        description="Simulated BTC/USD price feed - demo only",
        encrypted=True,
        price_per_obs=10,  # 10 sats per observation
        created_at=now,
        cadence_seconds=30,  # Publishes every 30 seconds
        tags=["demo", "bitcoin", "price", "usd"]
    )

    event_id = await client.announce_datastream(metadata)
    print(f"✓ Datastream announced: {metadata.stream_name}")
    print(f"  Event ID: {event_id}\n")

    # Start monitoring for payments
    print("Waiting for subscribers and payments...\n")

    # Create background task to monitor payments
    async def monitor_payments():
        async for payment in client.payments():
            print(f"\n💰 Payment received!")
            print(f"   From: {payment.payment.from_pubkey[:16]}...")
            print(f"   Amount: {payment.payment.amount_sats} sats")
            print(f"   Stream: {payment.payment.stream_name}")
            print(f"   Seq #: {payment.payment.seq_num}")

    payment_task = asyncio.create_task(monitor_payments())

    # Publish observations periodically
    seq_num = 0
    base_price = 45000

    try:
        while True:
            seq_num += 1

            # Simulate price changes
            price = base_price + (seq_num * 100)
            volume = 100 + (seq_num * 5)

            observation = DatastreamObservation(
                stream_name="demo-bitcoin-price",
                timestamp=int(time.time()),
                value={
                    "price": price,
                    "volume": volume,
                    "exchange": "demo"
                },
                seq_num=seq_num
            )

            # Publish to all paid subscribers
            event_ids = await client.publish_observation(observation, metadata)

            if event_ids:
                print(f"📊 Observation #{seq_num} published to {len(event_ids)} subscriber(s)")
                print(f"   Price: ${price:,.2f}")
                print(f"   Volume: {volume:,.2f} BTC")
            else:
                print(f"📊 Observation #{seq_num} ready (no paid subscribers yet)")

            # Show current subscribers
            subscribers = client.get_subscribers("demo-bitcoin-price")
            if subscribers:
                print(f"   Active subscribers: {len(subscribers)}")

            # Wait 30 seconds before next observation
            await asyncio.sleep(30)

    except KeyboardInterrupt:
        print("\n\nShutting down...")
    finally:
        payment_task.cancel()
        await client.stop()
        print("✓ Disconnected\n")


if __name__ == "__main__":
    # For demo purposes, you can provide your own keys via environment variable
    # export NOSTR_PROVIDER_KEY="nsec1..."
    asyncio.run(main())
