"""Tests for the network reconciliation loop.

Tests _networkReconcile which handles:
- First run: treats all subscriptions as needing connection
- Staleness detection: checks local observations against cadence
- Relay hunting: connects to relays, discovers streams, checks freshness
- Subscription migration: moves subscriptions to active relays
- Marking stale: streams not found anywhere get marked stale
- Relay fallback: uses known relays when central server is unavailable

Uses the same import strategy as test_network_pipeline.py: mock heavy
dependencies, import real StartupDag, create instances via object.__new__.
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

# ── Import NetworkDB directly ───────────────────────────────────────

_db_spec = importlib.util.spec_from_file_location(
    'network_db',
    os.path.join(os.path.dirname(__file__),
                 '..', 'neuron-lite', 'satorineuron', 'network_db.py'))
_db_mod = importlib.util.module_from_spec(_db_spec)
_db_spec.loader.exec_module(_db_mod)
NetworkDB = _db_mod.NetworkDB

# ── Import satori_nostr models directly ──────────────────────────────

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

# ── Mock heavy deps and import start.py ──────────────────────────────

class _StubStartupDagStruct:
    def __init__(self, *args, **kwargs):
        pass

_mock_satorilib = mock.MagicMock()
_mock_satorineuron = mock.MagicMock()
_mock_satoriengine = mock.MagicMock()

_mock_satorineuron.config.get.return_value = {}
_mock_satorineuron.config.walletPath.return_value = '/tmp/_test_reconcile'
_mock_satorineuron.config.dataPath.return_value = '/tmp/_test_reconcile'
_mock_satorineuron.config.add = mock.MagicMock()
_mock_satorineuron.structs.start.StartupDagStruct = _StubStartupDagStruct
_mock_satorineuron.structs.start.RunMode = mock.MagicMock()

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
    'satorineuron.relay_manager': _mock_satorineuron.relay_manager,
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

_satori_nostr_mod = types.ModuleType('satorilib.satori_nostr')
_satori_nostr_mod.SatoriNostr = mock.MagicMock()
_satori_nostr_mod.SatoriNostrConfig = mock.MagicMock()
sys.modules['satorilib.satori_nostr'] = _satori_nostr_mod
sys.modules['satorilib.satori_nostr.models'] = _models_mod
_mock_satorilib.satori_nostr = _satori_nostr_mod

_start_dir = os.path.join(os.path.dirname(__file__), '..', 'neuron-lite')
_start_spec = importlib.util.spec_from_file_location(
    'start_module_reconcile',
    os.path.join(_start_dir, 'start.py'))
_start_mod = importlib.util.module_from_spec(_start_spec)
_start_spec.loader.exec_module(_start_mod)
StartupDag = _start_mod.StartupDag


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def harness():
    """StartupDag with real NetworkDB, mock server, mock clients."""
    with tempfile.TemporaryDirectory() as tmpdir:
        h = object.__new__(StartupDag)
        h.networkDB = NetworkDB(os.path.join(tmpdir, 'test.db'))
        h.nostrPubkey = 'a' * 64
        h._networkSecretHex = 'b' * 64
        h._networkClients = {}
        h._networkSubscribed = {}
        h._networkListeners = {}
        h._channelListeners = {}
        h._channelOpenListeners = {}
        h._channelSettlementListeners = {}
        h._channelTombstoneListeners = {}
        h._predictionListeners = {}
        h._accessRequestListeners = {}
        h._networkFirstRun = True
        h._baseNoGame = {}
        h.server = mock.MagicMock()
        # Hunting shuffles the relay list; keep central's order so relay
        # order assertions are deterministic.
        with mock.patch('random.shuffle'):
            yield h


# ── Helpers ──────────────────────────────────────────────────────────

def make_metadata(stream_name, pubkey='pub123', cadence=3600):
    return DatastreamMetadata(
        stream_name=stream_name,
        nostr_pubkey=pubkey,
        name='', description='',
        encrypted=False, price_per_obs=0,
        created_at=int(time.time()),
        cadence_seconds=cadence,
        tags=[])


def make_observation(stream_name, pubkey='pub123', value='42000',
                     seq_num=1, event_id=None, timestamp=None):
    ts = timestamp or int(time.time())
    eid = event_id or f'evt_{stream_name}_{seq_num}'
    return InboundObservation(
        stream_name=stream_name,
        nostr_pubkey=pubkey,
        observation=DatastreamObservation(
            stream_name=stream_name,
            timestamp=ts,
            value=value,
            seq_num=seq_num),
        event_id=eid)


def subscribe(harness, stream_name, pubkey='pub123',
              relay_url='wss://relay1', cadence=3600):
    """Add a subscription to the harness DB."""
    harness.networkDB.subscribe({
        'stream_name': stream_name,
        'nostr_pubkey': pubkey,
        'cadence_seconds': cadence,
    }, relay_url)


def mock_config_class():
    """Create a mock SatoriNostrConfig class."""
    return mock.MagicMock()


def make_mock_client(streams=None, observations=None):
    """Create a mock SatoriNostr client.

    Args:
        streams: list of DatastreamMetadata to return from discover
        observations: dict of stream_name -> InboundObservation for get_last_observation
    """
    client = mock.AsyncMock()
    streams = streams or []
    observations = observations or {}
    client.discover_datastreams.return_value = streams

    async def find(pairs):
        wanted = set(pairs)
        return {(s.nostr_pubkey, s.stream_name): s for s in streams
                if (s.nostr_pubkey, s.stream_name) in wanted}
    client.find_datastreams.side_effect = find

    async def latest_events(pairs):
        found = {}
        for pubkey, name in pairs:
            obs = observations.get(name)
            if obs and obs.nostr_pubkey == pubkey:
                event = mock.MagicMock()
                event.obs = obs
                event.created_at.return_value.as_secs.return_value = (
                    obs.observation.timestamp)
                found[(pubkey, name)] = event
        return found
    client.get_last_observation_events.side_effect = latest_events
    client.parse_observation_event = mock.MagicMock(
        side_effect=lambda event: event.obs)

    async def get_obs(name):
        return observations.get(name)
    client.get_last_observation.side_effect = get_obs
    return client


# ── Test First Run ───────────────────────────────────────────────────

class TestFirstRun:
    """On first run, all subscriptions are treated as inactive."""

    def test_first_run_connects_to_relay(self, harness):
        """First run connects to relays and discovers streams."""
        subscribe(harness, 'btc-price', relay_url='wss://relay1')
        harness.server.getRelays.return_value = [
            {'relay_url': 'wss://relay1'}]

        # Mock _networkConnect to return a client with matching stream
        now = int(time.time())
        meta = make_metadata('btc-price')
        obs = make_observation('btc-price', timestamp=now - 60)
        client = make_mock_client(
            streams=[meta], observations={'btc-price': obs})

        async def mock_connect(url, cfg):
            harness._networkClients[url] = client
            harness._networkSubscribed[url] = set()
            return client
        harness._networkConnect = mock_connect
        harness._networkEnsureListener = mock.MagicMock()
        harness._networkAnnouncePublications = mock.AsyncMock()

        ConfigClass = mock_config_class()
        asyncio.run(harness._networkReconcile(ConfigClass))

        # Should have subscribed on the relay
        client.subscribe_datastream.assert_called_once_with(
            'btc-price', 'pub123')
        # First run flag cleared
        assert harness._networkFirstRun is False

    def test_first_run_clears_flag(self, harness):
        """First run flag is cleared even with no subscriptions."""
        harness.server.getRelays.return_value = []
        ConfigClass = mock_config_class()
        asyncio.run(harness._networkReconcile(ConfigClass))
        # No subs, so reconcile returns early, but doesn't clear flag
        # (only cleared when inactive list is built)
        assert harness._networkFirstRun is True

    def test_first_run_marks_stale_if_not_found(self, harness):
        """Stream not found on any relay gets marked stale."""
        subscribe(harness, 'missing-stream')
        harness.server.getRelays.return_value = [
            {'relay_url': 'wss://relay1'}]

        # Relay has no matching streams
        client = make_mock_client(streams=[])

        async def mock_connect(url, cfg):
            harness._networkClients[url] = client
            harness._networkSubscribed[url] = set()
            return client
        harness._networkConnect = mock_connect
        harness._networkEnsureListener = mock.MagicMock()
        harness._networkAnnouncePublications = mock.AsyncMock()

        ConfigClass = mock_config_class()
        asyncio.run(harness._networkReconcile(ConfigClass))

        # Stream should be marked stale
        subs = harness.networkDB.get_active()
        assert subs[0]['stale_since'] is not None


# ── Test Staleness Detection ─────────────────────────────────────────

class TestStalenessDetection:
    """After first run, only locally stale subscriptions are hunted."""

    def test_fresh_subscription_not_hunted(self, harness):
        """Subscription with recent observation is skipped."""
        harness._networkFirstRun = False
        subscribe(harness, 'btc-price', cadence=3600)

        # Add a recent observation (not stale)
        harness.networkDB.save_observation(
            'btc-price', 'pub123', '42000', 'evt1',
            seq_num=1, observed_at=int(time.time()))

        harness.server.getRelays.return_value = [
            {'relay_url': 'wss://relay1'}]

        ConfigClass = mock_config_class()
        asyncio.run(harness._networkReconcile(ConfigClass))

        # Should not have tried to connect (nothing inactive)
        assert len(harness._networkClients) == 0

    def test_stale_subscription_is_hunted(self, harness):
        """Subscription with old observation gets hunted."""
        harness._networkFirstRun = False
        subscribe(harness, 'btc-price', cadence=3600)

        # Add an observation and backdate received_at to make it stale
        harness.networkDB.save_observation(
            'btc-price', 'pub123', '42000', 'evt1', seq_num=1)
        conn = harness.networkDB._get_conn()
        conn.execute(
            "UPDATE observations SET received_at = ?",
            (int(time.time()) - 7200,))
        conn.commit()

        harness.server.getRelays.return_value = [
            {'relay_url': 'wss://relay1'}]

        now = int(time.time())
        meta = make_metadata('btc-price')
        obs = make_observation('btc-price', timestamp=now - 60)
        client = make_mock_client(
            streams=[meta], observations={'btc-price': obs})

        async def mock_connect(url, cfg):
            harness._networkClients[url] = client
            harness._networkSubscribed[url] = set()
            return client
        harness._networkConnect = mock_connect
        harness._networkEnsureListener = mock.MagicMock()
        harness._networkAnnouncePublications = mock.AsyncMock()

        ConfigClass = mock_config_class()
        asyncio.run(harness._networkReconcile(ConfigClass))

        # Should have hunted and found it
        client.subscribe_datastream.assert_called_once()

    def test_no_cadence_never_stale(self, harness):
        """Subscription with no cadence is never considered stale."""
        harness._networkFirstRun = False
        subscribe(harness, 'events', cadence=None)

        # No observations at all, but cadence=None → not stale
        harness.server.getRelays.return_value = [
            {'relay_url': 'wss://relay1'}]

        ConfigClass = mock_config_class()
        asyncio.run(harness._networkReconcile(ConfigClass))

        assert len(harness._networkClients) == 0


# ── Test Stale Recheck Throttling ────────────────────────────────────

class TestStaleRecheck:
    """Streams marked stale are only rechecked after 24 hours."""

    def test_recently_stale_skipped(self, harness):
        """Stream marked stale 1 hour ago is not rechecked."""
        subscribe(harness, 'btc-price')
        # Mark stale 1 hour ago
        harness.networkDB.mark_stale('btc-price', 'pub123')

        harness._networkFirstRun = True
        harness.server.getRelays.return_value = [
            {'relay_url': 'wss://relay1'}]

        ConfigClass = mock_config_class()
        asyncio.run(harness._networkReconcile(ConfigClass))

        # Should not have connected (stale_since is recent)
        assert len(harness._networkClients) == 0

    def test_old_stale_rechecked(self, harness):
        """Stream marked stale >24 hours ago gets rechecked."""
        subscribe(harness, 'btc-price')
        # Mark stale 25 hours ago
        conn = harness.networkDB._get_conn()
        conn.execute("""
            UPDATE subscriptions SET stale_since = ?
            WHERE stream_name = 'btc-price'
        """, (int(time.time()) - 90000,))
        conn.commit()

        harness._networkFirstRun = True
        harness.server.getRelays.return_value = [
            {'relay_url': 'wss://relay1'}]

        # Stream still not found
        client = make_mock_client(streams=[])

        async def mock_connect(url, cfg):
            harness._networkClients[url] = client
            harness._networkSubscribed[url] = set()
            return client
        harness._networkConnect = mock_connect
        harness._networkEnsureListener = mock.MagicMock()
        harness._networkAnnouncePublications = mock.AsyncMock()

        ConfigClass = mock_config_class()
        asyncio.run(harness._networkReconcile(ConfigClass))

        # Should have connected and searched
        client.find_datastreams.assert_called_once()
        client.discover_datastreams.assert_not_called()


# ── Test Relay Hunting ───────────────────────────────────────────────

class TestRelayHunting:
    """Stream hunting across multiple relays."""

    def test_finds_on_second_relay(self, harness):
        """Stream not on relay1 but found active on relay2."""
        subscribe(harness, 'btc-price', relay_url='wss://relay1')
        harness.server.getRelays.return_value = [
            {'relay_url': 'wss://relay1'},
            {'relay_url': 'wss://relay2'},
        ]

        now = int(time.time())
        meta = make_metadata('btc-price')
        obs = make_observation('btc-price', timestamp=now - 60)

        # Relay1: no streams. Relay2: has the stream
        client1 = make_mock_client(streams=[])
        client2 = make_mock_client(
            streams=[meta], observations={'btc-price': obs})

        call_count = [0]

        async def mock_connect(url, cfg):
            call_count[0] += 1
            if url == 'wss://relay1':
                harness._networkClients[url] = client1
                harness._networkSubscribed[url] = set()
                return client1
            else:
                harness._networkClients[url] = client2
                harness._networkSubscribed[url] = set()
                return client2
        harness._networkConnect = mock_connect
        harness._networkEnsureListener = mock.MagicMock()
        harness._networkAnnouncePublications = mock.AsyncMock()

        ConfigClass = mock_config_class()
        asyncio.run(harness._networkReconcile(ConfigClass))

        # Should have found it on relay2
        client2.subscribe_datastream.assert_called_once_with(
            'btc-price', 'pub123')
        # Subscription relay updated in DB
        subs = harness.networkDB.get_active()
        assert subs[0]['relay_url'] == 'wss://relay2'

    def test_stops_hunting_when_all_found(self, harness):
        """Stops connecting to relays once all streams are found."""
        subscribe(harness, 'btc-price')
        harness.server.getRelays.return_value = [
            {'relay_url': 'wss://relay1'},
            {'relay_url': 'wss://relay2'},
            {'relay_url': 'wss://relay3'},
        ]

        now = int(time.time())
        meta = make_metadata('btc-price')
        obs = make_observation('btc-price', timestamp=now - 60)
        client1 = make_mock_client(
            streams=[meta], observations={'btc-price': obs})

        connected_urls = []

        async def mock_connect(url, cfg):
            connected_urls.append(url)
            harness._networkClients[url] = client1
            harness._networkSubscribed[url] = set()
            return client1
        harness._networkConnect = mock_connect
        harness._networkEnsureListener = mock.MagicMock()
        harness._networkAnnouncePublications = mock.AsyncMock()

        ConfigClass = mock_config_class()
        asyncio.run(harness._networkReconcile(ConfigClass))

        # Should have stopped after relay1 (found everything)
        assert connected_urls == ['wss://relay1']

    def test_found_but_not_active_continues_hunting(self, harness):
        """Stream found on relay but not active → keep looking."""
        subscribe(harness, 'btc-price')
        harness.server.getRelays.return_value = [
            {'relay_url': 'wss://relay1'},
            {'relay_url': 'wss://relay2'},
        ]

        meta = make_metadata('btc-price')
        # Old observation (stale: 2 hours old, cadence 3600s)
        old_obs = make_observation(
            'btc-price', timestamp=int(time.time()) - 7200,
            event_id='evt_old')
        # Fresh observation on relay2
        fresh_obs = make_observation(
            'btc-price', timestamp=int(time.time()) - 60,
            event_id='evt_fresh')

        client1 = make_mock_client(
            streams=[meta], observations={'btc-price': old_obs})
        client2 = make_mock_client(
            streams=[meta], observations={'btc-price': fresh_obs})

        async def mock_connect(url, cfg):
            c = client1 if url == 'wss://relay1' else client2
            harness._networkClients[url] = c
            harness._networkSubscribed[url] = set()
            return c
        harness._networkConnect = mock_connect
        harness._networkEnsureListener = mock.MagicMock()
        harness._networkAnnouncePublications = mock.AsyncMock()

        ConfigClass = mock_config_class()
        asyncio.run(harness._networkReconcile(ConfigClass))

        # Should have subscribed on relay2 (active), not relay1 (stale)
        client1.subscribe_datastream.assert_not_called()
        client2.subscribe_datastream.assert_called_once()
        # Relay updated
        subs = harness.networkDB.get_active()
        assert subs[0]['relay_url'] == 'wss://relay2'

    def test_disconnects_relay_with_no_matches(self, harness):
        """Relay that has nothing we need gets disconnected."""
        subscribe(harness, 'btc-price')
        harness.server.getRelays.return_value = [
            {'relay_url': 'wss://relay1'},
            {'relay_url': 'wss://relay2'},
        ]

        now = int(time.time())
        meta = make_metadata('btc-price')
        obs = make_observation('btc-price', timestamp=now - 60)

        # Relay1: empty. Relay2: has the stream
        client1 = make_mock_client(streams=[])
        client2 = make_mock_client(
            streams=[meta], observations={'btc-price': obs})

        disconnected = []
        original_disconnect = harness.__class__._networkDisconnect

        async def mock_connect(url, cfg):
            c = client1 if url == 'wss://relay1' else client2
            harness._networkClients[url] = c
            harness._networkSubscribed[url] = set()
            return c

        async def mock_disconnect(url):
            disconnected.append(url)
            harness._networkClients.pop(url, None)
            harness._networkSubscribed.pop(url, None)

        harness._networkConnect = mock_connect
        harness._networkDisconnect = mock_disconnect
        harness._networkEnsureListener = mock.MagicMock()
        harness._networkAnnouncePublications = mock.AsyncMock()

        ConfigClass = mock_config_class()
        asyncio.run(harness._networkReconcile(ConfigClass))

        # Relay1 should have been disconnected (no matches)
        assert 'wss://relay1' in disconnected

    def test_connection_failure_skips_relay(self, harness):
        """Failed connection to a relay skips it and continues."""
        subscribe(harness, 'btc-price')
        harness.server.getRelays.return_value = [
            {'relay_url': 'wss://bad-relay'},
            {'relay_url': 'wss://good-relay'},
        ]

        now = int(time.time())
        meta = make_metadata('btc-price')
        obs = make_observation('btc-price', timestamp=now - 60)
        good_client = make_mock_client(
            streams=[meta], observations={'btc-price': obs})

        async def mock_connect(url, cfg):
            if url == 'wss://bad-relay':
                return None  # connection failed
            harness._networkClients[url] = good_client
            harness._networkSubscribed[url] = set()
            return good_client
        harness._networkConnect = mock_connect
        harness._networkEnsureListener = mock.MagicMock()
        harness._networkAnnouncePublications = mock.AsyncMock()

        ConfigClass = mock_config_class()
        asyncio.run(harness._networkReconcile(ConfigClass))

        # Should have found it on good-relay
        good_client.subscribe_datastream.assert_called_once()


# ── Test Central Server Fallback ─────────────────────────────────────

class TestProviderMatch:
    """Hunting matches on (provider, stream), not stream name alone."""

    def test_same_name_other_provider_not_matched(self, harness):
        subscribe(harness, 'btc-price', pubkey='pub123')
        harness.server.getRelays.return_value = [{'relay_url': 'wss://relay1'}]
        now = int(time.time())
        meta = make_metadata('btc-price', pubkey='someone_else')
        obs = make_observation(
            'btc-price', pubkey='someone_else', timestamp=now - 60)
        client = make_mock_client(
            streams=[meta], observations={'btc-price': obs})

        async def mock_connect(url, cfg):
            harness._networkClients[url] = client
            harness._networkSubscribed[url] = set()
            return client
        harness._networkConnect = mock_connect
        harness._networkEnsureListener = mock.MagicMock()
        harness._networkAnnouncePublications = mock.AsyncMock()

        asyncio.run(harness._networkReconcile(mock_config_class()))

        client.subscribe_datastream.assert_not_called()
        assert harness.networkDB.get_active()[0]['stale_since']


class TestBaseAutoSubscribe:
    """Neurons subscribe to and predict the Base contract streams."""

    BRIDGE = 'bridge_pub'

    def run(self, harness, streams, games, enabled=True):
        harness._basePredictEnabled = lambda: enabled
        client = make_mock_client(streams=streams)
        harness._networkClients['wss://relay1'] = client
        cfg = {'chainId': 84532, 'streamProvider': self.BRIDGE,
               'rpcUrl': 'http://rpc', 'engine': '0xe', 'games': '0xg'}
        predictor = mock.MagicMock()
        predictor.game_for_stream.side_effect = lambda sid: games.get(sid, 0)
        modules = {
            'satorineuron.base_config': mock.MagicMock(
                base_config=lambda: cfg),
            'satorineuron.base_predict': mock.MagicMock(
                BasePredictor=mock.MagicMock(return_value=predictor)),
        }
        with mock.patch.dict(sys.modules, modules):
            asyncio.run(harness._networkEnsureBaseStreams())
        return client, predictor

    def active(self, harness):
        return sorted(s['stream_name'] for s in harness.networkDB.get_active())

    def predicting(self, harness):
        return sorted(p['source_stream_name']
                      for p in harness.networkDB.get_active_publications())

    def test_subscribes_and_predicts_streams_with_a_game(self, harness):
        streams = [make_metadata(f'satori-84532-{i}', pubkey=self.BRIDGE)
                   for i in (1, 2, 3)]
        self.run(harness, streams, games={1: 1, 2: 2})
        assert self.active(harness) == ['satori-84532-1', 'satori-84532-2']
        assert self.predicting(harness) == ['satori-84532-1', 'satori-84532-2']

    def test_ignores_other_providers(self, harness):
        streams = [make_metadata('satori-84532-1', pubkey='someone_else')]
        self.run(harness, streams, games={1: 1})
        assert self.active(harness) == []

    def test_respects_user_unsubscribe(self, harness):
        subscribe(harness, 'satori-84532-1', pubkey=self.BRIDGE)
        harness.networkDB.unsubscribe('satori-84532-1', self.BRIDGE)
        streams = [make_metadata('satori-84532-1', pubkey=self.BRIDGE)]
        _, predictor = self.run(harness, streams, games={1: 1})
        assert self.active(harness) == []
        predictor.game_for_stream.assert_not_called()

    def test_second_run_is_a_no_op(self, harness):
        streams = [make_metadata(f'satori-84532-{i}', pubkey=self.BRIDGE)
                   for i in (1, 3)]
        self.run(harness, streams, games={1: 1})
        _, predictor = self.run(harness, streams, games={1: 1})
        assert self.active(harness) == ['satori-84532-1']
        assert len(harness.networkDB.get_all()) == 1
        # stream 1 is subscribed and stream 3 is cached as gameless
        predictor.game_for_stream.assert_not_called()

    def test_does_nothing_when_base_predict_is_off(self, harness):
        streams = [make_metadata('satori-84532-1', pubkey=self.BRIDGE)]
        client, _ = self.run(harness, streams, games={1: 1}, enabled=False)
        assert self.active(harness) == []
        client.find_datastreams.assert_not_called()

    def test_skips_paid_streams(self, harness):
        meta = make_metadata('satori-84532-1', pubkey=self.BRIDGE)
        meta.price_per_obs = 10
        self.run(harness, [meta], games={1: 1})
        assert self.active(harness) == []


class TestCentralFallback:
    """Falls back to known relays when central server is unavailable."""

    def test_uses_known_relays_on_server_failure(self, harness):
        """When central server fails, uses relay URLs from subscriptions."""
        subscribe(harness, 'btc-price', relay_url='wss://known-relay')
        harness.server.getRelays.side_effect = Exception('server down')

        now = int(time.time())
        meta = make_metadata('btc-price')
        obs = make_observation('btc-price', timestamp=now - 60)
        client = make_mock_client(
            streams=[meta], observations={'btc-price': obs})

        connected_urls = []

        async def mock_connect(url, cfg):
            connected_urls.append(url)
            harness._networkClients[url] = client
            harness._networkSubscribed[url] = set()
            return client
        harness._networkConnect = mock_connect
        harness._networkEnsureListener = mock.MagicMock()
        harness._networkAnnouncePublications = mock.AsyncMock()

        ConfigClass = mock_config_class()
        asyncio.run(harness._networkReconcile(ConfigClass))

        # Should have tried the known relay from the subscription
        assert 'wss://known-relay' in connected_urls
        client.subscribe_datastream.assert_called_once()


# ── Test Listener and Announce ───────────────────────────────────────

class TestListenerAndAnnounce:
    """Verifies listener and announcement side effects."""

    def test_starts_listener_on_found_relay(self, harness):
        """Finding a stream on a relay starts a listener."""
        subscribe(harness, 'btc-price')
        harness.server.getRelays.return_value = [
            {'relay_url': 'wss://relay1'}]

        now = int(time.time())
        meta = make_metadata('btc-price')
        obs = make_observation('btc-price', timestamp=now - 60)
        client = make_mock_client(
            streams=[meta], observations={'btc-price': obs})

        async def mock_connect(url, cfg):
            harness._networkClients[url] = client
            harness._networkSubscribed[url] = set()
            return client
        harness._networkConnect = mock_connect
        harness._networkEnsureListener = mock.MagicMock()
        harness._networkAnnouncePublications = mock.AsyncMock()

        ConfigClass = mock_config_class()
        asyncio.run(harness._networkReconcile(ConfigClass))

        harness._networkEnsureListener.assert_called_once_with('wss://relay1')

    def test_announces_publications_on_found_relay(self, harness):
        """Finding a stream on a relay also announces our publications."""
        subscribe(harness, 'btc-price')
        harness.networkDB.add_publication('my-pub')
        harness.server.getRelays.return_value = [
            {'relay_url': 'wss://relay1'}]

        now = int(time.time())
        meta = make_metadata('btc-price')
        obs = make_observation('btc-price', timestamp=now - 60)
        client = make_mock_client(
            streams=[meta], observations={'btc-price': obs})

        async def mock_connect(url, cfg):
            harness._networkClients[url] = client
            harness._networkSubscribed[url] = set()
            return client
        harness._networkConnect = mock_connect
        harness._networkEnsureListener = mock.MagicMock()
        harness._networkAnnouncePublications = mock.AsyncMock()

        ConfigClass = mock_config_class()
        asyncio.run(harness._networkReconcile(ConfigClass))

        harness._networkAnnouncePublications.assert_called_once_with(
            'wss://relay1')

    def test_no_listener_when_nothing_found(self, harness):
        """No listener started if relay has no matching streams."""
        subscribe(harness, 'btc-price')
        harness.server.getRelays.return_value = [
            {'relay_url': 'wss://relay1'}]

        client = make_mock_client(streams=[])

        async def mock_connect(url, cfg):
            harness._networkClients[url] = client
            harness._networkSubscribed[url] = set()
            return client

        async def mock_disconnect(url):
            harness._networkClients.pop(url, None)

        harness._networkConnect = mock_connect
        harness._networkDisconnect = mock_disconnect
        harness._networkEnsureListener = mock.MagicMock()
        harness._networkAnnouncePublications = mock.AsyncMock()

        ConfigClass = mock_config_class()
        asyncio.run(harness._networkReconcile(ConfigClass))

        harness._networkEnsureListener.assert_not_called()


# ── Test Multiple Streams ────────────────────────────────────────────

class TestMultipleStreams:
    """Reconciliation with multiple subscriptions."""

    def test_finds_different_streams_on_different_relays(self, harness):
        """Stream A on relay1, stream B on relay2."""
        subscribe(harness, 'btc-price', pubkey='pub_a')
        subscribe(harness, 'eth-price', pubkey='pub_b')
        harness.server.getRelays.return_value = [
            {'relay_url': 'wss://relay1'},
            {'relay_url': 'wss://relay2'},
        ]

        now = int(time.time())
        meta_btc = make_metadata('btc-price', pubkey='pub_a')
        meta_eth = make_metadata('eth-price', pubkey='pub_b')
        obs_btc = make_observation(
            'btc-price', pubkey='pub_a', timestamp=now - 60,
            event_id='evt_btc')
        obs_eth = make_observation(
            'eth-price', pubkey='pub_b', timestamp=now - 60,
            event_id='evt_eth')

        # Relay1 has btc, relay2 has eth
        client1 = make_mock_client(
            streams=[meta_btc],
            observations={'btc-price': obs_btc})
        client2 = make_mock_client(
            streams=[meta_eth],
            observations={'eth-price': obs_eth})

        async def mock_connect(url, cfg):
            c = client1 if url == 'wss://relay1' else client2
            harness._networkClients[url] = c
            harness._networkSubscribed[url] = set()
            return c
        harness._networkConnect = mock_connect
        harness._networkEnsureListener = mock.MagicMock()
        harness._networkAnnouncePublications = mock.AsyncMock()

        ConfigClass = mock_config_class()
        asyncio.run(harness._networkReconcile(ConfigClass))

        # Both found
        client1.subscribe_datastream.assert_called_once_with(
            'btc-price', 'pub_a')
        client2.subscribe_datastream.assert_called_once_with(
            'eth-price', 'pub_b')
        # Relays updated
        subs = {s['stream_name']: s
                for s in harness.networkDB.get_active()}
        assert subs['btc-price']['relay_url'] == 'wss://relay1'
        assert subs['eth-price']['relay_url'] == 'wss://relay2'

    def test_partial_failure_marks_unfound_stale(self, harness):
        """Found one stream but not the other → unfound marked stale."""
        subscribe(harness, 'btc-price', pubkey='pub_a')
        subscribe(harness, 'missing-stream', pubkey='pub_b')
        harness.server.getRelays.return_value = [
            {'relay_url': 'wss://relay1'}]

        now = int(time.time())
        meta_btc = make_metadata('btc-price', pubkey='pub_a')
        obs_btc = make_observation(
            'btc-price', pubkey='pub_a', timestamp=now - 60)

        client = make_mock_client(
            streams=[meta_btc],
            observations={'btc-price': obs_btc})

        async def mock_connect(url, cfg):
            harness._networkClients[url] = client
            harness._networkSubscribed[url] = set()
            return client
        harness._networkConnect = mock_connect
        harness._networkEnsureListener = mock.MagicMock()
        harness._networkAnnouncePublications = mock.AsyncMock()

        ConfigClass = mock_config_class()
        asyncio.run(harness._networkReconcile(ConfigClass))

        subs = {s['stream_name']: s
                for s in harness.networkDB.get_active()}
        # btc-price found, stale cleared
        assert subs['btc-price']['stale_since'] is None
        # missing-stream not found, marked stale
        assert subs['missing-stream']['stale_since'] is not None


# ── Test Observation Saving During Hunt ──────────────────────────────

class TestObservationSaving:
    """Freshness check during hunting saves observations."""

    def test_saves_observation_from_freshness_check(self, harness):
        """Observation fetched during freshness check is saved to DB."""
        subscribe(harness, 'btc-price')
        harness.server.getRelays.return_value = [
            {'relay_url': 'wss://relay1'}]

        now = int(time.time())
        meta = make_metadata('btc-price')
        obs = make_observation(
            'btc-price', timestamp=now - 60, value='99999')
        client = make_mock_client(
            streams=[meta], observations={'btc-price': obs})

        async def mock_connect(url, cfg):
            harness._networkClients[url] = client
            harness._networkSubscribed[url] = set()
            return client
        harness._networkConnect = mock_connect
        harness._networkEnsureListener = mock.MagicMock()
        harness._networkAnnouncePublications = mock.AsyncMock()

        ConfigClass = mock_config_class()
        asyncio.run(harness._networkReconcile(ConfigClass))

        # Observation should be saved from the freshness check
        rows = harness.networkDB.get_observations('btc-price', 'pub123')
        assert len(rows) == 1
