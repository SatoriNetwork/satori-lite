#!/usr/bin/env python3
"""Simple Datastream Subscriber Example

This example demonstrates how to:
1. Discover available datastreams
2. Subscribe to a datastream
3. Receive observations
4. Send payments to providers
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
)


async def main():
    """Run a simple datastream subscriber."""
    print("=== Simple Datastream Subscriber ===\n")

    # Generate or load keys
    # In production, you'd load from secure storage
    keys = Keys.generate()
    print(f"Subscriber Public Key: {keys.public_key().to_hex()}\n")

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

    # Discover datastreams
    print("Searching for datastreams...\n")

    # Search by tags
    streams = await client.discover_datastreams(tags=["bitcoin", "price"], limit=10)

    if not streams:
        print("No datastreams found. Try running simple_provider.py first!\n")
        await client.stop()
        return

    print(f"Found {len(streams)} datastream(s):\n")
    for i, stream in enumerate(streams, 1):
        print(f"{i}. {stream.name}")
        print(f"   ID: {stream.stream_name}")
        print(f"   Provider: {stream.nostr_pubkey[:16]}...")
        print(f"   Price: {stream.price_per_obs} sats/observation")
        print(f"   Encrypted: {stream.encrypted}")
        print(f"   Tags: {', '.join(stream.tags)}")
        print()

    # For demo, subscribe to the first stream
    stream = streams[0]
    print(f"Subscribing to: {stream.name}\n")

    # Announce subscription
    event_id = await client.subscribe_datastream(
        stream_name=stream.stream_name,
        provider_pubkey=stream.nostr_pubkey,
        payment_channel="demo:channel123"  # In production, use real Lightning channel
    )

    print(f"✓ Subscription announced")
    print(f"  Event ID: {event_id}\n")

    # Start receiving observations
    print("Waiting for observations...\n")

    observation_count = 0
    max_observations = 10  # Receive 10 observations then exit

    try:
        async for inbound in client.observations():
            observation_count += 1
            obs = inbound.observation

            print(f"📊 Observation #{obs.seq_num} received")
            print(f"   Stream: {obs.stream_name}")
            print(f"   Timestamp: {time.ctime(obs.timestamp)}")
            print(f"   Value: {obs.value}")
            print(f"   From: {inbound.provider_pubkey[:16]}...")

            # If this is a paid stream, send payment for the NEXT observation
            if stream.price_per_obs > 0:
                next_seq = obs.seq_num + 1
                print(f"\n💸 Sending payment for observation #{next_seq}")

                try:
                    payment_event_id = await client.send_payment(
                        provider_pubkey=stream.nostr_pubkey,
                        stream_name=stream.stream_name,
                        seq_num=next_seq,
                        amount_sats=stream.price_per_obs,
                        tx_id=f"demo:tx_{next_seq}"  # In production, use real tx ID
                    )
                    print(f"   Payment sent: {stream.price_per_obs} sats")
                    print(f"   Event ID: {payment_event_id}")
                except Exception as e:
                    print(f"   ⚠ Payment failed: {e}")

            print()

            # Exit after receiving max_observations
            if observation_count >= max_observations:
                print(f"Received {max_observations} observations. Exiting...\n")
                break

    except KeyboardInterrupt:
        print("\n\nShutting down...")
    finally:
        await client.stop()
        print("✓ Disconnected\n")


if __name__ == "__main__":
    # For demo purposes, you can provide your own keys via environment variable
    # export NOSTR_SUBSCRIBER_KEY="nsec1..."
    asyncio.run(main())
