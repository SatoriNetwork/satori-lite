"""Freeze tests for relay stream identity (`satorineuron.relay_ids`).

These uuids key the engine's per-model file (`StreamModel.modelPath`) and the
`StreamStore` observation history. If they ever change for a given
`(stream_name, provider_pubkey)`, a node silently loses its trained model and its
accumulated observations. So the exact values are PINNED here as literals — if a
change to the seed shape breaks these, that is the intended early warning, not a
test to "fix" by recomputing.
"""

import uuid as _uuid

from satorineuron.relay_ids import (
    relay_uuid,
    relay_prediction_uuid,
    relay_stream_ids,
    RELAY_SOURCE,
    _canonical_pubkey,
)
from satorilib.concepts.structs import StreamId


PROVIDER = '62b18453c2e2d89ebe5c7c91f17360ae2bcae17e58ace0ab33faa9c03c17633f'
STREAM = 'bitcoin_price'

# Pinned literals. Do NOT update casually — see module docstring.
FROZEN_SUB_UUID = 'e6673aad-177a-5f82-b16a-f045d2bc246b'
FROZEN_PRED_UUID = 'eb895a20-c025-5995-a7d7-daf9371e1b03'


def test_relay_uuid_is_frozen():
    assert relay_uuid(STREAM, PROVIDER) == FROZEN_SUB_UUID


def test_relay_prediction_uuid_is_frozen():
    assert relay_prediction_uuid(STREAM, PROVIDER) == FROZEN_PRED_UUID


def test_relay_uuid_is_deterministic():
    assert relay_uuid(STREAM, PROVIDER) == relay_uuid(STREAM, PROVIDER)


def test_pubkey_casing_and_whitespace_canonicalized():
    # Same provider in a different casing / with surrounding whitespace must map
    # to the same uuid, otherwise one provider could spawn two model dirs.
    assert relay_uuid(STREAM, PROVIDER) == relay_uuid(STREAM, f'  {PROVIDER.upper()}  ')
    assert _canonical_pubkey(f'  {PROVIDER.upper()}  ') == PROVIDER


def test_distinct_streams_and_providers_differ():
    other_provider = (
        'f8a391de126d441965b0598e2c819878f68ae7eee1742fa477f410b0bcb17d8e')
    assert relay_uuid(STREAM, PROVIDER) != relay_uuid('other_stream', PROVIDER)
    assert relay_uuid(STREAM, PROVIDER) != relay_uuid(STREAM, other_provider)


def test_prediction_stream_id_suffix_and_distinctness():
    sub, pub = relay_stream_ids(STREAM, PROVIDER)
    assert sub.stream == STREAM
    assert pub.stream == f'{STREAM}_pred'
    assert sub.uuid != pub.uuid
    assert sub.uuid == FROZEN_SUB_UUID
    assert pub.uuid == FROZEN_PRED_UUID


def test_no_collision_with_central_source():
    # Central uses source='central-lite'; relay uses 'satori-relay'. The same
    # stream name on each path must NOT produce the same uuid.
    central = StreamId(
        source='central-lite', author='satori', stream=STREAM, target='')
    sub, _ = relay_stream_ids(STREAM, PROVIDER)
    assert sub.uuid != central.uuid
    assert RELAY_SOURCE != 'central-lite'


def test_empty_provider_is_handled():
    # A missing provider must not raise; it just canonicalizes to ''.
    assert isinstance(relay_uuid(STREAM, ''), str)
    assert isinstance(relay_uuid(STREAM, None), str)
