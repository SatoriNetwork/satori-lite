"""Tests for the network processing pipeline.

Tests the async methods in StartupDag that handle observation processing,
freshness checking, data source fetching, engine, and publishing.

Strategy: import start.py for StartupDag's real methods, with only the AI
engine stubbed. Create instances via object.__new__ to skip __init__, then
manually set just the attributes needed for testing.
"""

import asyncio
import json
import os
import sys
import tempfile
import time
import types
import unittest.mock as mock

import pytest

from satorilib.satori_nostr.models import (
    DatastreamMetadata, DatastreamObservation, InboundObservation)
from satorineuron.network_db import NetworkDB

from tests.neuron_start import start

StartupDag = start.StartupDag


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def harness(monkeypatch):
    """Create a StartupDag instance with real NetworkDB and mock clients.

    Uses object.__new__ to skip __init__ (which needs wallet, config, etc.),
    then manually sets just the attributes the pipeline methods need.
    """
    # An empty config, whatever neuron-lite/config holds on this box.
    monkeypatch.setattr(
        start, 'config', mock.MagicMock(**{'get.return_value': {}}))
    with tempfile.TemporaryDirectory() as tmpdir:
        h = object.__new__(StartupDag)
        h.networkDB = NetworkDB(os.path.join(tmpdir, 'test.db'))
        h.nostrPubkey = 'a' * 64
        h._networkClients = {}
        h._networkSubscribed = {}
        h._networkListeners = {}
        h._networkRecentSeqs = {}
        h._networkPlan = {}
        h._networkHeld = []
        h._networkWatch = start.RelayWatch()
        h._networkUnsent = {}
        # No engine: a prediction echoes its observation (_networkRunEngine's
        # fallback). A test of a forecast sets _relayPredict.
        h.ensureEngine = lambda: None
        yield h


# ── Helpers ──────────────────────────────────────────────────────────

def make_observation(stream_name='btc-price', pubkey='pub123',
                     value='42000', seq_num=1, event_id='evt1',
                     timestamp=None):
    """Create an InboundObservation for testing."""
    ts = timestamp or int(time.time())
    return InboundObservation(
        stream_name=stream_name,
        nostr_pubkey=pubkey,
        observation=DatastreamObservation(
            stream_name=stream_name,
            timestamp=ts,
            value=value,
            seq_num=seq_num),
        event_id=event_id)


def make_metadata(stream_name='btc-price', pubkey='pub123',
                  cadence_seconds=3600):
    """Create a DatastreamMetadata for testing."""
    return DatastreamMetadata(
        stream_name=stream_name,
        nostr_pubkey=pubkey,
        name='', description='',
        encrypted=False, price_per_obs=0,
        created_at=int(time.time()),
        cadence_seconds=cadence_seconds,
        tags=[])


# ── TestProcessObservation ───────────────────────────────────────────

class TestProcessObservation:

    def test_saves_to_db(self, harness):
        obs = make_observation()
        asyncio.run(harness._networkProcessObservation(obs))
        rows = harness.networkDB.get_observations('btc-price', 'pub123')
        assert len(rows) == 1

    def test_dedup_by_event_id(self, harness):
        obs = make_observation()
        asyncio.run(harness._networkProcessObservation(obs))
        asyncio.run(harness._networkProcessObservation(obs))
        rows = harness.networkDB.get_observations('btc-price', 'pub123')
        assert len(rows) == 1

    def test_multiple_events_saved(self, harness):
        obs1 = make_observation(event_id='evt1', seq_num=1, value='41000')
        obs2 = make_observation(event_id='evt2', seq_num=2, value='42000')
        asyncio.run(harness._networkProcessObservation(obs1))
        asyncio.run(harness._networkProcessObservation(obs2))
        rows = harness.networkDB.get_observations('btc-price', 'pub123')
        assert len(rows) == 2

    def test_no_engine_when_not_predicting(self, harness):
        obs = make_observation()
        asyncio.run(harness._networkProcessObservation(obs))
        preds = harness.networkDB.get_predictions('btc-price', 'pub123')
        assert len(preds) == 0

    def test_engine_runs_when_predicting(self, harness):
        # Subscribe and set up prediction publication
        harness.networkDB.subscribe({
            'stream_name': 'btc-price',
            'nostr_pubkey': 'pub123',
        }, 'wss://relay1')
        harness.networkDB.add_publication(
            'btc-price_pred',
            source_stream_name='btc-price',
            source_provider_pubkey='pub123')

        obs = make_observation()
        asyncio.run(harness._networkProcessObservation(obs))

        preds = harness.networkDB.get_predictions('btc-price', 'pub123')
        assert len(preds) == 1
        assert preds[0]['value'] == '42000'

    def test_null_observation(self, harness):
        """Observation with None body still gets saved (as null value)."""
        obs = InboundObservation(
            stream_name='btc-price',
            nostr_pubkey='pub123',
            observation=None,
            event_id='evt_null')
        asyncio.run(harness._networkProcessObservation(obs))
        rows = harness.networkDB.get_observations('btc-price', 'pub123')
        assert len(rows) == 1


# ── TestRunEngine ────────────────────────────────────────────────────

class TestRunEngine:

    def _setup_prediction(self, harness):
        """Set up subscription + prediction publication."""
        harness.networkDB.subscribe({
            'stream_name': 'btc-price',
            'nostr_pubkey': 'pub123',
        }, 'wss://relay1')
        harness.networkDB.add_publication(
            'btc-price_pred',
            source_stream_name='btc-price',
            source_provider_pubkey='pub123')

    def test_saves_prediction(self, harness):
        self._setup_prediction(harness)
        obs_data = DatastreamObservation(
            stream_name='btc-price',
            timestamp=int(time.time()),
            value='42000',
            seq_num=1)
        asyncio.run(harness._networkRunEngine(
            'btc-price', 'pub123', obs_data))

        preds = harness.networkDB.get_predictions('btc-price', 'pub123')
        assert len(preds) == 1
        assert preds[0]['value'] == '42000'

    def test_publishes_to_relay(self, harness):
        self._setup_prediction(harness)
        mock_client = mock.AsyncMock()
        harness._networkClients['wss://relay1'] = mock_client

        obs_data = DatastreamObservation(
            stream_name='btc-price',
            timestamp=int(time.time()),
            value='42000',
            seq_num=1)
        asyncio.run(harness._networkRunEngine(
            'btc-price', 'pub123', obs_data))

        mock_client.publish_observation.assert_called_once()
        published_obs = mock_client.publish_observation.call_args[0][0]
        assert published_obs.stream_name == 'btc-price_pred'

    def test_marks_prediction_published(self, harness):
        self._setup_prediction(harness)
        obs_data = DatastreamObservation(
            stream_name='btc-price',
            timestamp=int(time.time()),
            value='42000',
            seq_num=1)
        asyncio.run(harness._networkRunEngine(
            'btc-price', 'pub123', obs_data))

        preds = harness.networkDB.get_predictions('btc-price', 'pub123')
        assert preds[0]['published'] == 1

    def test_echo_engine_preserves_value(self, harness):
        """With no forecast, the prediction is the exact observation value."""
        self._setup_prediction(harness)
        obs_data = DatastreamObservation(
            stream_name='btc-price',
            timestamp=int(time.time()),
            value='99.99',
            seq_num=5)
        asyncio.run(harness._networkRunEngine(
            'btc-price', 'pub123', obs_data))

        preds = harness.networkDB.get_predictions('btc-price', 'pub123')
        assert preds[0]['value'] == '99.99'
        assert preds[0]['observation_seq'] == 5


# ── TestPublishObservation ───────────────────────────────────────────

class TestPublishObservation:

    def test_publishes_to_all_clients(self, harness):
        harness.networkDB.add_publication('my-stream')
        c1 = mock.AsyncMock()
        c2 = mock.AsyncMock()
        harness._networkClients['wss://r1'] = c1
        harness._networkClients['wss://r2'] = c2

        asyncio.run(harness._networkPublishObservation('my-stream', '42'))

        c1.publish_observation.assert_called_once()
        c2.publish_observation.assert_called_once()

    def test_increments_seq_num(self, harness):
        harness.networkDB.add_publication('my-stream')
        c = mock.AsyncMock()
        harness._networkClients['wss://r1'] = c

        asyncio.run(harness._networkPublishObservation('my-stream', '1'))
        asyncio.run(harness._networkPublishObservation('my-stream', '2'))

        calls = c.publish_observation.call_args_list
        assert calls[0][0][0].seq_num == 1
        assert calls[1][0][0].seq_num == 2

    def test_creates_correct_metadata(self, harness):
        harness.networkDB.add_publication(
            'pred-stream',
            source_stream_name='btc-price',
            source_provider_pubkey='pub123')
        c = mock.AsyncMock()
        harness._networkClients['wss://r1'] = c

        asyncio.run(harness._networkPublishObservation('pred-stream', '42'))

        metadata = c.publish_observation.call_args[0][1]
        assert metadata.stream_name == 'pred-stream'
        assert metadata.nostr_pubkey == 'a' * 64
        assert metadata.metadata['source_stream_name'] == 'btc-price'

    def test_skips_missing_publication(self, harness):
        c = mock.AsyncMock()
        harness._networkClients['wss://r1'] = c

        asyncio.run(harness._networkPublishObservation('nonexistent', '42'))

        c.publish_observation.assert_not_called()

    def test_handles_client_error(self, harness):
        harness.networkDB.add_publication('my-stream')
        c1 = mock.AsyncMock()
        c1.publish_observation.side_effect = Exception('connection lost')
        c2 = mock.AsyncMock()
        harness._networkClients['wss://r1'] = c1
        harness._networkClients['wss://r2'] = c2

        asyncio.run(harness._networkPublishObservation('my-stream', '42'))

        # c2 should still be called even though c1 failed
        c2.publish_observation.assert_called_once()


# ── TestCheckFreshness ───────────────────────────────────────────────

def check_freshness(harness, metadata, obs=None, created_at=None,
                    error=None):
    """What the freshness check says of one stream: (last_obs_time, active).

    The relay's latest event for it reads as `obs`; with only `created_at`
    it is one this neuron can't read (a paid stream's), stamped then; with
    neither the relay has none. `error` is what asking the relay raises.
    """
    key = (metadata.nostr_pubkey, metadata.stream_name)
    client = mock.AsyncMock()
    event = mock.MagicMock()
    event.created_at.return_value.as_secs.return_value = created_at
    client.get_last_observation_events.return_value = (
        {key: event} if obs or created_at else {})
    client.get_last_observation_events.side_effect = error
    client.parse_observation_event = mock.MagicMock(return_value=obs)
    fresh = asyncio.run(
        harness._networkCheckFreshnessBatch(client, [metadata]))
    client.get_last_observation_events.assert_called_once_with([key])
    return fresh[key]


class TestCheckFreshness:

    def test_active_stream(self, harness):
        now = int(time.time())
        obs = make_observation(event_id='evt_fresh', timestamp=now - 100)

        last_obs, active = check_freshness(
            harness, make_metadata(cadence_seconds=3600), obs)

        assert active is True
        assert last_obs == now - 100

    def test_stale_stream(self, harness):
        now = int(time.time())
        obs = make_observation(event_id='evt_stale', timestamp=now - 7200)

        last_obs, active = check_freshness(
            harness, make_metadata(cadence_seconds=3600), obs)

        assert active is False
        assert last_obs == now - 7200

    def test_no_observation_returns_false(self, harness):
        last_obs, active = check_freshness(harness, make_metadata())

        assert active is False
        assert last_obs is None

    def test_saves_observation_during_check(self, harness):
        obs = make_observation(event_id='evt_disc')

        check_freshness(harness, make_metadata(), obs)

        rows = harness.networkDB.get_observations('btc-price', 'pub123')
        assert len(rows) == 1

    def test_no_cadence_always_active(self, harness):
        now = int(time.time())
        obs = make_observation(event_id='evt_nocad', timestamp=now - 86400)

        _, active = check_freshness(
            harness, make_metadata(cadence_seconds=None), obs)

        assert active is True

    def test_client_error_returns_false(self, harness):
        last_obs, active = check_freshness(
            harness, make_metadata(), error=Exception('timeout'))

        assert active is False
        assert last_obs is None

    def test_unreadable_observation_is_as_fresh_as_its_event(self, harness):
        """A paid stream's observation, which only its buyers can read."""
        now = int(time.time())

        last_obs, active = check_freshness(
            harness, make_metadata(cadence_seconds=3600),
            created_at=now - 100)

        assert active is True
        assert last_obs == now - 100
        assert harness.networkDB.get_observations('btc-price', 'pub123') == []


# ── TestFetchDataSources ─────────────────────────────────────────────

class TestFetchDataSources:

    @staticmethod
    def _mock_response(text, status=200):
        resp = mock.MagicMock()
        resp.text = text
        resp.status_code = status
        resp.raise_for_status = mock.MagicMock()
        if status >= 400:
            resp.raise_for_status.side_effect = Exception(f'HTTP {status}')
        return resp

    @staticmethod
    def _fetch_all(harness):
        """Fetch all active data sources (replaces old batch method)."""
        sources = harness.networkDB.get_active_data_sources()
        for src in sources:
            if not src.get('url') or not src.get('cadence_seconds'):
                continue
            pub_map = {p['stream_name']: p
                       for p in harness.networkDB.get_active_publications()}
            if src['stream_name'] not in pub_map:
                continue
            asyncio.run(harness._networkFetchOneDataSource(src))

    def test_json_path_fetch_and_publish(self, harness):
        harness.networkDB.add_data_source(
            stream_name='btc-price',
            url='https://api.example.com/price',
            cadence_seconds=60,
            parser_type='json_path',
            parser_config='data.price')
        harness.networkDB.add_publication('btc-price', cadence_seconds=60)

        mock_client = mock.AsyncMock()
        harness._networkClients['wss://r1'] = mock_client

        mock_requests = mock.MagicMock()
        mock_requests.get.return_value = self._mock_response(
            '{"data": {"price": "42000"}}')

        with mock.patch.dict(sys.modules, {'requests': mock_requests}):
            self._fetch_all(harness)

        mock_client.publish_observation.assert_called_once()
        obs = mock_client.publish_observation.call_args[0][0]
        assert obs.value == '42000'

    def test_json_path_nested_array(self, harness):
        harness.networkDB.add_data_source(
            stream_name='eth-price',
            url='https://api.example.com/prices',
            cadence_seconds=60,
            parser_type='json_path',
            parser_config='markets.0.price')
        harness.networkDB.add_publication('eth-price', cadence_seconds=60)

        mock_client = mock.AsyncMock()
        harness._networkClients['wss://r1'] = mock_client

        mock_requests = mock.MagicMock()
        mock_requests.get.return_value = self._mock_response(
            '{"markets": [{"price": 3200}]}')

        with mock.patch.dict(sys.modules, {'requests': mock_requests}):
            self._fetch_all(harness)

        obs = mock_client.publish_observation.call_args[0][0]
        assert obs.value == '3200'

    def test_python_parser(self, harness):
        harness.networkDB.add_data_source(
            stream_name='custom',
            url='https://api.example.com/data',
            cadence_seconds=60,
            parser_type='python',
            parser_config='return str(float(text) * 2)')
        harness.networkDB.add_publication('custom', cadence_seconds=60)

        mock_client = mock.AsyncMock()
        harness._networkClients['wss://r1'] = mock_client

        mock_requests = mock.MagicMock()
        mock_requests.get.return_value = self._mock_response('21.5')

        with mock.patch.dict(sys.modules, {'requests': mock_requests}):
            self._fetch_all(harness)

        mock_client.publish_observation.assert_called_once()
        obs = mock_client.publish_observation.call_args[0][0]
        assert obs.value == '43.0'

    def test_post_method(self, harness):
        harness.networkDB.add_data_source(
            stream_name='post-stream',
            url='https://api.example.com/data',
            cadence_seconds=60,
            parser_type='json_path',
            parser_config='value',
            method='POST')
        harness.networkDB.add_publication('post-stream', cadence_seconds=60)

        mock_client = mock.AsyncMock()
        harness._networkClients['wss://r1'] = mock_client

        mock_requests = mock.MagicMock()
        mock_requests.post.return_value = self._mock_response('{"value": "99"}')

        with mock.patch.dict(sys.modules, {'requests': mock_requests}):
            self._fetch_all(harness)

        # Should have used POST, not GET
        mock_requests.post.assert_called_once()
        mock_requests.get.assert_not_called()

    def test_skips_no_url(self, harness):
        harness.networkDB.add_data_source(
            stream_name='ext', url='', cadence_seconds=60)
        harness.networkDB.add_publication('ext', cadence_seconds=60)

        mock_requests = mock.MagicMock()
        with mock.patch.dict(sys.modules, {'requests': mock_requests}):
            self._fetch_all(harness)

        mock_requests.get.assert_not_called()

    def test_skips_no_cadence(self, harness):
        harness.networkDB.add_data_source(
            stream_name='nocad',
            url='https://example.com',
            cadence_seconds=0,
            parser_type='json_path',
            parser_config='x')
        harness.networkDB.add_publication('nocad')

        mock_requests = mock.MagicMock()
        with mock.patch.dict(sys.modules, {'requests': mock_requests}):
            self._fetch_all(harness)

        mock_requests.get.assert_not_called()

    def test_handles_fetch_error(self, harness):
        harness.networkDB.add_data_source(
            stream_name='fail',
            url='https://example.com/fail',
            cadence_seconds=60,
            parser_type='json_path',
            parser_config='x')
        harness.networkDB.add_publication('fail', cadence_seconds=60)

        mock_client = mock.AsyncMock()
        harness._networkClients['wss://r1'] = mock_client

        mock_requests = mock.MagicMock()
        mock_requests.get.side_effect = Exception('Connection refused')

        with mock.patch.dict(sys.modules, {'requests': mock_requests}):
            self._fetch_all(harness)

        mock_client.publish_observation.assert_not_called()

    def test_handles_parse_error(self, harness):
        harness.networkDB.add_data_source(
            stream_name='badparse',
            url='https://example.com',
            cadence_seconds=60,
            parser_type='json_path',
            parser_config='nonexistent.key')
        harness.networkDB.add_publication('badparse', cadence_seconds=60)

        mock_client = mock.AsyncMock()
        harness._networkClients['wss://r1'] = mock_client

        mock_requests = mock.MagicMock()
        mock_requests.get.return_value = self._mock_response('{"other": 1}')

        with mock.patch.dict(sys.modules, {'requests': mock_requests}):
            self._fetch_all(harness)

        mock_client.publish_observation.assert_not_called()


# ── TestEndToEnd ─────────────────────────────────────────────────────

class TestEndToEnd:

    def test_observation_triggers_prediction_and_publish(self, harness):
        """Full pipeline: observation → save → engine → prediction → publish."""
        # Setup: subscribe + predict
        harness.networkDB.subscribe({
            'stream_name': 'btc-price',
            'nostr_pubkey': 'pub123',
        }, 'wss://relay1')
        harness.networkDB.add_publication(
            'btc-price_pred',
            source_stream_name='btc-price',
            source_provider_pubkey='pub123')

        mock_client = mock.AsyncMock()
        harness._networkClients['wss://relay1'] = mock_client

        # Process observation
        obs = make_observation(value='50000', seq_num=1)
        asyncio.run(harness._networkProcessObservation(obs))

        # Observation saved
        rows = harness.networkDB.get_observations('btc-price', 'pub123')
        assert len(rows) == 1

        # Prediction saved (echo engine)
        preds = harness.networkDB.get_predictions('btc-price', 'pub123')
        assert len(preds) == 1
        assert preds[0]['value'] == '50000'
        assert preds[0]['published'] == 1

        # Published to relay
        mock_client.publish_observation.assert_called_once()
        pub_obs = mock_client.publish_observation.call_args[0][0]
        assert pub_obs.stream_name == 'btc-price_pred'
        assert pub_obs.seq_num == 1

    def test_freshness_check_saves_and_predicts(self, harness):
        """Freshness check saves observation and triggers engine."""
        # Setup: subscribe + predict
        harness.networkDB.subscribe({
            'stream_name': 'btc-price',
            'nostr_pubkey': 'pub123',
        }, 'wss://relay1')
        harness.networkDB.add_publication(
            'btc-price_pred',
            source_stream_name='btc-price',
            source_provider_pubkey='pub123')

        mock_relay = mock.AsyncMock()
        harness._networkClients['wss://relay1'] = mock_relay

        # Mock relay returns a fresh observation
        now = int(time.time())
        obs = make_observation(event_id='evt_disc', value='48000',
                               seq_num=5, timestamp=now - 60)
        last_obs, active = check_freshness(
            harness, make_metadata(cadence_seconds=3600), obs)

        assert active is True

        # Observation was saved
        rows = harness.networkDB.get_observations('btc-price', 'pub123')
        assert len(rows) == 1

        # Prediction was generated
        preds = harness.networkDB.get_predictions('btc-price', 'pub123')
        assert len(preds) == 1
        assert preds[0]['value'] == '48000'

    def test_data_source_full_cycle(self, harness):
        """Data source fetch → parse → publish → seq increment."""
        harness.networkDB.add_data_source(
            stream_name='sensor',
            url='https://api.example.com/sensor',
            cadence_seconds=60,
            parser_type='json_path',
            parser_config='reading')
        harness.networkDB.add_publication('sensor', cadence_seconds=60)

        mock_client = mock.AsyncMock()
        harness._networkClients['wss://r1'] = mock_client

        mock_requests = mock.MagicMock()

        # First fetch
        mock_requests.get.return_value = TestFetchDataSources._mock_response(
            '{"reading": "23.5"}')
        with mock.patch.dict(sys.modules, {'requests': mock_requests}):
            TestFetchDataSources._fetch_all(harness)

        assert mock_client.publish_observation.call_count == 1
        obs1 = mock_client.publish_observation.call_args[0][0]
        assert obs1.value == '23.5'
        assert obs1.seq_num == 1

        # Verify publication was updated
        pubs = harness.networkDB.get_active_publications()
        pub = next(p for p in pubs if p['stream_name'] == 'sensor')
        assert pub['last_seq_num'] == 1
        assert pub['last_published_at'] is not None


# ── TestBridgeObservationValues ──────────────────────────────────────

class TestBridgeObservationValues:
    """Base bridge streams carry an OBJECT as the observation value.

    Ordinary streams carry a scalar ('42000'). The Base bridge publishes one
    object per on-chain write:

        {"chainId": 84532, "streamId": 1, "block": 45772737,
         "round": 41373, "raw": "1"}

    Before _numericObservationValue existed, float() on that dict raised and
    every bridge observation silently took the echo path: stored correctly,
    never predicted on.
    """

    @staticmethod
    def _bridge(raw='1', value=None):
        v = {'chainId': 84532, 'streamId': 1,
             'block': 45772737, 'round': 41373, 'raw': raw}
        if value is not None:
            v['value'] = value
        return v

    # -- the helper itself --

    def test_plain_string_scalar_unchanged(self):
        assert StartupDag._numericObservationValue('42000') == 42000.0

    def test_plain_number_scalar_unchanged(self):
        assert StartupDag._numericObservationValue(99.5) == 99.5

    def test_non_numeric_scalar_is_none(self):
        assert StartupDag._numericObservationValue('sunny') is None
        assert StartupDag._numericObservationValue(None) is None

    def test_bridge_object_uses_raw(self):
        assert StartupDag._numericObservationValue(self._bridge(raw='1')) == 1.0

    def test_bridge_object_prefers_converted_value(self):
        """value is the economically meaningful number when a conversion existed."""
        v = self._bridge(raw='201360', value='3421.77')
        assert StartupDag._numericObservationValue(v) == 3421.77

    def test_missing_value_falls_back_to_raw_not_zero(self):
        """Absent 'value' means no conversion existed. It must NOT become 0."""
        v = self._bridge(raw='7')
        assert 'value' not in v
        assert StartupDag._numericObservationValue(v) == 7.0

    def test_zero_value_is_a_real_zero(self):
        """value=0 is a legitimate reading, distinct from value absent."""
        assert StartupDag._numericObservationValue(
            self._bridge(raw='5', value=0)) == 0.0

    def test_zero_raw_is_a_real_zero(self):
        assert StartupDag._numericObservationValue(self._bridge(raw='0')) == 0.0

    def test_negative_raw(self):
        """int256 is signed; negatives occur."""
        assert StartupDag._numericObservationValue(
            self._bridge(raw='-42')) == -42.0

    def test_raw_wider_than_float64(self):
        """int256 exceeds 2**53. Must convert, not crash."""
        big = str(2 ** 200)
        out = StartupDag._numericObservationValue(self._bridge(raw=big))
        assert out == pytest.approx(float(big))

    def test_object_without_raw_or_value_is_none(self):
        assert StartupDag._numericObservationValue(
            {'chainId': 84532, 'round': 41373}) is None

    def test_object_with_garbage_raw_is_none(self):
        assert StartupDag._numericObservationValue(
            self._bridge(raw='0xdeadbeef')) is None

    # -- the ingest path --

    def _setup(self, harness, stream='satori-84532-1', pub='bridgepub'):
        harness.networkDB.subscribe(
            {'stream_name': stream, 'nostr_pubkey': pub}, 'wss://r1')
        harness.networkDB.add_publication(
            f'{stream}_pred',
            source_stream_name=stream,
            source_provider_pubkey=pub)

    def test_bridge_observation_reaches_engine_not_echo(self, harness):
        """The regression this whole change exists for."""
        self._setup(harness)
        seen = {}

        def fake_predict(stream_name, provider_pubkey, observation, numeric):
            seen['numeric'] = numeric
            return '0.5'

        harness._relayPredict = fake_predict
        obs = DatastreamObservation(
            stream_name='satori-84532-1',
            timestamp=1787313600,
            value=self._bridge(raw='1'),
            seq_num=1)
        asyncio.run(harness._networkRunEngine(
            'satori-84532-1', 'bridgepub', obs))

        # the engine was reached, and got the unwrapped number
        assert seen.get('numeric') == 1.0
        # the stored prediction is the engine's forecast, not an echoed object
        preds = harness.networkDB.get_predictions(
            'satori-84532-1', 'bridgepub')
        assert len(preds) == 1
        assert preds[0]['value'] == '0.5'

    def test_ordinary_scalar_stream_still_works(self, harness):
        """Regression guard: scalar streams behave exactly as before."""
        self._setup(harness, stream='btc-price', pub='pub123')
        seen = {}

        def fake_predict(stream_name, provider_pubkey, observation, numeric):
            seen['numeric'] = numeric
            return '43000'

        harness._relayPredict = fake_predict
        obs = DatastreamObservation(
            stream_name='btc-price', timestamp=int(time.time()),
            value='42000', seq_num=1)
        asyncio.run(harness._networkRunEngine('btc-price', 'pub123', obs))

        assert seen.get('numeric') == 42000.0
        preds = harness.networkDB.get_predictions('btc-price', 'pub123')
        assert preds[0]['value'] == '43000'

    def test_unparseable_object_still_echoes(self, harness):
        """No raw, no value: cannot predict, must fall back to echo safely."""
        self._setup(harness, stream='weird', pub='pub9')
        harness._relayPredict = lambda *a, **k: pytest.fail(
            'engine must not be reached for an unparseable value')
        obs = DatastreamObservation(
            stream_name='weird', timestamp=int(time.time()),
            value={'chainId': 84532}, seq_num=1)
        asyncio.run(harness._networkRunEngine('weird', 'pub9', obs))

        preds = harness.networkDB.get_predictions('weird', 'pub9')
        assert len(preds) == 1
        assert json.loads(preds[0]['value']) == {'chainId': 84532}


    # -- ticks: labelled, unconverted, and never a value --

    def test_unconverted_tick_is_not_a_number(self):
        """A tick is the logarithm of a price. Feeding tick 213397 into the
        same series that later carries price 540.44 mixes two bases; the
        helper must refuse it and let the observation echo."""
        v = self._bridge(raw='213397')
        v['unit'] = 'tick'
        assert StartupDag._numericObservationValue(v) is None

    def test_converted_tick_uses_the_price(self):
        v = self._bridge(raw='213397', value=540.44)
        v['unit'] = 'tick'
        assert StartupDag._numericObservationValue(v) == 540.44

    def test_raw_unit_streams_unaffected_by_tick_guard(self):
        v = self._bridge(raw='7')
        v['unit'] = 'raw'
        assert StartupDag._numericObservationValue(v) == 7.0

    # -- replay across the whole ingest path --

    def test_republished_round_does_not_reach_the_engine_twice(self, harness):
        """The failure the round column exists for: a bridge restart republishes
        round 41373 with a fresh event id and a bumped seq_num, which passes
        the event_id and seq_num guards. It must be dropped as a duplicate."""
        self._setup(harness)
        calls = []

        def fake_predict(stream_name, provider_pubkey, observation, numeric):
            calls.append(numeric)
            return '0.5'

        harness._relayPredict = fake_predict

        def inbound(event_id, seq):
            return types.SimpleNamespace(
                stream_name='satori-84532-1', nostr_pubkey='bridgepub',
                event_id=event_id, paid=False,
                observation=DatastreamObservation(
                    stream_name='satori-84532-1', timestamp=1787313600,
                    value=self._bridge(raw='1'), seq_num=seq))

        asyncio.run(harness._networkProcessObservation(inbound('evt-a', 1)))
        asyncio.run(harness._networkProcessObservation(inbound('evt-b', 7)))

        assert len(calls) == 1, 'the replayed round must not re-run the engine'
        obs = harness.networkDB.get_observations('satori-84532-1', 'bridgepub')
        assert len(obs) == 1


# ── What a relay is asked for, and copies from several relays ────────

class _RelayOf:
    """A relay client that delivers these observations, then goes quiet."""

    def __init__(self, observations):
        self._observations = observations

    async def observations(self):
        for obs in self._observations:
            await asyncio.sleep(0)  # let the other relay's listener run in between
            yield obs


class TestFollowedStreams:

    def _subscribe(self, harness, name, pubkey='pub123'):
        harness.networkDB.subscribe({
            'stream_name': name, 'nostr_pubkey': pubkey,
            'name': name, 'cadence_seconds': 3600}, 'ws://relay-a')

    def test_a_relay_is_asked_for_the_subscribed_streams_by_name(self, harness):
        self._subscribe(harness, 'gold')
        self._subscribe(harness, 'bitcoin')
        self._subscribe(harness, 'gold', 'otherpub')
        streams, receivers = asyncio.run(harness._networkFollowed())
        assert streams == [('otherpub', 'gold'), ('pub123', 'bitcoin'), ('pub123', 'gold')]
        assert receivers == []

    def test_the_same_value_from_three_relays_is_processed_once(self, harness):
        self._subscribe(harness, 'gold')
        processed = []

        async def process(obs):
            processed.append((obs.stream_name, obs.observation.seq_num))
            await asyncio.sleep(0.01)  # a real save and prediction take a while
        harness._networkProcessObservation = process

        def copy(seq, event_id):
            return make_observation(stream_name='gold', seq_num=seq, event_id=event_id)
        # Each relay's copy is its own event (its own id), with the value's seq.
        for n in ('a', 'b', 'c'):
            harness._networkClients[f'ws://relay-{n}'] = _RelayOf(
                [copy(7, f'evt7-{n}'), copy(8, f'evt8-{n}')])

        async def listen_on_all():
            await asyncio.gather(*(harness._networkListen(url) for url in harness._networkClients))
        asyncio.run(listen_on_all())
        assert processed == [('gold', 7), ('gold', 8)]

    def test_a_slower_relays_copy_of_an_older_value_is_not_taken_again(self, harness):
        self._subscribe(harness, 'gold')
        processed = []

        async def process(obs):
            processed.append(obs.observation.seq_num)
        harness._networkProcessObservation = process

        def copy(seq, relay):
            return make_observation(stream_name='gold', seq_num=seq, event_id=f'evt{seq}-{relay}')
        # relay-a is level; relay-b delivers 7 only after 8 was taken, then 8 and a new 9.
        harness._networkClients['ws://relay-a'] = _RelayOf([copy(7, 'a'), copy(8, 'a')])
        asyncio.run(harness._networkListen('ws://relay-a'))
        harness._networkClients['ws://relay-b'] = _RelayOf([copy(7, 'b'), copy(8, 'b'), copy(9, 'b')])
        asyncio.run(harness._networkListen('ws://relay-b'))
        assert processed == [7, 8, 9]

    def test_a_stream_not_subscribed_is_dropped(self, harness):
        self._subscribe(harness, 'gold')
        processed = []

        async def process(obs):
            processed.append(obs.stream_name)
        harness._networkProcessObservation = process
        harness._networkClients['ws://relay-a'] = _RelayOf(
            [make_observation(stream_name='eth'), make_observation(stream_name='gold')])
        asyncio.run(harness._networkListen('ws://relay-a'))
        assert processed == ['gold']


# ── Central's relay plan: a neuron holds the relays central gave it ──

CENTRAL = 'c' * 64   # the key that signs central's streams


class _Central:
    """Central as the neuron sees it: /peer/relay-config and /peer/relays."""

    def __init__(self, relays=None, config=None, down=False):
        self.relays = relays
        self.config = {'max_streams': 60, 'stream_publisher_pubkey': CENTRAL, 'keep': 2} if config is None else config
        self.down = down
        self.asked = []

    def getRelayConfig(self):
        return {} if self.down else self.config

    def getRelays(self, forPubkey=None):
        self.asked.append(forPubkey)
        if self.down:
            raise ConnectionError('central is down')
        return self.relays


def _planned(*names, healthy=True):
    return [{'relay_url': f'ws://relay-{n}', 'nostr_pubkey': None, 'address': None, 'healthy': healthy}
            for n in names]


class _Held:
    """A relay client: connected or not."""

    def __init__(self, connected=True):
        self.connected = connected

    async def is_connected(self):
        return self.connected


@pytest.fixture
def planned(harness):
    """The harness with relays that connect on request; `harness.unreachable` do not answer."""
    harness.unreachable = set()
    harness.announced, harness.dropped = [], []

    async def connect(relay_url, ConfigClass):
        if relay_url not in harness._networkClients:
            harness._networkClients[relay_url] = _Held(relay_url not in harness.unreachable)
        return harness._networkClients[relay_url]

    async def announce(relay_url):
        harness.announced.append(relay_url)

    async def disconnect(relay_url):
        harness._networkClients.pop(relay_url, None)
        harness.dropped.append(relay_url)

    harness._networkConnect = connect
    harness._networkAnnouncePublications = announce
    harness._networkDisconnect = disconnect
    harness._networkEnsurePredictionListener = lambda relay_url: None
    harness._networkEnsureAccessRequestListener = lambda relay_url: None
    harness.server = _Central(_planned('a', 'b', 'c'))
    return harness


def _refresh_and_hold(h):
    async def run():
        await h._networkRefreshPlan()
        await h._networkHoldPlan(None)
    asyncio.run(run())


class TestRelayPlan:

    def _subscribe(self, harness, name, pubkey, relay_url):
        harness.networkDB.subscribe({
            'stream_name': name, 'nostr_pubkey': pubkey,
            'name': name, 'cadence_seconds': 3600}, relay_url)

    def test_it_asks_with_its_own_key_and_holds_the_first_two(self, planned):
        _refresh_and_hold(planned)
        assert planned.server.asked == ['a' * 64]
        assert planned._networkPlanRelays() == ['ws://relay-a', 'ws://relay-b', 'ws://relay-c']
        assert planned._networkHeld == ['ws://relay-a', 'ws://relay-b']
        assert set(planned._networkClients) == {'ws://relay-a', 'ws://relay-b'}   # the third is never dialled
        assert planned.announced == ['ws://relay-a', 'ws://relay-b']

    def test_a_relay_that_does_not_answer_is_passed_over(self, planned):
        planned.unreachable = {'ws://relay-a'}
        _refresh_and_hold(planned)
        assert planned._networkHeld == ['ws://relay-b', 'ws://relay-c']
        asyncio.run(planned._networkPrune())
        assert set(planned._networkClients) == {'ws://relay-b', 'ws://relay-c'}

    def test_central_says_how_many_to_hold(self, planned):
        for keep, held in ((1, 1), (3, 3), (None, 2), (0, 2), (9, 3), ('x', 2)):
            planned.server.config = {'keep': keep}
            _refresh_and_hold(planned)
            assert len(planned._networkHeld) == held, keep

    def test_holding_again_changes_nothing_and_announces_nothing_new(self, planned):
        _refresh_and_hold(planned)
        _refresh_and_hold(planned)
        assert planned._networkHeld == ['ws://relay-a', 'ws://relay-b']
        assert planned.announced == ['ws://relay-a', 'ws://relay-b']

    def test_a_held_relay_central_calls_unhealthy_gives_way_to_the_next(self, planned):
        _refresh_and_hold(planned)
        # Central moved relay-a to the end of the list (unhealthy).
        planned.server.relays = _planned('b', 'c') + _planned('a', healthy=False)
        _refresh_and_hold(planned)
        assert planned._networkHeld == ['ws://relay-b', 'ws://relay-c']
        asyncio.run(planned._networkPrune())
        assert planned.dropped == ['ws://relay-a']

    def test_an_older_central_answers_with_every_relay_which_is_not_a_plan(self, planned):
        planned.server.relays = [{'relay_url': f'wss://r{i}.example', 'nostr_pubkey': None, 'address': None}
                                 for i in range(40)]
        planned.server.config = {'max_streams': 60}
        _refresh_and_hold(planned)
        assert planned._networkPlanRelays() == [] and planned._networkHeld == []
        assert planned._networkClients == {}

    def test_an_empty_list_is_no_plan(self, planned):
        _refresh_and_hold(planned)
        planned.server.relays = []
        _refresh_and_hold(planned)
        assert planned._networkPlanRelays() == [] and planned._networkHeld == []

    def test_when_central_does_not_answer_the_last_list_is_used_also_after_a_restart(self, planned):
        _refresh_and_hold(planned)
        planned.server.down = True
        _refresh_and_hold(planned)
        assert planned._networkHeld == ['ws://relay-a', 'ws://relay-b']
        # A restart: nothing in memory, central still down.
        planned._networkPlan, planned._networkHeld, planned._networkClients = {}, [], {}
        _refresh_and_hold(planned)
        assert planned._networkPlanRelays() == ['ws://relay-a', 'ws://relay-b', 'ws://relay-c']
        assert planned._networkHeld == ['ws://relay-a', 'ws://relay-b']
        assert planned._networkPlanKeep() == 2 and planned._networkPlan['publisher'] == CENTRAL

    def test_needed_relays_are_the_held_ones_a_community_streams_relay_and_those_added_in_settings(self, planned):
        self._subscribe(planned, 'gold', CENTRAL, 'ws://relay-c')          # central's: arrives on the held relays
        self._subscribe(planned, 'weather', 'pub123', 'ws://community')    # someone else's stream
        planned.networkDB.upsert_relay('ws://mine', user_added=True)
        planned.networkDB.upsert_relay('ws://seen-once')
        _refresh_and_hold(planned)
        assert planned._neededRelays() == {'ws://relay-a', 'ws://relay-b', 'ws://community'}
        planned.networkDB.add_publication('gold_pred')
        assert planned._neededRelays() == {'ws://relay-a', 'ws://relay-b', 'ws://community', 'ws://mine'}

    def test_without_a_plan_the_needed_relays_are_as_before(self, harness):
        self._subscribe(harness, 'gold', CENTRAL, 'ws://relay-c')
        harness.networkDB.upsert_relay('ws://seen-once')
        assert harness._neededRelays() == {'ws://relay-c'}
        harness.networkDB.add_publication('gold_pred')
        assert harness._neededRelays() == {'ws://relay-c', 'ws://seen-once'}

    def test_a_predicting_neuron_connects_to_the_needed_relays_and_no_others(self, planned):
        planned.networkDB.add_publication('gold_pred')
        planned.networkDB.upsert_relay('ws://mine', user_added=True)
        for i in range(20):
            planned.networkDB.upsert_relay(f'ws://seen-{i}')
        _refresh_and_hold(planned)
        asked = len(planned.server.asked)

        async def run():
            await planned._networkEnsurePublisherConnections(None)
            await planned._networkPrune()
        asyncio.run(run())
        assert set(planned._networkClients) == {'ws://relay-a', 'ws://relay-b', 'ws://mine'}
        assert len(planned.server.asked) == asked   # central's every-relay list is not fetched
        assert planned.dropped == []

    def test_prune_lets_go_of_what_is_not_needed_only_with_a_plan(self, planned):
        planned._networkClients['ws://left-over'] = _Held()
        asyncio.run(planned._networkPrune())
        assert 'ws://left-over' in planned._networkClients   # no plan yet: nothing is dropped
        _refresh_and_hold(planned)
        asyncio.run(planned._networkPrune())
        assert set(planned._networkClients) == {'ws://relay-a', 'ws://relay-b'}

    def test_centrals_streams_are_not_hunted_relay_by_relay(self, planned):
        self._subscribe(planned, 'gold', CENTRAL, 'ws://relay-c')
        planned._networkFirstRun = True
        _refresh_and_hold(planned)
        asked = len(planned.server.asked)
        asyncio.run(planned._networkReconcile(None))
        assert len(planned.server.asked) == asked           # no relay list fetched to hunt with
        assert planned._networkHeld == ['ws://relay-a', 'ws://relay-b'] and planned.dropped == []


class _Clock:
    def __init__(self):
        self.now = 5000.0

    def __call__(self):
        return self.now


@pytest.fixture
def watched(planned, monkeypatch):
    """A neuron holding relay-a and relay-b, with a clock the test moves."""
    clock = _Clock()
    planned._networkWatch = start.RelayWatch(clock=clock, spread=lambda low, high: low)
    planned.clock = clock
    _refresh_and_hold(planned)
    assert planned._networkHeld == ['ws://relay-a', 'ws://relay-b']
    return planned


def _look(h, seconds=0):
    h.clock.now += seconds
    asyncio.run(h._networkWatchHeld(None))


def _delivers(h, relay_url, stream, at):
    h._networkWatch.delivered(relay_url, stream, at)


class TestRelayWatch:
    """Between the hourly passes a failed relay is left for the next one on the list."""

    def test_a_relay_that_stops_answering_is_replaced_after_a_minute(self, watched):
        watched._networkClients['ws://relay-a'].connected = False
        _look(watched)
        _look(watched, 59)
        assert watched._networkHeld == ['ws://relay-a', 'ws://relay-b']   # not yet
        _look(watched, 1)
        assert watched._networkHeld == ['ws://relay-c', 'ws://relay-b']
        assert watched.dropped == ['ws://relay-a'] and 'ws://relay-c' in watched.announced

    def test_a_short_drop_moves_nobody(self, watched):
        watched._networkClients['ws://relay-a'].connected = False
        _look(watched)
        _look(watched, 30)
        watched._networkClients['ws://relay-a'].connected = True
        _look(watched, 30)
        _look(watched, 120)
        assert watched._networkHeld == ['ws://relay-a', 'ws://relay-b'] and watched.dropped == []

    def test_a_relay_that_is_connected_but_not_fed_is_left(self, watched):
        for relay_url in watched._networkHeld:
            _delivers(watched, relay_url, 'gold', 1000)
        _delivers(watched, 'ws://relay-a', 'gold', 4600)    # relay-b did not get the next value
        _look(watched)
        _look(watched, 60)
        assert watched._networkHeld == ['ws://relay-a', 'ws://relay-c']

    def test_a_relay_that_catches_up_in_time_is_kept(self, watched):
        for relay_url in watched._networkHeld:
            _delivers(watched, relay_url, 'gold', 1000)
        _delivers(watched, 'ws://relay-a', 'gold', 4600)
        _look(watched)
        _delivers(watched, 'ws://relay-b', 'gold', 4600)    # the publisher sent it again
        _look(watched, 60)
        _look(watched, 60)
        assert watched._networkHeld == ['ws://relay-a', 'ws://relay-b']

    def test_with_no_other_relay_answering_the_failed_one_stays_held(self, watched):
        watched.unreachable = {'ws://relay-c'}
        watched._networkClients['ws://relay-a'].connected = False
        _look(watched)
        _look(watched, 60)
        assert watched._networkHeld == ['ws://relay-a', 'ws://relay-b']
        assert 'ws://relay-c' not in watched._networkClients    # the one tried is let go again
        watched._networkClients['ws://relay-a'].connected = True
        _look(watched, 15)
        assert watched._networkWatch.faults == {}

    def test_an_empty_place_is_filled(self, planned, monkeypatch):
        clock = _Clock()
        planned._networkWatch = start.RelayWatch(clock=clock)
        planned.clock = clock
        planned.unreachable = {'ws://relay-b', 'ws://relay-c'}
        _refresh_and_hold(planned)
        asyncio.run(planned._networkPrune())
        assert planned._networkHeld == ['ws://relay-a']
        planned.unreachable = set()
        _look(planned)
        assert planned._networkHeld == ['ws://relay-a', 'ws://relay-b']

    def _overdue_gold(self, h, keep=2):
        h.networkDB.subscribe({'stream_name': 'gold', 'nostr_pubkey': CENTRAL,
                               'name': 'gold', 'cadence_seconds': 3600}, 'ws://relay-a')
        last = int(time.time()) - 3600 - 600      # the next value is 10 minutes late
        for relay_url in h._networkHeld:
            _delivers(h, relay_url, 'gold', last)
        return last

    def test_an_overdue_value_makes_it_try_the_next_relay_and_take_it_if_it_has_the_value(self, watched):
        last = self._overdue_gold(watched)
        _look(watched)
        assert 'ws://relay-c' in watched._networkClients and watched._networkHeld == ['ws://relay-a', 'ws://relay-b']
        _delivers(watched, 'ws://relay-c', 'gold', last + 3600)   # relay-c holds the new value
        _look(watched, 10)
        assert watched._networkHeld == ['ws://relay-a', 'ws://relay-c']
        # relay-a is now behind relay-c and is left in turn, for the only relay there is.
        _look(watched, 15)
        _look(watched, 60)
        assert watched._networkHeld == ['ws://relay-b', 'ws://relay-c']

    def test_when_the_publisher_is_late_nobody_moves(self, watched):
        last = self._overdue_gold(watched)
        _look(watched)
        _delivers(watched, 'ws://relay-c', 'gold', last)          # relay-c has nothing newer
        _look(watched, 10)
        assert watched._networkHeld == ['ws://relay-a', 'ws://relay-b']
        assert 'ws://relay-c' not in watched._networkClients
        clients_before = dict(watched._networkClients)
        _look(watched, 300)                                       # and it is not tried again at once
        assert watched._networkClients == clients_before
        _look(watched, 300)
        assert 'ws://relay-c' in watched._networkClients          # ten minutes on, once more

    def test_holding_one_relay_an_unfed_relay_is_left_through_the_overdue_value(self, planned, monkeypatch):
        planned.server.config = {'keep': 1, 'stream_publisher_pubkey': CENTRAL}
        clock = _Clock()
        planned._networkWatch = start.RelayWatch(clock=clock, spread=lambda low, high: low)
        planned.clock = clock
        _refresh_and_hold(planned)
        assert planned._networkHeld == ['ws://relay-a']
        last = self._overdue_gold(planned)
        _look(planned)
        _delivers(planned, 'ws://relay-b', 'gold', last + 3600)
        _look(planned, 10)
        assert planned._networkHeld == ['ws://relay-b'] and planned.dropped == ['ws://relay-a']

    def test_the_hourly_pass_returns_to_centrals_first_choices(self, watched):
        watched._networkClients['ws://relay-a'].connected = False
        _look(watched)
        _look(watched, 60)
        assert watched._networkHeld == ['ws://relay-c', 'ws://relay-b']
        _look(watched, 600)                                       # relay-a is back; nothing moves by itself
        assert watched._networkHeld == ['ws://relay-c', 'ws://relay-b']
        _refresh_and_hold(watched)
        asyncio.run(watched._networkPrune())
        assert watched._networkHeld == ['ws://relay-a', 'ws://relay-b']
        assert set(watched._networkClients) == {'ws://relay-a', 'ws://relay-b'}

    def test_without_a_plan_nothing_is_watched(self, harness):
        asyncio.run(harness._networkWatchHeld(None))
        assert harness._networkClients == {}


class _Publishes:
    def __init__(self, takes=True, delay=0.0):
        self.takes, self.delay, self.sent = takes, delay, []

    async def publish_observation(self, observation, metadata):
        await asyncio.sleep(self.delay)
        self.sent.append(observation.seq_num)
        return self.takes


class TestSending:

    def _publish(self, h, value='1.5'):
        async def no_bounty(**kwargs):
            return None
        h._bountyScoreAndPay = no_bounty
        h.market = None
        return asyncio.run(h._networkPublishObservation('gold_pred', value))

    def test_a_value_no_relay_took_is_sent_again_when_one_answers(self, harness):
        harness.networkDB.add_publication('gold_pred')
        down = harness._networkClients['ws://relay-a'] = _Publishes(takes=False)
        assert self._publish(harness) == 0
        assert list(harness._networkUnsent) == ['gold_pred']
        down.takes = True
        asyncio.run(harness._networkResendUnsent())
        assert harness._networkUnsent == {} and down.sent == [1, 1]   # the same value, the same seq

    def test_only_the_newest_value_of_a_stream_is_kept_for_sending_again(self, harness):
        harness.networkDB.add_publication('gold_pred')
        down = harness._networkClients['ws://relay-a'] = _Publishes(takes=False)
        self._publish(harness, '1.5')
        self._publish(harness, '1.6')
        down.takes = True
        down.sent.clear()
        asyncio.run(harness._networkResendUnsent())
        assert down.sent == [2]

    def test_one_relay_taking_it_is_enough(self, harness):
        harness.networkDB.add_publication('gold_pred')
        harness._networkClients['ws://relay-a'] = _Publishes(takes=False)
        harness._networkClients['ws://relay-b'] = _Publishes()
        assert self._publish(harness) == 1 and harness._networkUnsent == {}

    def test_a_relay_that_hangs_does_not_hold_back_the_other(self, harness):
        harness.networkDB.add_publication('gold_pred')
        harness._networkClients['ws://slow'] = _Publishes(delay=0.3)
        harness._networkClients['ws://quick'] = _Publishes(delay=0.3)
        began = time.monotonic()
        assert self._publish(harness) == 2
        assert time.monotonic() - began < 0.5    # together, not one after the other
