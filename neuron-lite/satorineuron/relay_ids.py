"""Deterministic stream identity for relay (Nostr) prediction streams.

A relay stream is keyed by ``(stream_name, provider_pubkey)``. To run it through
the heavy engine's ``StreamModel`` (which is keyed by a stream uuid and persists
its trained model under that uuid, see ``StreamModel.modelPath``), we need a uuid
that is:

  - **deterministic and stable across restarts** so the model file and the
    ``StreamStore`` observation history survive a restart, and
  - **collision-free with the central path's uuids**.

Both guarantees come from reusing ``StreamId.generateUUID`` (which seeds
``uuid5(NAMESPACE_DNS, "source:author:stream:target")``) with a relay-specific
source. The central path mints ids with ``source='central-lite'``; we use
``source='satori-relay'``, so the seed strings can never coincide and the uuids
can never collide. The provider pubkey goes in the natural ``author`` slot.

The source string and field layout are FROZEN. Changing either would orphan every
relay model's joblib file and its ``StreamStore`` history (a silent full retrain
and loss of accumulated observations). The frozen values are pinned in
``tests/test_relay_ids.py``.
"""

from satorilib.concepts.structs import StreamId

# Frozen forever. Distinct from the central path's 'central-lite' source, which
# is exactly what guarantees relay uuids never collide with central uuids.
RELAY_SOURCE = 'satori-relay'

# Suffix appended to the stream name for the prediction (publication) stream,
# matching the relay path's existing '{stream}_pred' channel convention.
PRED_SUFFIX = '_pred'


def _canonical_pubkey(provider_pubkey) -> str:
    """Canonicalize a provider pubkey so one provider maps to exactly one uuid.

    Relay providers are identified by 64-char lowercase-hex Nostr pubkeys; we
    strip and lowercase to absorb incidental casing/whitespace differences so the
    same provider always seeds the same uuid. The result must stay stable for a
    given provider forever.
    """
    if not provider_pubkey:
        return ''
    return str(provider_pubkey).strip().lower()


def relay_stream_ids(stream_name: str, provider_pubkey) -> tuple[StreamId, StreamId]:
    """Return ``(subscriptionStreamId, predictionStreamId)`` for a relay stream.

    The subscription id identifies the incoming stream; the prediction id (stream
    name suffixed ``_pred``) identifies where predictions are published and drives
    the engine's per-model file path. Both are deterministic and stable.
    """
    author = _canonical_pubkey(provider_pubkey)
    sub = StreamId(
        source=RELAY_SOURCE, author=author, stream=stream_name, target='')
    pub = StreamId(
        source=RELAY_SOURCE, author=author,
        stream=f'{stream_name}{PRED_SUFFIX}', target='')
    return sub, pub


def relay_uuid(stream_name: str, provider_pubkey) -> str:
    """Deterministic subscription-stream uuid for a relay stream."""
    sub, _ = relay_stream_ids(stream_name, provider_pubkey)
    return sub.uuid


def relay_prediction_uuid(stream_name: str, provider_pubkey) -> str:
    """Deterministic prediction-stream uuid for a relay stream.

    This drives ``StreamModel.modelPath`` (where the joblib model is persisted),
    so it must be as stable as ``relay_uuid``.
    """
    _, pub = relay_stream_ids(stream_name, provider_pubkey)
    return pub.uuid
