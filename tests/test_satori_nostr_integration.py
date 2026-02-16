"""Integration tests for Satori Nostr full provider/subscriber flow.

These tests simulate real-world scenarios with multiple clients.
"""
import asyncio
import pytest
import time
from nostr_sdk import Keys

from satorilib.satori_nostr import (
    SatoriNostr,
    SatoriNostrConfig,
    DatastreamMetadata,
    DatastreamObservation,
    PaymentNotification,
)


@pytest.fixture
def provider_config():
    """Create provider client configuration."""
    keys = Keys.generate()
    return SatoriNostrConfig(
        keys=keys.secret_key().to_hex(),
        relay_urls=["wss://relay.damus.io"],
    )


@pytest.fixture
def subscriber_config():
    """Create subscriber client configuration."""
    keys = Keys.generate()
    return SatoriNostrConfig(
        keys=keys.secret_key().to_hex(),
        relay_urls=["wss://relay.damus.io"],
    )


class TestProviderSubscriberFlow:
    """Test full provider/subscriber interaction flow."""

    @pytest.mark.asyncio
    async def test_announce_and_discover(self, provider_config, subscriber_config):
        """Test that subscribers can discover announced datastreams."""
        provider = SatoriNostr(provider_config)
        subscriber = SatoriNostr(subscriber_config)

        try:
            await provider.start()
            await subscriber.start()

            # Provider announces a datastream
            now = int(time.time())
            metadata = DatastreamMetadata(
                stream_id="test-stream-1",
                neuron_pubkey=provider.pubkey(),
                name="Test Stream",
                description="Integration test stream",
                encrypted=False,
                price_per_obs=0,  # Free stream
                created_at=now,
                cadence_seconds=3600,
                tags=["test", "integration"],
            )

            event_id = await provider.announce_datastream(metadata)
            assert event_id is not None
            assert len(event_id) == 64  # Hex event ID

            # Wait for relay propagation
            await asyncio.sleep(2)

            # Subscriber discovers the stream
            streams = await subscriber.discover_datastreams(tags=["test"])

            # Should find at least our stream (might find others too)
            found = any(s.stream_id == "test-stream-1" for s in streams)
            assert found, "Announced stream should be discoverable"

        finally:
            await provider.stop()
            await subscriber.stop()

    @pytest.mark.asyncio
    async def test_subscription_flow(self, provider_config, subscriber_config):
        """Test subscription announcement and tracking."""
        provider = SatoriNostr(provider_config)
        subscriber = SatoriNostr(subscriber_config)

        try:
            await provider.start()
            await subscriber.start()

            # Subscriber subscribes to a stream
            event_id = await subscriber.subscribe_datastream(
                stream_id="test-stream-2",
                provider_pubkey=provider.pubkey(),
                payment_channel="lightning:test123",
            )

            assert event_id is not None

            # Wait for relay propagation
            await asyncio.sleep(2)

            # Provider should see the subscription
            # (In real implementation, this would be tracked via event listener)
            # For now, manually record it
            provider.record_subscription(
                stream_id="test-stream-2",
                subscriber_pubkey=subscriber.pubkey(),
                payment_channel="lightning:test123",
            )

            subscribers = provider.get_subscribers("test-stream-2")
            assert subscriber.pubkey() in subscribers

        finally:
            await provider.stop()
            await subscriber.stop()

    @pytest.mark.asyncio
    async def test_free_stream_observation_delivery(self, provider_config, subscriber_config):
        """Test observation delivery for free streams (no payment required)."""
        provider = SatoriNostr(provider_config)
        subscriber = SatoriNostr(subscriber_config)

        try:
            await provider.start()
            await subscriber.start()

            # Setup: subscriber subscribes
            provider.record_subscription(
                stream_id="free-stream",
                subscriber_pubkey=subscriber.pubkey(),
            )

            # Create metadata for free stream
            now = int(time.time())
            metadata = DatastreamMetadata(
                stream_id="free-stream",
                neuron_pubkey=provider.pubkey(),
                name="Free Stream",
                description="No payment required",
                encrypted=True,
                price_per_obs=0,  # Free!
                created_at=now,
                cadence_seconds=60,
                tags=["free"],
            )

            # Publish observation
            observation = DatastreamObservation(
                stream_id="free-stream",
                timestamp=int(time.time()),
                value={"data": "test_value"},
                seq_num=1,
            )

            event_ids = await provider.publish_observation(observation, metadata)

            # Should have sent to subscriber
            assert len(event_ids) == 1

            # Wait for delivery
            await asyncio.sleep(2)

            # Note: Subscriber would receive via observations() iterator in real usage
            # This test verifies the event was published

        finally:
            await provider.stop()
            await subscriber.stop()

    @pytest.mark.asyncio
    async def test_paid_stream_with_payment(self, provider_config, subscriber_config):
        """Test paid stream: payment required before observation delivery."""
        provider = SatoriNostr(provider_config)
        subscriber = SatoriNostr(subscriber_config)

        try:
            await provider.start()
            await subscriber.start()

            # Setup: subscriber subscribes
            provider.record_subscription(
                stream_id="paid-stream",
                subscriber_pubkey=subscriber.pubkey(),
            )

            # Create metadata for paid stream
            now = int(time.time())
            metadata = DatastreamMetadata(
                stream_id="paid-stream",
                neuron_pubkey=provider.pubkey(),
                name="Paid Stream",
                description="Requires payment",
                encrypted=True,
                price_per_obs=10,  # 10 sats per observation
                created_at=now,
                cadence_seconds=60,
                tags=["paid"],
            )

            # Observation to publish
            observation = DatastreamObservation(
                stream_id="paid-stream",
                timestamp=int(time.time()),
                value={"price": 45000},
                seq_num=1,
            )

            # Try to publish without payment - should not send to subscriber
            event_ids = await provider.publish_observation(observation, metadata)
            assert len(event_ids) == 0, "Should not send without payment"

            # Subscriber sends payment for seq 1
            await subscriber.send_payment(
                provider_pubkey=provider.pubkey(),
                stream_id="paid-stream",
                seq_num=1,
                amount_sats=10,
            )

            # Wait for payment to be received
            await asyncio.sleep(2)

            # Provider records payment (in real usage, this happens via event listener)
            provider.record_payment(
                stream_id="paid-stream",
                subscriber_pubkey=subscriber.pubkey(),
                seq_num=1,
            )

            # Now publish again - should send to subscriber
            event_ids = await provider.publish_observation(observation, metadata)
            assert len(event_ids) == 1, "Should send after payment"

        finally:
            await provider.stop()
            await subscriber.stop()

    @pytest.mark.asyncio
    async def test_multiple_subscribers(self, provider_config):
        """Test observation delivery to multiple subscribers."""
        provider = SatoriNostr(provider_config)

        # Create 3 subscribers
        sub1_keys = Keys.generate()
        sub2_keys = Keys.generate()
        sub3_keys = Keys.generate()

        sub1_pubkey = sub1_keys.public_key().to_hex()
        sub2_pubkey = sub2_keys.public_key().to_hex()
        sub3_pubkey = sub3_keys.public_key().to_hex()

        try:
            await provider.start()

            # All subscribe to the same stream
            provider.record_subscription("multi-stream", sub1_pubkey)
            provider.record_subscription("multi-stream", sub2_pubkey)
            provider.record_subscription("multi-stream", sub3_pubkey)

            # Record payments for sub1 and sub2 (sub3 doesn't pay)
            provider.record_payment("multi-stream", sub1_pubkey, seq_num=1)
            provider.record_payment("multi-stream", sub2_pubkey, seq_num=1)

            # Create paid stream metadata
            now = int(time.time())
            metadata = DatastreamMetadata(
                stream_id="multi-stream",
                neuron_pubkey=provider.pubkey(),
                name="Multi-Subscriber Stream",
                description="Testing multiple subscribers",
                encrypted=True,
                price_per_obs=5,
                created_at=now,
                cadence_seconds=60,
                tags=["multi"],
            )

            # Publish observation
            observation = DatastreamObservation(
                stream_id="multi-stream",
                timestamp=int(time.time()),
                value={"data": "multi_test"},
                seq_num=1,
            )

            event_ids = await provider.publish_observation(observation, metadata)

            # Should send to 2 subscribers (sub1 and sub2 paid, sub3 didn't)
            assert len(event_ids) == 2

        finally:
            await provider.stop()

    @pytest.mark.asyncio
    async def test_subscriber_state_tracking(self, provider_config):
        """Test that provider correctly tracks subscriber state."""
        provider = SatoriNostr(provider_config)

        try:
            await provider.start()

            sub_pubkey = Keys.generate().public_key().to_hex()

            # Record subscription
            provider.record_subscription(
                stream_id="state-stream",
                subscriber_pubkey=sub_pubkey,
                payment_channel="lightning:xyz",
            )

            # Check subscriber exists
            subscribers = provider.get_subscribers("state-stream")
            assert sub_pubkey in subscribers

            # Record payment for seq 5
            provider.record_payment("state-stream", sub_pubkey, seq_num=5)

            # Record higher payment (seq 10) - should update
            provider.record_payment("state-stream", sub_pubkey, seq_num=10)

            # Record lower payment (seq 3) - should not override
            provider.record_payment("state-stream", sub_pubkey, seq_num=3)

            # Verify state (last_paid_seq should be 10)
            state = provider._subscribers["state-stream"][sub_pubkey]
            assert state.last_paid_seq == 10

        finally:
            await provider.stop()

    @pytest.mark.asyncio
    async def test_sparse_data_handling(self, provider_config, subscriber_config):
        """Test handling of sparse datastreams (long gaps between observations)."""
        provider = SatoriNostr(provider_config)
        subscriber = SatoriNostr(subscriber_config)

        try:
            await provider.start()
            await subscriber.start()

            # Setup subscription
            provider.record_subscription(
                stream_id="sparse-stream",
                subscriber_pubkey=subscriber.pubkey(),
            )

            now = int(time.time())
            metadata = DatastreamMetadata(
                stream_id="sparse-stream",
                neuron_pubkey=provider.pubkey(),
                name="Sparse Stream",
                description="Infrequent observations",
                encrypted=True,
                price_per_obs=0,
                created_at=now,
                cadence_seconds=86400,  # Daily sparse stream
                tags=["sparse"],
            )

            # Publish observations with large sequence number gaps
            # (simulating hours/days between observations)
            for seq_num in [1, 100, 500, 1000]:
                observation = DatastreamObservation(
                    stream_id="sparse-stream",
                    timestamp=int(time.time()),
                    value={"seq": seq_num},
                    seq_num=seq_num,
                )

                event_ids = await provider.publish_observation(observation, metadata)
                assert len(event_ids) == 1

                # Small delay for relay
                await asyncio.sleep(0.5)

        finally:
            await provider.stop()
            await subscriber.stop()


class TestPaymentFlow:
    """Test payment-related functionality."""

    @pytest.mark.asyncio
    async def test_payment_notification_structure(self, subscriber_config):
        """Test payment notification has correct structure."""
        subscriber = SatoriNostr(subscriber_config)

        try:
            await subscriber.start()

            provider_pubkey = Keys.generate().public_key().to_hex()

            event_id = await subscriber.send_payment(
                provider_pubkey=provider_pubkey,
                stream_id="test-stream",
                seq_num=42,
                amount_sats=10,
                tx_id="lightning:test_tx_123",
            )

            assert event_id is not None
            assert len(event_id) == 64  # Hex event ID

        finally:
            await subscriber.stop()

    @pytest.mark.asyncio
    async def test_payment_without_tx_id(self, subscriber_config):
        """Test payment notification without transaction ID."""
        subscriber = SatoriNostr(subscriber_config)

        try:
            await subscriber.start()

            provider_pubkey = Keys.generate().public_key().to_hex()

            event_id = await subscriber.send_payment(
                provider_pubkey=provider_pubkey,
                stream_id="test-stream",
                seq_num=1,
                amount_sats=5,
                tx_id=None,  # No transaction ID
            )

            assert event_id is not None

        finally:
            await subscriber.stop()


class TestErrorHandling:
    """Test error handling and edge cases."""

    @pytest.mark.asyncio
    async def test_start_already_running(self, provider_config):
        """Test that starting an already-running client raises error."""
        client = SatoriNostr(provider_config)

        try:
            await client.start()

            with pytest.raises(RuntimeError, match="already running"):
                await client.start()

        finally:
            await client.stop()

    @pytest.mark.asyncio
    async def test_stop_not_running(self, provider_config):
        """Test that stopping a non-running client raises error."""
        client = SatoriNostr(provider_config)

        with pytest.raises(RuntimeError, match="not running"):
            await client.stop()

    @pytest.mark.asyncio
    async def test_operations_require_running_client(self, provider_config):
        """Test that operations fail when client not running."""
        client = SatoriNostr(provider_config)

        now = int(time.time())
        metadata = DatastreamMetadata(
            stream_id="test",
            neuron_pubkey=client.pubkey(),
            name="Test",
            description="Test",
            encrypted=False,
            price_per_obs=0,
            created_at=now,
            cadence_seconds=3600,
            tags=[],
        )

        with pytest.raises(RuntimeError, match="not running"):
            await client.announce_datastream(metadata)

        with pytest.raises(RuntimeError, match="not running"):
            await client.discover_datastreams()

    @pytest.mark.asyncio
    async def test_nonexistent_stream_subscribers(self, provider_config):
        """Test getting subscribers for non-existent stream."""
        client = SatoriNostr(provider_config)

        subscribers = client.get_subscribers("nonexistent-stream")
        assert subscribers == []


class TestClientBasics:
    """Test basic client functionality."""

    def test_client_pubkey(self, provider_config):
        """Test getting client public key."""
        client = SatoriNostr(provider_config)

        pubkey = client.pubkey()
        assert isinstance(pubkey, str)
        assert len(pubkey) == 64  # Hex pubkey

    def test_is_running_states(self, provider_config):
        """Test is_running in different states."""
        client = SatoriNostr(provider_config)

        assert not client.is_running()

    @pytest.mark.asyncio
    async def test_client_lifecycle(self, provider_config):
        """Test client start/stop lifecycle."""
        client = SatoriNostr(provider_config)

        assert not client.is_running()

        await client.start()
        assert client.is_running()

        await client.stop()
        assert not client.is_running()
