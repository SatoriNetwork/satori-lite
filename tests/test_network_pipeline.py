"""Tests for the network processing pipeline.

Tests the async methods in StartupDag that handle observation processing,
freshness checking, data source fetching, engine, and publishing.

Strategy: Mock heavy dependencies (wallet, engine, config), import StartupDag
to get the real method implementations. Create instances via object.__new__
to skip __init__, then manually set just the attributes needed for testing.
"""

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import time
import types
import unittest.mock as mock

import pytest

# ── Step 1: Import NetworkDB directly (skip __init__.py chain) ───────

_db_spec = importlib.util.spec_from_file_location(
    'network_db',
    os.path.join(os.path.dirname(__file__),
                 '..', 'neuron-lite', 'satorineuron', 'network_db.py'))
_db_mod = importlib.util.module_from_spec(_db_spec)
_db_spec.loader.exec_module(_db_mod)
NetworkDB = _db_mod.NetworkDB


# ── Step 2: Import satori_nostr models directly (pure dataclasses) ───

_models_path = os.path.join(
    os.path.dirname(__file__),
    '..', '..', 'satorilib', 'src', 'satorilib', 'satori_nostr', 'models.py')
_models_spec = importlib.util.spec_from_file_location(
    'satori_nostr_models', _models_path)
_models_mod = importlib.util.module_from_spec(_models_spec)
_models_spec.loader.exec_module(_models_mod)
DatastreamMetadata = _models_mod.DatastreamMetadata
DatastreamObservation = _models_mod.DatastreamObservation
InboundObservation = _models_mod.InboundObservation


# ── Step 3: Mock heavy deps and import start.py ─────────────────────

# Stub base class (must be a real type for class inheritance)
class _StubStartupDagStruct:
    def __init__(self, *args, **kwargs):
        pass


# Create parent mock objects so attribute chains are consistent
_mock_satorilib = mock.MagicMock()
_mock_satorineuron = mock.MagicMock()
_mock_satoriengine = mock.MagicMock()

# Wire up config mock with needed return values
_mock_satorineuron.config.get.return_value = {}
_mock_satorineuron.config.walletPath.return_value = '/tmp/_test_pipeline'
_mock_satorineuron.config.dataPath.return_value = '/tmp/_test_pipeline'
_mock_satorineuron.config.add = mock.MagicMock()

# Wire up StartupDagStruct as a real class
_mock_satorineuron.structs.start.StartupDagStruct = _StubStartupDagStruct
_mock_satorineuron.structs.start.RunMode = mock.MagicMock()

# Install mock modules in sys.modules
_MOCK_MAP = {
    'satorilib': _mock_satorilib,
    'satorilib.concepts': _mock_satorilib.concepts,
    'satorilib.concepts.structs': _mock_satorilib.concepts.structs,
    'satorilib.concepts.constants': _mock_satorilib.concepts.constants,
    'satorilib.wallet': _mock_satorilib.wallet,
    'satorilib.wallet.evrmore': _mock_satorilib.wallet.evrmore,
    'satorilib.wallet.evrmore.identity': _mock_satorilib.wallet.evrmore.identity,
    'satorilib.server': _mock_satorilib.server,
    'satorineuron': _mock_satorineuron,
    'satorineuron.logging': _mock_satorineuron.logging,
    'satorineuron.config': _mock_satorineuron.config,
    'satorineuron.init': _mock_satorineuron.init,
    'satorineuron.init.wallet': _mock_satorineuron.init.wallet,
    'satorineuron.structs': _mock_satorineuron.structs,
    'satorineuron.structs.start': _mock_satorineuron.structs.start,
    'satoriengine': _mock_satoriengine,
    'satoriengine.veda': _mock_satoriengine.veda,
    'satoriengine.veda.engine': _mock_satoriengine.veda.engine,
}

for _name, _mod in _MOCK_MAP.items():
    sys.modules[_name] = _mod

# Provide real satori_nostr models for deferred imports inside methods
_satori_nostr_mod = types.ModuleType('satorilib.satori_nostr')
_satori_nostr_mod.SatoriNostr = mock.MagicMock()
_satori_nostr_mod.SatoriNostrConfig = mock.MagicMock()
sys.modules['satorilib.satori_nostr'] = _satori_nostr_mod
sys.modules['satorilib.satori_nostr.models'] = _models_mod
_mock_satorilib.satori_nostr = _satori_nostr_mod

# Import start.py to get the real StartupDag class
_start_dir = os.path.join(os.path.dirname(__file__), '..', 'neuron-lite')
_start_spec = importlib.util.spec_from_file_location(
    'start_module',
    os.path.join(_start_dir, 'start.py'))
_start_mod = importlib.util.module_from_spec(_start_spec)
_start_spec.loader.exec_module(_start_mod)
StartupDag = _start_mod.StartupDag


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def harness():
    """Create a StartupDag instance with real NetworkDB and mock clients.

    Uses object.__new__ to skip __init__ (which needs wallet, config, etc.),
    then manually sets just the attributes the pipeline methods need.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        h = object.__new__(StartupDag)
        h.networkDB = NetworkDB(os.path.join(tmpdir, 'test.db'))
        h.nostrPubkey = 'a' * 64
        h._networkClients = {}
        h._networkSubscribed = {}
        h._networkListeners = {}
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
        """Engine echoes exact observation value as prediction."""
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

class TestCheckFreshness:

    def test_active_stream(self, harness):
        now = int(time.time())
        obs = make_observation(event_id='evt_fresh', timestamp=now - 100)
        mock_client = mock.AsyncMock()
        mock_client.get_last_observation.return_value = obs
        metadata = make_metadata(cadence_seconds=3600)

        last_obs, active = asyncio.run(
            harness._networkCheckFreshness(mock_client, 'btc-price', metadata))

        assert active is True
        assert last_obs == now - 100

    def test_stale_stream(self, harness):
        now = int(time.time())
        obs = make_observation(event_id='evt_stale', timestamp=now - 7200)
        mock_client = mock.AsyncMock()
        mock_client.get_last_observation.return_value = obs
        metadata = make_metadata(cadence_seconds=3600)

        last_obs, active = asyncio.run(
            harness._networkCheckFreshness(mock_client, 'btc-price', metadata))

        assert active is False
        assert last_obs == now - 7200

    def test_no_observation_returns_false(self, harness):
        mock_client = mock.AsyncMock()
        mock_client.get_last_observation.return_value = None
        metadata = make_metadata()

        last_obs, active = asyncio.run(
            harness._networkCheckFreshness(mock_client, 'btc-price', metadata))

        assert active is False
        assert last_obs is None

    def test_saves_observation_during_check(self, harness):
        obs = make_observation(event_id='evt_disc')
        mock_client = mock.AsyncMock()
        mock_client.get_last_observation.return_value = obs
        metadata = make_metadata()

        asyncio.run(
            harness._networkCheckFreshness(mock_client, 'btc-price', metadata))

        rows = harness.networkDB.get_observations('btc-price', 'pub123')
        assert len(rows) == 1

    def test_no_cadence_always_active(self, harness):
        now = int(time.time())
        obs = make_observation(event_id='evt_nocad', timestamp=now - 86400)
        mock_client = mock.AsyncMock()
        mock_client.get_last_observation.return_value = obs
        metadata = make_metadata(cadence_seconds=None)

        _, active = asyncio.run(
            harness._networkCheckFreshness(mock_client, 'btc-price', metadata))

        assert active is True

    def test_client_error_returns_false(self, harness):
        mock_client = mock.AsyncMock()
        mock_client.get_last_observation.side_effect = Exception('timeout')
        metadata = make_metadata()

        last_obs, active = asyncio.run(
            harness._networkCheckFreshness(mock_client, 'btc-price', metadata))

        assert active is False
        assert last_obs is None


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
        mock_relay_for_discovery = mock.AsyncMock()
        mock_relay_for_discovery.get_last_observation.return_value = obs
        metadata = make_metadata(cadence_seconds=3600)

        last_obs, active = asyncio.run(
            harness._networkCheckFreshness(
                mock_relay_for_discovery, 'btc-price', metadata))

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
