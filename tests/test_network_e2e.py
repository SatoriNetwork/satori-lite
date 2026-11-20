"""End-to-end tests for the Satori network pipeline.

Two simulated neurons communicating through a real in-process Nostr relay:
- Neuron A: publishes a data stream (provider)
- Neuron B: discovers, subscribes, receives observations, predicts

Uses MiniRelay (no Docker), real SatoriNostr clients, real NetworkDB.
"""

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import time

import pytest
import pytest_asyncio
from nostr_sdk import Keys

# ── Import NetworkDB directly (skip __init__.py chain) ───────────────

_db_spec = importlib.util.spec_from_file_location(
    'network_db',
    os.path.join(os.path.dirname(__file__),
                 '..', 'neuron-lite', 'satorineuron', 'network_db.py'))
_db_mod = importlib.util.module_from_spec(_db_spec)
_db_spec.loader.exec_module(_db_mod)
NetworkDB = _db_mod.NetworkDB


# ── Import satorilib components ──────────────────────────────────────

# Add satorilib to path for clean imports
_satorilib_src = os.path.join(
    os.path.dirname(__file__), '..', '..', 'satorilib', 'src')
if _satorilib_src not in sys.path:
    sys.path.insert(0, _satorilib_src)

# Clear any mock versions of satorilib from sys.modules
# (pipeline tests may mock these at module level)
for _k in [k for k in list(sys.modules.keys())
           if k.startswith('satorilib')]:
    _mod = sys.modules[_k]
    if not hasattr(_mod, '__file__') and not hasattr(_mod, '__path__'):
        del sys.modules[_k]

from satorilib.satori_nostr import SatoriNostr, SatoriNostrConfig
from satorilib.satori_nostr.models import (
    DatastreamMetadata,
    DatastreamObservation,
    InboundObservation,
    KIND_DATASTREAM_ANNOUNCE,
    KIND_DATASTREAM_DATA,
)
# Import MiniRelay directly (avoid conflict with neuron's tests/ package)
_relay_spec = importlib.util.spec_from_file_location(
    'mini_relay',
    os.path.join(os.path.dirname(__file__),
                 '..', '..', 'satorilib', 'tests', 'mini_relay.py'))
_relay_mod = importlib.util.module_from_spec(_relay_spec)
_relay_spec.loader.exec_module(_relay_mod)
MiniRelay = _relay_mod.MiniRelay


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def relay():
    """Start a fresh MiniRelay for each test."""
    r = MiniRelay()
    await r.start()
    yield r
    await r.stop()


@pytest.fixture
def provider_keys():
    return Keys.generate()


@pytest.fixture
def subscriber_keys():
    return Keys.generate()


@pytest.fixture
def provider_db():
    with tempfile.TemporaryDirectory() as d:
        yield NetworkDB(os.path.join(d, 'provider.db'))


@pytest.fixture
def subscriber_db():
    with tempfile.TemporaryDirectory() as d:
        yield NetworkDB(os.path.join(d, 'subscriber.db'))


# ── Helpers ──────────────────────────────────────────────────────────

def make_config(keys: Keys, relay_url: str) -> SatoriNostrConfig:
    return SatoriNostrConfig(
        keys=keys.secret_key().to_hex(),
        relay_urls=[relay_url],
    )


def make_metadata(keys: Keys, stream_name: str,
                  cadence_seconds: int = 3600) -> DatastreamMetadata:
    return DatastreamMetadata(
        stream_name=stream_name,
        nostr_pubkey=keys.public_key().to_hex(),
        name=f'Test {stream_name}',
        description='Test stream',
        encrypted=False,
        price_per_obs=0,
        created_at=int(time.time()),
        cadence_seconds=cadence_seconds,
        tags=['test'],
    )


async def last_observation(client: SatoriNostr, provider: Keys,
                           stream_name: str) -> InboundObservation | None:
    """A provider's latest observation of its stream on the relay."""
    key = (provider.public_key().to_hex(), stream_name)
    events = await client.get_last_observation_events([key])
    return client.parse_observation_event(events[key]) if key in events else None


# ── Tests ────────────────────────────────────────────────────────────

class TestDiscovery:
    """Neuron discovers streams on a relay."""

    @pytest.mark.asyncio
    async def test_discover_announced_stream(
            self, relay, provider_keys, subscriber_keys):
        """Provider announces, subscriber discovers the stream."""
        stream_name = f'disc-{int(time.time())}'
        provider = SatoriNostr(make_config(provider_keys, relay.url))
        subscriber = SatoriNostr(make_config(subscriber_keys, relay.url))

        await provider.start()
        await subscriber.start()
        try:
            # Provider announces
            metadata = make_metadata(provider_keys, stream_name)
            await provider.announce_datastream(metadata)
            await asyncio.sleep(0.5)

            # Subscriber discovers
            streams = await subscriber.discover_datastreams()
            names = [s.stream_name for s in streams]
            assert stream_name in names

            found = next(s for s in streams if s.stream_name == stream_name)
            assert found.nostr_pubkey == provider_keys.public_key().to_hex()
            assert found.cadence_seconds == 3600
        finally:
            await provider.stop()
            await subscriber.stop()

    @pytest.mark.asyncio
    async def test_discover_saves_to_db(
            self, relay, provider_keys, subscriber_keys, subscriber_db):
        """Discovered streams can be saved to local NetworkDB."""
        stream_name = f'disc-db-{int(time.time())}'
        provider = SatoriNostr(make_config(provider_keys, relay.url))
        subscriber = SatoriNostr(make_config(subscriber_keys, relay.url))

        await provider.start()
        await subscriber.start()
        try:
            metadata = make_metadata(provider_keys, stream_name)
            await provider.announce_datastream(metadata)
            await asyncio.sleep(0.5)

            streams = await subscriber.discover_datastreams()
            found = next(s for s in streams if s.stream_name == stream_name)

            # Save to local DB (what the neuron does on subscribe)
            subscriber_db.subscribe(found.to_dict(), relay.url)
            subs = subscriber_db.get_active()
            assert len(subs) == 1
            assert subs[0]['stream_name'] == stream_name
            assert subs[0]['relay_url'] == relay.url
        finally:
            await provider.stop()
            await subscriber.stop()


class TestFreshnessCheck:
    """Neuron checks if a stream is active by querying latest observation."""

    @pytest.mark.asyncio
    async def test_fresh_observation_detected(
            self, relay, provider_keys, subscriber_keys, subscriber_db):
        """Stream with recent observation is detected as active."""
        stream_name = f'fresh-{int(time.time())}'
        provider = SatoriNostr(make_config(provider_keys, relay.url))
        subscriber = SatoriNostr(make_config(subscriber_keys, relay.url))

        await provider.start()
        await subscriber.start()
        try:
            metadata = make_metadata(provider_keys, stream_name)
            await provider.announce_datastream(metadata)

            # Provider publishes an observation
            obs = DatastreamObservation(
                stream_name=stream_name,
                timestamp=int(time.time()),
                value='42000',
                seq_num=1)
            await provider.publish_observation(obs, metadata)
            await asyncio.sleep(0.5)

            # Subscriber fetches latest observation
            last_obs = await last_observation(
                subscriber, provider_keys, stream_name)
            assert last_obs is not None
            assert last_obs.observation.value == '42000'
            assert last_obs.observation.seq_num == 1

            # Check freshness
            is_active = metadata.is_likely_active(last_obs.observation.timestamp)
            assert is_active is True

            # Save observation to DB (dedup by event_id)
            subscriber_db.save_observation(
                last_obs.stream_name,
                last_obs.nostr_pubkey,
                last_obs.observation.to_json(),
                last_obs.event_id,
                last_obs.observation.seq_num,
                last_obs.observation.timestamp)
            rows = subscriber_db.get_observations(
                stream_name, provider_keys.public_key().to_hex())
            assert len(rows) == 1
        finally:
            await provider.stop()
            await subscriber.stop()

    @pytest.mark.asyncio
    async def test_observation_dedup_on_refetch(
            self, relay, provider_keys, subscriber_keys, subscriber_db):
        """Fetching same observation twice doesn't create duplicates."""
        stream_name = f'dedup-{int(time.time())}'
        provider = SatoriNostr(make_config(provider_keys, relay.url))
        subscriber = SatoriNostr(make_config(subscriber_keys, relay.url))

        await provider.start()
        await subscriber.start()
        try:
            metadata = make_metadata(provider_keys, stream_name)
            await provider.announce_datastream(metadata)
            obs = DatastreamObservation(
                stream_name=stream_name,
                timestamp=int(time.time()),
                value='100',
                seq_num=1)
            await provider.publish_observation(obs, metadata)
            await asyncio.sleep(0.5)

            # Fetch and save twice
            for _ in range(2):
                last_obs = await last_observation(
                    subscriber, provider_keys, stream_name)
                subscriber_db.save_observation(
                    last_obs.stream_name,
                    last_obs.nostr_pubkey,
                    last_obs.observation.to_json(),
                    last_obs.event_id,
                    last_obs.observation.seq_num,
                    last_obs.observation.timestamp)

            rows = subscriber_db.get_observations(
                stream_name, provider_keys.public_key().to_hex())
            assert len(rows) == 1  # deduped by event_id
        finally:
            await provider.stop()
            await subscriber.stop()


class TestLiveSubscription:
    """Neuron subscribes and receives observations in real time."""

    @pytest.mark.asyncio
    async def test_receive_live_observation(
            self, relay, provider_keys, subscriber_keys, subscriber_db):
        """Subscribe to a stream, receive observation, save to DB."""
        stream_name = f'live-{int(time.time())}'
        provider = SatoriNostr(make_config(provider_keys, relay.url))
        subscriber = SatoriNostr(make_config(subscriber_keys, relay.url))

        await provider.start()
        await subscriber.start()
        try:
            metadata = make_metadata(provider_keys, stream_name)
            await provider.announce_datastream(metadata)

            # Subscribe
            await subscriber.subscribe_datastream(
                stream_name, provider_keys.public_key().to_hex())
            subscriber_db.subscribe(metadata.to_dict(), relay.url)
            await asyncio.sleep(1)

            # Provider publishes
            obs = DatastreamObservation(
                stream_name=stream_name,
                timestamp=int(time.time()),
                value='55000',
                seq_num=1)
            await provider.publish_observation(obs, metadata)

            # Subscriber receives
            received = None
            async for inbound in subscriber.observations():
                if inbound.stream_name == stream_name:
                    received = inbound
                    break

            assert received is not None
            assert received.observation.value == '55000'

            # Save to DB
            subscriber_db.save_observation(
                received.stream_name,
                received.nostr_pubkey,
                received.observation.to_json(),
                received.event_id,
                received.observation.seq_num,
                received.observation.timestamp)

            rows = subscriber_db.get_observations(
                stream_name, provider_keys.public_key().to_hex())
            assert len(rows) == 1
        finally:
            await provider.stop()
            await subscriber.stop()

    @pytest.mark.asyncio
    async def test_receive_multiple_observations(
            self, relay, provider_keys, subscriber_keys, subscriber_db):
        """Receive and save multiple sequential observations."""
        stream_name = f'multi-{int(time.time())}'
        provider = SatoriNostr(make_config(provider_keys, relay.url))
        subscriber = SatoriNostr(make_config(subscriber_keys, relay.url))

        await provider.start()
        await subscriber.start()
        try:
            metadata = make_metadata(provider_keys, stream_name)
            await provider.announce_datastream(metadata)
            await subscriber.subscribe_datastream(
                stream_name, provider_keys.public_key().to_hex())
            await asyncio.sleep(1)

            # Publish 3 observations
            for i in range(1, 4):
                obs = DatastreamObservation(
                    stream_name=stream_name,
                    timestamp=int(time.time()),
                    value=str(40000 + i * 1000),
                    seq_num=i)
                await provider.publish_observation(obs, metadata)
                await asyncio.sleep(0.3)

            # Collect observations with a timeout
            count = 0
            async for inbound in subscriber.observations():
                if inbound.stream_name == stream_name:
                    subscriber_db.save_observation(
                        inbound.stream_name,
                        inbound.nostr_pubkey,
                        inbound.observation.to_json(),
                        inbound.event_id,
                        inbound.observation.seq_num,
                        inbound.observation.timestamp)
                    count += 1
                    if count >= 3:
                        break

            rows = subscriber_db.get_observations(
                stream_name, provider_keys.public_key().to_hex())
            assert len(rows) == 3
        finally:
            await provider.stop()
            await subscriber.stop()


class TestPredictionPublishing:
    """Neuron receives observation, generates prediction, publishes it back."""

    @pytest.mark.asyncio
    async def test_predict_and_publish(
            self, relay, provider_keys, subscriber_keys,
            provider_db, subscriber_db):
        """Full cycle: observe → predict → publish prediction → provider sees it."""
        stream_name = f'pred-{int(time.time())}'
        pred_stream = stream_name + '_pred'
        provider = SatoriNostr(make_config(provider_keys, relay.url))
        subscriber = SatoriNostr(make_config(subscriber_keys, relay.url))

        await provider.start()
        await subscriber.start()
        try:
            # 1. Provider announces source stream
            metadata = make_metadata(provider_keys, stream_name)
            await provider.announce_datastream(metadata)

            # 2. Subscriber subscribes + sets up prediction
            await subscriber.subscribe_datastream(
                stream_name, provider_keys.public_key().to_hex())
            subscriber_db.subscribe(metadata.to_dict(), relay.url)
            subscriber_db.add_publication(
                pred_stream,
                source_stream_name=stream_name,
                source_provider_pubkey=provider_keys.public_key().to_hex())
            await asyncio.sleep(1)

            # 3. Provider publishes observation
            obs = DatastreamObservation(
                stream_name=stream_name,
                timestamp=int(time.time()),
                value='60000',
                seq_num=1)
            await provider.publish_observation(obs, metadata)

            # 4. Subscriber receives observation
            received = None
            async for inbound in subscriber.observations():
                if inbound.stream_name == stream_name:
                    received = inbound
                    break
            assert received is not None

            # 5. Save observation + generate prediction (echo engine)
            subscriber_db.save_observation(
                received.stream_name,
                received.nostr_pubkey,
                received.observation.to_json(),
                received.event_id,
                received.observation.seq_num,
                received.observation.timestamp)
            pred_id = subscriber_db.save_prediction(
                stream_name,
                provider_keys.public_key().to_hex(),
                value='60000',
                observation_seq=1,
                observed_at=received.observation.timestamp)

            # 6. Subscriber publishes prediction back to relay
            pred_metadata = make_metadata(
                subscriber_keys, pred_stream, cadence_seconds=3600)
            await subscriber.announce_datastream(pred_metadata)
            seq = subscriber_db.mark_published(pred_stream)
            pred_obs = DatastreamObservation(
                stream_name=pred_stream,
                timestamp=int(time.time()),
                value='60000',
                seq_num=seq)
            await subscriber.publish_observation(pred_obs, pred_metadata)
            subscriber_db.mark_prediction_published(pred_id)
            await asyncio.sleep(0.5)

            # 7. Provider can discover the prediction stream
            streams = await provider.discover_datastreams()
            pred_streams = [s for s in streams if s.stream_name == pred_stream]
            assert len(pred_streams) == 1

            # 8. Provider can fetch the prediction value
            last_pred = await last_observation(
                provider, subscriber_keys, pred_stream)
            assert last_pred is not None
            assert last_pred.observation.value == '60000'

            # Verify subscriber's local state
            preds = subscriber_db.get_predictions(
                stream_name, provider_keys.public_key().to_hex())
            assert len(preds) == 1
            assert preds[0]['published'] == 1

            pubs = subscriber_db.get_active_publications()
            pub = next(p for p in pubs if p['stream_name'] == pred_stream)
            assert pub['last_seq_num'] == 1
        finally:
            await provider.stop()
            await subscriber.stop()


class TestFullNeuronCycle:
    """Complete two-neuron interaction through a relay."""

    @pytest.mark.asyncio
    async def test_two_neurons_full_cycle(
            self, relay, provider_keys, subscriber_keys,
            provider_db, subscriber_db):
        """
        Neuron A publishes data → relay → Neuron B discovers, subscribes,
        receives, predicts, publishes prediction → relay → Neuron A sees it.
        """
        stream_name = f'full-{int(time.time())}'
        pred_stream = stream_name + '_pred'
        pub_hex = provider_keys.public_key().to_hex()
        sub_hex = subscriber_keys.public_key().to_hex()

        neuron_a = SatoriNostr(make_config(provider_keys, relay.url))
        neuron_b = SatoriNostr(make_config(subscriber_keys, relay.url))

        await neuron_a.start()
        await neuron_b.start()
        try:
            # ── Neuron A: setup publication ──
            provider_db.add_publication(stream_name, cadence_seconds=3600)
            metadata_a = make_metadata(provider_keys, stream_name)
            await neuron_a.announce_datastream(metadata_a)

            # ── Neuron B: discover ──
            await asyncio.sleep(0.5)
            streams = await neuron_b.discover_datastreams()
            found = next(s for s in streams if s.stream_name == stream_name)
            assert found.nostr_pubkey == pub_hex

            # ── Neuron B: check freshness (no obs yet) ──
            last = await last_observation(
                neuron_b, provider_keys, stream_name)
            assert last is None  # nothing published yet

            # ── Neuron A: publish first observation ──
            seq = provider_db.mark_published(stream_name)
            obs1 = DatastreamObservation(
                stream_name=stream_name,
                timestamp=int(time.time()),
                value='100',
                seq_num=seq)
            await neuron_a.publish_observation(obs1, metadata_a)

            # ── Neuron B: check freshness again (should find obs) ──
            await asyncio.sleep(0.5)
            last = await last_observation(
                neuron_b, provider_keys, stream_name)
            assert last is not None
            assert last.observation.value == '100'
            assert metadata_a.is_likely_active(last.observation.timestamp)

            # ── Neuron B: subscribe ──
            subscriber_db.subscribe(found.to_dict(), relay.url)
            subscriber_db.upsert_relay(relay.url)
            await neuron_b.subscribe_datastream(stream_name, pub_hex)

            # Save the observation from freshness check
            subscriber_db.save_observation(
                last.stream_name, last.nostr_pubkey,
                last.observation.to_json(), last.event_id,
                last.observation.seq_num, last.observation.timestamp)

            # ── Neuron B: start predicting ──
            subscriber_db.add_publication(
                pred_stream,
                source_stream_name=stream_name,
                source_provider_pubkey=pub_hex)
            pred_metadata = make_metadata(subscriber_keys, pred_stream)
            await neuron_b.announce_datastream(pred_metadata)
            await asyncio.sleep(1)

            # ── Neuron A: publish second observation ──
            seq2 = provider_db.mark_published(stream_name)
            obs2 = DatastreamObservation(
                stream_name=stream_name,
                timestamp=int(time.time()),
                value='200',
                seq_num=seq2)
            await neuron_a.publish_observation(obs2, metadata_a)

            # ── Neuron B: receive live observation ──
            # May also receive obs1 replayed from subscription; consume until obs2
            received = None
            async for inbound in neuron_b.observations():
                if (inbound.stream_name == stream_name
                        and inbound.observation.seq_num == seq2):
                    received = inbound
                    break
            assert received is not None
            assert received.observation.value == '200'

            # Save observation
            subscriber_db.save_observation(
                received.stream_name, received.nostr_pubkey,
                received.observation.to_json(), received.event_id,
                received.observation.seq_num, received.observation.timestamp)

            # Generate prediction (echo engine)
            pred_id = subscriber_db.save_prediction(
                stream_name, pub_hex,
                value='200',
                observation_seq=seq2,
                observed_at=received.observation.timestamp)

            # Publish prediction
            pred_seq = subscriber_db.mark_published(pred_stream)
            pred_obs = DatastreamObservation(
                stream_name=pred_stream,
                timestamp=int(time.time()),
                value='200',
                seq_num=pred_seq)
            await neuron_b.publish_observation(pred_obs, pred_metadata)
            subscriber_db.mark_prediction_published(pred_id)

            # ── Neuron A: fetch prediction ──
            await asyncio.sleep(0.5)
            pred_result = await last_observation(
                neuron_a, subscriber_keys, pred_stream)
            assert pred_result is not None
            assert pred_result.observation.value == '200'
            assert pred_result.nostr_pubkey == sub_hex

            # ── Verify local DB state ──
            # Neuron A: 2 publishes
            pubs_a = provider_db.get_active_publications()
            assert pubs_a[0]['last_seq_num'] == 2

            # Neuron B: 2 observations saved
            obs_rows = subscriber_db.get_observations(stream_name, pub_hex)
            assert len(obs_rows) == 2

            # Neuron B: 1 prediction saved and published
            preds = subscriber_db.get_predictions(stream_name, pub_hex)
            assert len(preds) == 1
            assert preds[0]['published'] == 1

            # Neuron B: subscription + relay in DB
            subs = subscriber_db.get_active()
            assert len(subs) == 1
            relays = subscriber_db.get_relays()
            assert len(relays) >= 1
        finally:
            await neuron_a.stop()
            await neuron_b.stop()
