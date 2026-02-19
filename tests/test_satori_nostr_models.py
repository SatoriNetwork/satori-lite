"""Tests for Satori Nostr data models."""
import json
import pytest
import time
from satorilib.satori_nostr.models import (
    DatastreamMetadata,
    DatastreamObservation,
    SubscriptionAnnouncement,
    PaymentNotification,
    InboundObservation,
    InboundPayment,
    SatoriNostrConfig,
    compute_stream_topic_tag,
)


class TestDatastreamMetadata:
    """Tests for DatastreamMetadata model."""

    def test_create_metadata(self):
        """Test creating datastream metadata."""
        metadata = DatastreamMetadata(
            stream_name="btc-price-usd",
            nostr_pubkey="abc123",
            name="Bitcoin Price (USD)",
            description="Real-time BTC/USD price from Coinbase",
            encrypted=True,
            price_per_obs=10,
            created_at=1234567890,
            cadence_seconds=3600,
            tags=["bitcoin", "price", "usd"],
        )

        assert metadata.stream_name == "btc-price-usd"
        assert metadata.nostr_pubkey == "abc123"
        assert metadata.name == "Bitcoin Price (USD)"
        assert metadata.encrypted is True
        assert metadata.price_per_obs == 10
        assert metadata.cadence_seconds == 3600
        assert len(metadata.tags) == 3

    def test_serialize_to_dict(self):
        """Test serializing metadata to dictionary."""
        metadata = DatastreamMetadata(
            stream_name="btc-price-usd",
            nostr_pubkey="abc123",
            name="Bitcoin Price",
            description="BTC/USD",
            encrypted=False,
            price_per_obs=0,
            created_at=1234567890,
            cadence_seconds=3600,
            tags=["bitcoin"],
        )

        data = metadata.to_dict()
        assert isinstance(data, dict)
        assert data["stream_name"] == "btc-price-usd"
        assert data["encrypted"] is False
        assert data["price_per_obs"] == 0
        assert data["cadence_seconds"] == 3600

    def test_deserialize_from_dict(self):
        """Test deserializing metadata from dictionary."""
        data = {
            "stream_name": "eth-price",
            "nostr_pubkey": "def456",
            "name": "ETH Price",
            "description": "Ethereum price",
            "encrypted": True,
            "price_per_obs": 5,
            "created_at": 1234567890,
            "cadence_seconds": 3600,
            "tags": ["ethereum", "price"],
        }

        metadata = DatastreamMetadata.from_dict(data)
        assert metadata.stream_name == "eth-price"
        assert metadata.encrypted is True
        assert metadata.cadence_seconds == 3600
        assert len(metadata.tags) == 2

    def test_json_round_trip(self):
        """Test JSON serialization round-trip."""
        original = DatastreamMetadata(
            stream_name="test-stream",
            nostr_pubkey="pubkey123",
            name="Test Stream",
            description="Testing",
            encrypted=False,
            price_per_obs=0,
            created_at=1234567890,
            cadence_seconds=None,
            tags=["test"],
        )

        json_str = original.to_json()
        restored = DatastreamMetadata.from_json(json_str)

        assert restored.stream_name == original.stream_name
        assert restored.nostr_pubkey == original.nostr_pubkey
        assert restored.encrypted == original.encrypted
        assert restored.tags == original.tags

    def test_is_likely_active_regular_cadence(self):
        """Test stream health check with regular cadence."""
        now = int(time.time())

        # Stream with hourly cadence
        metadata = DatastreamMetadata(
            stream_name="test",
            nostr_pubkey="abc",
            name="Test",
            description="Test",
            encrypted=False,
            price_per_obs=0,
            created_at=now - 7200,
            cadence_seconds=3600,  # Hourly
            tags=["test"],
        )

        # Last observation 30 minutes ago - should be active (30 min < 2 * 60 min)
        last_obs_30min_ago = now - 1800
        assert metadata.is_likely_active(last_obs_30min_ago) is True

        # Last observation 3 hours ago - should be stale (3 hours > 2 * 1 hour)
        last_obs_3h_ago = now - 10800
        assert metadata.is_likely_active(last_obs_3h_ago) is False

    def test_is_likely_active_irregular_cadence(self):
        """Test stream health check with irregular cadence."""
        now = int(time.time())

        # Stream with no fixed cadence
        metadata = DatastreamMetadata(
            stream_name="test",
            nostr_pubkey="abc",
            name="Test",
            description="Test",
            encrypted=False,
            price_per_obs=0,
            created_at=now - 172800,
            cadence_seconds=None,  # Irregular
            tags=["test"],
        )

        # Last observation 1 hour ago - should be active (1 hour < 24 hours)
        last_obs_1h_ago = now - 3600
        assert metadata.is_likely_active(last_obs_1h_ago) is True

        # Last observation 2 days ago - should be stale (2 days > 24 hours)
        last_obs_2d_ago = now - 172800
        assert metadata.is_likely_active(last_obs_2d_ago) is False

    def test_metadata_field_optional(self):
        """Test that metadata field is optional and defaults to None."""
        now = int(time.time())

        # Create without metadata
        stream = DatastreamMetadata(
            stream_name="test",
            nostr_pubkey="abc",
            name="Test",
            description="Test",
            encrypted=False,
            price_per_obs=0,
            created_at=now,
            cadence_seconds=3600,
            tags=["test"],
        )

        assert stream.metadata is None

    def test_metadata_field_with_source_info(self):
        """Test metadata field with source information."""
        now = int(time.time())

        metadata_dict = {
            "version": "1.0",
            "source": {
                "type": "api",
                "url": "https://api.example.com/data",
                "method": "GET"
            },
            "target": "price"
        }

        stream = DatastreamMetadata(
            stream_name="test",
            nostr_pubkey="abc",
            name="Test",
            description="Test",
            encrypted=False,
            price_per_obs=0,
            created_at=now,
            cadence_seconds=3600,
            tags=["test"],
            metadata=metadata_dict
        )

        assert stream.metadata is not None
        assert stream.metadata["version"] == "1.0"
        assert stream.metadata["source"]["url"] == "https://api.example.com/data"
        assert stream.metadata["target"] == "price"

    def test_metadata_field_with_lineage(self):
        """Test metadata field with data lineage information."""
        now = int(time.time())

        metadata_dict = {
            "version": "1.0",
            "lineage": {
                "original_source": "Coinbase API",
                "transformations": ["outlier_removal", "smoothing"],
                "quality_score": 0.98
            },
            "model": {
                "type": "LSTM",
                "accuracy": 0.85
            }
        }

        stream = DatastreamMetadata(
            stream_name="btc-prediction",
            nostr_pubkey="abc",
            name="Bitcoin Prediction",
            description="ML prediction",
            encrypted=True,
            price_per_obs=10,
            created_at=now,
            cadence_seconds=3600,
            tags=["bitcoin", "prediction"],
            metadata=metadata_dict
        )

        assert stream.metadata["lineage"]["original_source"] == "Coinbase API"
        assert "outlier_removal" in stream.metadata["lineage"]["transformations"]
        assert stream.metadata["model"]["type"] == "LSTM"

    def test_metadata_json_serialization(self):
        """Test that metadata field serializes/deserializes correctly."""
        now = int(time.time())

        original = DatastreamMetadata(
            stream_name="test",
            nostr_pubkey="abc",
            name="Test",
            description="Test",
            encrypted=False,
            price_per_obs=0,
            created_at=now,
            cadence_seconds=3600,
            tags=["test"],
            metadata={
                "version": "1.0",
                "custom_field": "custom_value",
                "nested": {
                    "key": "value"
                }
            }
        )

        # Serialize to JSON
        json_str = original.to_json()

        # Deserialize back
        restored = DatastreamMetadata.from_json(json_str)

        assert restored.metadata == original.metadata
        assert restored.metadata["custom_field"] == "custom_value"
        assert restored.metadata["nested"]["key"] == "value"

    def test_uuid_property(self):
        """Test that UUID property generates deterministic UUIDs."""
        now = int(time.time())

        metadata = DatastreamMetadata(
            stream_name="btc-price",
            nostr_pubkey="abc123",
            name="Bitcoin Price",
            description="Test",
            encrypted=False,
            price_per_obs=0,
            created_at=now,
            cadence_seconds=3600,
            tags=["test"]
        )

        # UUID should be a valid UUID string
        uuid_str = metadata.uuid
        assert isinstance(uuid_str, str)
        assert len(uuid_str) == 36  # Standard UUID format
        assert uuid_str.count('-') == 4  # UUID has 4 dashes

        # Should be deterministic - same input produces same UUID
        uuid2 = metadata.uuid
        assert uuid_str == uuid2

    def test_uuid_deterministic_across_instances(self):
        """Test that same pubkey+stream_name produces same UUID across instances."""
        now = int(time.time())

        metadata1 = DatastreamMetadata(
            stream_name="btc-price",
            nostr_pubkey="abc123",
            name="Bitcoin Price",
            description="Test 1",
            encrypted=False,
            price_per_obs=0,
            created_at=now,
            cadence_seconds=3600,
            tags=["test"]
        )

        metadata2 = DatastreamMetadata(
            stream_name="btc-price",
            nostr_pubkey="abc123",
            name="Bitcoin Price (Different Name)",  # Different name
            description="Test 2",  # Different description
            encrypted=True,  # Different encryption
            price_per_obs=10,  # Different price
            created_at=now + 1000,  # Different timestamp
            cadence_seconds=60,  # Different cadence
            tags=["different", "tags"]  # Different tags
        )

        # Same pubkey + stream_name should produce same UUID
        # even though all other fields are different
        assert metadata1.uuid == metadata2.uuid

    def test_uuid_different_for_different_streams(self):
        """Test that different pubkey or stream_name produces different UUID."""
        now = int(time.time())

        stream1 = DatastreamMetadata(
            stream_name="btc-price",
            nostr_pubkey="abc123",
            name="Test",
            description="Test",
            encrypted=False,
            price_per_obs=0,
            created_at=now,
            cadence_seconds=3600,
            tags=["test"]
        )

        # Different stream_name, same pubkey
        stream2 = DatastreamMetadata(
            stream_name="eth-price",  # Different stream_name
            nostr_pubkey="abc123",
            name="Test",
            description="Test",
            encrypted=False,
            price_per_obs=0,
            created_at=now,
            cadence_seconds=3600,
            tags=["test"]
        )

        # Same stream_name, different pubkey
        stream3 = DatastreamMetadata(
            stream_name="btc-price",
            nostr_pubkey="xyz789",  # Different pubkey
            name="Test",
            description="Test",
            encrypted=False,
            price_per_obs=0,
            created_at=now,
            cadence_seconds=3600,
            tags=["test"]
        )

        # All should have different UUIDs
        assert stream1.uuid != stream2.uuid
        assert stream1.uuid != stream3.uuid
        assert stream2.uuid != stream3.uuid


class TestDatastreamObservation:
    """Tests for DatastreamObservation model."""

    def test_create_observation(self):
        """Test creating an observation."""
        obs = DatastreamObservation(
            stream_name="btc-price-usd",
            timestamp=int(time.time()),
            value={"price": 45000.50, "volume": 123.45},
            seq_num=1,
        )

        assert obs.stream_name == "btc-price-usd"
        assert obs.seq_num == 1
        assert obs.value["price"] == 45000.50

    def test_observation_with_various_value_types(self):
        """Test observations with different value types."""
        # Dict value
        obs1 = DatastreamObservation(
            stream_name="test", timestamp=123, value={"a": 1}, seq_num=1
        )
        assert isinstance(obs1.value, dict)

        # Number value
        obs2 = DatastreamObservation(
            stream_name="test", timestamp=123, value=42.5, seq_num=2
        )
        assert isinstance(obs2.value, float)

        # String value
        obs3 = DatastreamObservation(
            stream_name="test", timestamp=123, value="hello", seq_num=3
        )
        assert isinstance(obs3.value, str)

        # List value
        obs4 = DatastreamObservation(
            stream_name="test", timestamp=123, value=[1, 2, 3], seq_num=4
        )
        assert isinstance(obs4.value, list)

    def test_json_round_trip(self):
        """Test JSON serialization round-trip."""
        original = DatastreamObservation(
            stream_name="test-stream",
            timestamp=1234567890,
            value={"price": 100, "volume": 50},
            seq_num=42,
        )

        json_str = original.to_json()
        restored = DatastreamObservation.from_json(json_str)

        assert restored.stream_name == original.stream_name
        assert restored.timestamp == original.timestamp
        assert restored.value == original.value
        assert restored.seq_num == original.seq_num


class TestSubscriptionAnnouncement:
    """Tests for SubscriptionAnnouncement model."""

    def test_create_subscription(self):
        """Test creating a subscription announcement."""
        sub = SubscriptionAnnouncement(
            subscriber_pubkey="sub123",
            stream_name="btc-price-usd",
            nostr_pubkey="provider456",
            timestamp=int(time.time()),
            payment_channel="lightning:channel123",
        )

        assert sub.subscriber_pubkey == "sub123"
        assert sub.stream_name == "btc-price-usd"
        assert sub.nostr_pubkey == "provider456"
        assert sub.payment_channel == "lightning:channel123"

    def test_subscription_without_payment_channel(self):
        """Test subscription without payment channel (free stream)."""
        sub = SubscriptionAnnouncement(
            subscriber_pubkey="sub123",
            stream_name="free-stream",
            nostr_pubkey="provider456",
            timestamp=1234567890,
        )

        assert sub.payment_channel is None

    def test_json_round_trip(self):
        """Test JSON serialization round-trip."""
        original = SubscriptionAnnouncement(
            subscriber_pubkey="sub",
            stream_name="stream",
            nostr_pubkey="provider",
            timestamp=1234567890,
            payment_channel="lightning:xyz",
        )

        json_str = original.to_json()
        restored = SubscriptionAnnouncement.from_json(json_str)

        assert restored.subscriber_pubkey == original.subscriber_pubkey
        assert restored.stream_name == original.stream_name
        assert restored.payment_channel == original.payment_channel


class TestPaymentNotification:
    """Tests for PaymentNotification model."""

    def test_create_payment(self):
        """Test creating a payment notification."""
        payment = PaymentNotification(
            from_pubkey="subscriber123",
            to_pubkey="provider456",
            stream_name="btc-price-usd",
            seq_num=42,
            amount_sats=10,
            timestamp=int(time.time()),
            tx_id="lightning:tx789",
        )

        assert payment.from_pubkey == "subscriber123"
        assert payment.to_pubkey == "provider456"
        assert payment.stream_name == "btc-price-usd"
        assert payment.seq_num == 42
        assert payment.amount_sats == 10
        assert payment.tx_id == "lightning:tx789"

    def test_payment_without_tx_id(self):
        """Test payment without transaction ID."""
        payment = PaymentNotification(
            from_pubkey="sub",
            to_pubkey="provider",
            stream_name="stream",
            seq_num=1,
            amount_sats=5,
            timestamp=123,
        )

        assert payment.tx_id is None

    def test_json_round_trip(self):
        """Test JSON serialization round-trip."""
        original = PaymentNotification(
            from_pubkey="sub",
            to_pubkey="provider",
            stream_name="stream",
            seq_num=10,
            amount_sats=100,
            timestamp=1234567890,
            tx_id="tx123",
        )

        json_str = original.to_json()
        restored = PaymentNotification.from_json(json_str)

        assert restored.from_pubkey == original.from_pubkey
        assert restored.seq_num == original.seq_num
        assert restored.amount_sats == original.amount_sats
        assert restored.tx_id == original.tx_id


class TestInboundModels:
    """Tests for inbound message wrapper models."""

    def test_inbound_observation(self):
        """Test InboundObservation wrapper."""
        obs = DatastreamObservation(
            stream_name="test",
            timestamp=123,
            value={"data": 42},
            seq_num=1,
        )

        inbound = InboundObservation(
            stream_name="test",
            nostr_pubkey="provider123",
            observation=obs,
            event_id="event456",
        )

        assert inbound.stream_name == "test"
        assert inbound.nostr_pubkey == "provider123"
        assert inbound.observation.value["data"] == 42
        assert inbound.event_id == "event456"
        assert inbound.raw_event is None

    def test_inbound_payment(self):
        """Test InboundPayment wrapper."""
        payment = PaymentNotification(
            from_pubkey="sub",
            to_pubkey="provider",
            stream_name="stream",
            seq_num=5,
            amount_sats=10,
            timestamp=123,
        )

        inbound = InboundPayment(
            payment=payment,
            event_id="event789",
            raw_event={"kind": 34603},
        )

        assert inbound.payment.seq_num == 5
        assert inbound.event_id == "event789"
        assert inbound.raw_event["kind"] == 34603


class TestSatoriNostrConfig:
    """Tests for SatoriNostrConfig."""

    def test_create_config_minimal(self):
        """Test creating config with minimal required fields."""
        config = SatoriNostrConfig(
            keys="nsec1...",
            relay_urls=["wss://relay.damus.io"],
        )

        assert config.keys == "nsec1..."
        assert len(config.relay_urls) == 1
        assert config.active_relay_timeout_ms == 8000  # default
        assert config.dedupe_db_path is None  # default

    def test_create_config_full(self):
        """Test creating config with all fields."""
        config = SatoriNostrConfig(
            keys="nsec1...",
            relay_urls=["wss://relay1.io", "wss://relay2.io"],
            active_relay_timeout_ms=5000,
            dedupe_db_path="/tmp/dedupe.db",
        )

        assert len(config.relay_urls) == 2
        assert config.active_relay_timeout_ms == 5000
        assert config.dedupe_db_path == "/tmp/dedupe.db"


class TestUtilityFunctions:
    """Tests for utility functions."""

    def test_compute_stream_topic_tag(self):
        """Test computing stream topic tags."""
        tag1 = compute_stream_topic_tag("btc-price-usd")
        assert tag1 == "satori:stream:btc-price-usd"

        tag2 = compute_stream_topic_tag("eth-price")
        assert tag2 == "satori:stream:eth-price"

        # Different streams have different tags
        assert tag1 != tag2

    def test_topic_tag_format(self):
        """Test topic tag has correct format."""
        tag = compute_stream_topic_tag("my-stream")
        assert tag.startswith("satori:stream:")
        assert "my-stream" in tag
