#!/usr/bin/env python3
"""Datastream Marketplace Example

This example demonstrates a complete datastream ecosystem with:
- Multiple providers offering different datastreams
- Multiple subscribers discovering and consuming data
- Payment flows and access control
- Free and paid streams
"""
import asyncio
import time
import random
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


class DatastreamProvider:
    """Represents a single datastream provider neuron."""

    def __init__(self, name: str, stream_name: str, description: str, price: int, tags: list[str]):
        self.name = name
        self.stream_name = stream_name
        self.description = description
        self.price = price
        self.tags = tags
        self.keys = Keys.generate()
        self.client = None
        self.seq_num = 0

    async def start(self, relay_urls: list[str]):
        """Start the provider and announce datastream."""
        config = SatoriNostrConfig(
            keys=self.keys.secret_key().to_hex(),
            relay_urls=relay_urls,
        )

        self.client = SatoriNostr(config)
        await self.client.start()

        # Announce datastream
        now = int(time.time())
        metadata = DatastreamMetadata(
            stream_name=self.stream_name,
            nostr_pubkey=self.client.pubkey(),
            name=self.name,
            description=self.description,
            encrypted=(self.price > 0),  # Encrypt paid streams
            price_per_obs=self.price,
            created_at=now,
            cadence_seconds=3600,  # Hourly cadence for marketplace demo
            tags=self.tags
        )

        await self.client.announce_datastream(metadata)
        print(f"✓ {self.name} online (Provider: {self.client.pubkey()[:16]}...)")

        return metadata

    async def publish(self, metadata: DatastreamMetadata) -> int:
        """Publish a single observation."""
        self.seq_num += 1

        # Generate data based on stream type
        value = self._generate_data()

        observation = DatastreamObservation(
            stream_name=self.stream_name,
            timestamp=int(time.time()),
            value=value,
            seq_num=self.seq_num
        )

        event_ids = await self.client.publish_observation(observation, metadata)
        return len(event_ids)

    def _generate_data(self):
        """Generate simulated data."""
        if "price" in self.tags:
            return {
                "price": random.uniform(40000, 50000),
                "volume": random.uniform(100, 1000),
            }
        elif "weather" in self.tags:
            return {
                "temperature": random.uniform(60, 80),
                "humidity": random.uniform(40, 70),
                "conditions": random.choice(["sunny", "cloudy", "rainy"]),
            }
        elif "news" in self.tags:
            return {
                "headline": f"Breaking news #{self.seq_num}",
                "category": random.choice(["tech", "finance", "science"]),
            }
        else:
            return {"data": f"observation_{self.seq_num}"}

    async def monitor_payments(self):
        """Monitor and log incoming payments."""
        async for payment in self.client.payments():
            print(f"  💰 {self.name} received {payment.payment.amount_sats} sats "
                  f"for seq #{payment.payment.seq_num}")

    async def stop(self):
        """Stop the provider."""
        if self.client:
            await self.client.stop()


class DatastreamSubscriber:
    """Represents a subscriber consuming multiple datastreams."""

    def __init__(self, name: str):
        self.name = name
        self.keys = Keys.generate()
        self.client = None
        self.subscriptions = {}  # stream_name -> metadata

    async def start(self, relay_urls: list[str]):
        """Start the subscriber."""
        config = SatoriNostrConfig(
            keys=self.keys.secret_key().to_hex(),
            relay_urls=relay_urls,
        )

        self.client = SatoriNostr(config)
        await self.client.start()

        print(f"✓ {self.name} online (Subscriber: {self.client.pubkey()[:16]}...)")

    async def discover_and_subscribe(self, tags: list[str]):
        """Discover streams and subscribe to them."""
        streams = await self.client.discover_datastreams(tags=tags, limit=10)

        print(f"  {self.name} found {len(streams)} stream(s) with tags {tags}")

        for stream in streams[:3]:  # Subscribe to first 3
            await self.client.subscribe_datastream(
                stream_name=stream.stream_name,
                provider_pubkey=stream.nostr_pubkey,
            )

            self.subscriptions[stream.stream_name] = stream
            print(f"  {self.name} subscribed to {stream.name}")

            # If paid stream, send initial payment
            if stream.price_per_obs > 0:
                await self.client.send_payment(
                    provider_pubkey=stream.nostr_pubkey,
                    stream_name=stream.stream_name,
                    seq_num=1,  # Pay for first observation
                    amount_sats=stream.price_per_obs,
                )
                print(f"  {self.name} sent payment for {stream.name}")

    async def consume(self, max_observations: int = 5):
        """Consume observations from subscribed streams."""
        count = 0

        async for inbound in self.client.observations():
            obs = inbound.observation
            stream = self.subscriptions.get(obs.stream_name)

            if stream:
                print(f"  📊 {self.name} received: {stream.name} seq #{obs.seq_num}")

                # Send payment for next observation if paid stream
                if stream.price_per_obs > 0:
                    await self.client.send_payment(
                        provider_pubkey=stream.nostr_pubkey,
                        stream_name=stream.stream_name,
                        seq_num=obs.seq_num + 1,
                        amount_sats=stream.price_per_obs,
                    )

                count += 1
                if count >= max_observations:
                    break

    async def stop(self):
        """Stop the subscriber."""
        if self.client:
            await self.client.stop()


async def run_marketplace():
    """Run a simulated datastream marketplace."""
    print("=" * 60)
    print("DATASTREAM MARKETPLACE SIMULATION")
    print("=" * 60)
    print()

    relay_urls = [
        "wss://relay.damus.io",
        "wss://nostr.wine",
    ]

    # Create providers
    providers = [
        DatastreamProvider(
            name="Bitcoin Price Feed",
            stream_name="btc-usd-coinbase",
            description="Real-time BTC/USD from Coinbase",
            price=10,  # 10 sats/observation
            tags=["bitcoin", "price", "usd", "crypto"]
        ),
        DatastreamProvider(
            name="Weather Station NYC",
            stream_name="weather-nyc",
            description="Live weather data from New York City",
            price=0,  # Free
            tags=["weather", "nyc", "free"]
        ),
        DatastreamProvider(
            name="Tech News Feed",
            stream_name="tech-news",
            description="Breaking technology news headlines",
            price=5,  # 5 sats/observation
            tags=["news", "tech", "headlines"]
        ),
    ]

    # Start providers
    print("PROVIDERS STARTING...")
    print("-" * 60)

    provider_metadata = {}
    for provider in providers:
        metadata = await provider.start(relay_urls)
        provider_metadata[provider] = metadata

        # Start payment monitoring
        asyncio.create_task(provider.monitor_payments())

    print()

    # Wait for announcements to propagate
    await asyncio.sleep(3)

    # Create subscribers
    subscribers = [
        DatastreamSubscriber("Crypto Trader Bot"),
        DatastreamSubscriber("Weather Analytics"),
        DatastreamSubscriber("News Aggregator"),
    ]

    # Start subscribers
    print("SUBSCRIBERS STARTING...")
    print("-" * 60)

    for subscriber in subscribers:
        await subscriber.start(relay_urls)

    print()

    # Wait for connections
    await asyncio.sleep(2)

    # Subscribers discover and subscribe
    print("DISCOVERY & SUBSCRIPTION...")
    print("-" * 60)

    await subscribers[0].discover_and_subscribe(tags=["bitcoin", "price"])
    await asyncio.sleep(1)

    await subscribers[1].discover_and_subscribe(tags=["weather"])
    await asyncio.sleep(1)

    await subscribers[2].discover_and_subscribe(tags=["news", "tech"])
    await asyncio.sleep(1)

    print()

    # Wait for subscriptions to propagate
    await asyncio.sleep(3)

    # Start publishing observations
    print("DATASTREAM ACTIVITY...")
    print("-" * 60)

    # Create background tasks for subscribers
    subscriber_tasks = [
        asyncio.create_task(sub.consume(max_observations=3))
        for sub in subscribers
    ]

    # Publish observations from all providers
    for round_num in range(5):
        print(f"\n[Round {round_num + 1}]")

        for provider in providers:
            metadata = provider_metadata[provider]
            num_sent = await provider.publish(metadata)
            print(f"  {provider.name} published → {num_sent} subscriber(s)")

        await asyncio.sleep(3)

    # Wait for subscribers to finish
    await asyncio.sleep(5)

    # Cleanup
    print()
    print("SHUTTING DOWN...")
    print("-" * 60)

    for task in subscriber_tasks:
        task.cancel()

    for provider in providers:
        await provider.stop()

    for subscriber in subscribers:
        await subscriber.stop()

    print("✓ Marketplace simulation complete")
    print()


if __name__ == "__main__":
    try:
        asyncio.run(run_marketplace())
    except KeyboardInterrupt:
        print("\n\nSimulation interrupted by user\n")
