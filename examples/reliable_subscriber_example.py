#!/usr/bin/env python3
"""Reliable Subscriber Example

Demonstrates the integration layer for production-ready usage:
- Multi-relay coordination with deduplication
- Continuous stream health monitoring
- Auto-reconnection on failures
- Stream discovery across relays
"""
import asyncio
import sys

sys.path.insert(0, '/code/Satori/neuron')

from satorilib.satori_nostr.integrations import ReliableSubscriber


async def on_stream_stale(stream_name: str):
    """Called when a stream goes stale."""
    print(f"⚠️  ALERT: Stream '{stream_name}' went STALE!")


async def on_stream_active(stream_name: str):
    """Called when a stream becomes active again."""
    print(f"✅ GOOD: Stream '{stream_name}' is ACTIVE again!")


async def on_connection_lost(relay_url: str):
    """Called when connection to a relay is lost."""
    print(f"❌ CONNECTION LOST: {relay_url}")


async def on_connection_restored(relay_url: str):
    """Called when connection to a relay is restored."""
    print(f"✅ CONNECTION RESTORED: {relay_url}")


async def main():
    """Run reliable subscriber with health monitoring."""
    print("=" * 70)
    print("RELIABLE SUBSCRIBER - Production-Ready Pattern")
    print("=" * 70)
    print()

    # Create reliable subscriber with callbacks
    subscriber = ReliableSubscriber(
        keys="nsec1...",  # Your Nostr private key
        relay_urls=[
            "wss://relay.damus.io",
            "wss://nostr.wine",
            "wss://relay.primal.net"
        ],
        min_active_relays=2,          # Maintain at least 2 relay connections
        health_check_interval=60,     # Check stream health every minute
        on_stream_stale=on_stream_stale,
        on_stream_active=on_stream_active,
        on_connection_lost=on_connection_lost,
        on_connection_restored=on_connection_restored
    )

    await subscriber.start()
    print("✓ Reliable subscriber started\n")

    # Discover active streams across all relays
    print("Discovering active Bitcoin streams across all relays...")
    streams = await subscriber.discover_streams(
        tags=["bitcoin", "price"],
        active_only=True  # Only currently publishing streams
    )

    print(f"Found {len(streams)} active stream(s):\n")
    for stream in streams:
        last_time = await subscriber._client.get_last_observation_time(stream.stream_name)
        health = subscriber.get_stream_health(stream.stream_name)
        print(f"  • {stream.name}")
        print(f"    Stream: {stream.stream_name}")
        print(f"    Provider: {stream.nostr_pubkey[:16]}...")
        print(f"    Price: {stream.price_per_obs} sats/obs")
        print(f"    Health: {health}")
        print(f"    Cadence: {stream.cadence_seconds}s")
        print()

    # Subscribe to streams with auto-payment
    for stream in streams[:2]:  # Subscribe to first 2
        print(f"Subscribing to: {stream.name}")
        await subscriber.subscribe(
            stream_name=stream.stream_name,
            provider_pubkey=stream.nostr_pubkey,
            auto_pay=True  # Automatically pay for observations
        )

    print()
    print("=" * 70)
    print("Receiving observations (deduplicated across relays)...")
    print("Health monitoring running in background...")
    print("=" * 70)
    print()

    # Receive observations
    observation_count = 0
    max_observations = 20

    try:
        async for inbound in subscriber.observations():
            observation_count += 1
            obs = inbound.observation

            # Get health status
            health = subscriber.get_stream_health(obs.stream_name)

            print(f"📊 Observation #{obs.seq_num} from {obs.stream_name}")
            print(f"   Health: {health}")
            print(f"   Value: {obs.value}")
            print(f"   Provider: {inbound.nostr_pubkey[:16]}...")
            print()

            # Periodically show statistics
            if observation_count % 5 == 0:
                stats = subscriber.get_statistics()
                print("--- Statistics ---")
                print(f"  Observations received: {stats['observations_received']}")
                print(f"  Duplicates filtered: {stats['observations_deduplicated']}")
                print(f"  Payments sent: {stats['payments_sent']}")
                print(f"  Healthy relays: {stats['relay']['healthy_relays']}")
                print(f"  Active streams: {stats['health']['active_streams']}")
                print(f"  Stale streams: {stats['health']['stale_streams']}")
                print()

            # Exit after max observations
            if observation_count >= max_observations:
                print(f"Received {max_observations} observations. Exiting...")
                break

    except KeyboardInterrupt:
        print("\n\nInterrupted by user")

    finally:
        # Show final statistics
        print()
        print("=" * 70)
        print("Final Statistics:")
        print("=" * 70)

        stats = subscriber.get_statistics()
        print(f"Total observations: {stats['observations_received']}")
        print(f"Duplicates filtered: {stats['observations_deduplicated']}")
        print(f"Payments sent: {stats['payments_sent']}")
        print(f"Reconnections: {stats['reconnections']}")
        print()

        relay_status = subscriber.get_relay_status()
        print("Relay Status:")
        for url, status in relay_status.items():
            print(f"  {url}")
            print(f"    Connected: {status.connected}")
            print(f"    Errors: {status.error_count}")

        print()
        await subscriber.stop()
        print("✓ Subscriber stopped\n")


if __name__ == "__main__":
    """
    This example demonstrates the integration layer features:

    1. MULTI-RELAY COORDINATION
       - Connects to multiple relays simultaneously
       - Deduplicates events across relays
       - Failover to working relays

    2. STREAM HEALTH MONITORING
       - Continuously checks if streams are publishing
       - Alerts when streams go stale or recover
       - Uses relay timestamps (no metadata republishing)

    3. AUTO-RECONNECTION
       - Detects connection failures
       - Automatically reconnects to failed relays
       - Maintains minimum relay count

    4. DISCOVERY
       - Finds streams across all connected relays
       - Deduplicates by UUID
       - Filters to only active streams

    5. AUTO-PAYMENT
       - Automatically sends payments for observations
       - Configurable per subscription
    """
    asyncio.run(main())
