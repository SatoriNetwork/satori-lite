"""Satori Nostr - Datastream pub/sub with micropayments over Nostr."""

__version__ = "0.1.0"

from .client import SatoriNostr, SubscriberState
from .models import (
    DatastreamMetadata,
    DatastreamObservation,
    SubscriptionAnnouncement,
    PaymentNotification,
    InboundObservation,
    InboundPayment,
    SatoriNostrConfig,
    KIND_DATASTREAM_ANNOUNCE,
    KIND_DATASTREAM_DATA,
    KIND_SUBSCRIPTION_ANNOUNCE,
    KIND_PAYMENT,
    CADENCE_REALTIME,
    CADENCE_MINUTE,
    CADENCE_5MIN,
    CADENCE_HOURLY,
    CADENCE_DAILY,
    CADENCE_WEEKLY,
    CADENCE_IRREGULAR,
    compute_stream_topic_tag,
)
from .dedupe import DedupeCache
from .encryption import (
    encrypt_json,
    decrypt_json,
    encrypt_observation,
    decrypt_observation,
    encrypt_payment,
    decrypt_payment,
    EncryptionError,
)

# Import integrations submodule (production-ready reliability layer)
from . import integrations

__all__ = [
    # Main Client
    "SatoriNostr",
    "SubscriberState",

    # Models
    "DatastreamMetadata",
    "DatastreamObservation",
    "SubscriptionAnnouncement",
    "PaymentNotification",
    "InboundObservation",
    "InboundPayment",
    "SatoriNostrConfig",

    # Event Kind Constants
    "KIND_DATASTREAM_ANNOUNCE",
    "KIND_DATASTREAM_DATA",
    "KIND_SUBSCRIPTION_ANNOUNCE",
    "KIND_PAYMENT",

    # Cadence Constants
    "CADENCE_REALTIME",
    "CADENCE_MINUTE",
    "CADENCE_5MIN",
    "CADENCE_HOURLY",
    "CADENCE_DAILY",
    "CADENCE_WEEKLY",
    "CADENCE_IRREGULAR",

    # Functions
    "compute_stream_topic_tag",

    # Utilities
    "DedupeCache",
    "encrypt_json",
    "decrypt_json",
    "encrypt_observation",
    "decrypt_observation",
    "encrypt_payment",
    "decrypt_payment",
    "EncryptionError",
]
