"""Tests for NetworkDB — local SQLite storage for network datastreams."""

import importlib.util
import os
import sys
import tempfile
import time
import pytest

# Import NetworkDB directly to avoid __init__.py dependency chain
_spec = importlib.util.spec_from_file_location(
    'network_db',
    os.path.join(os.path.dirname(__file__),
                 '..', 'neuron-lite', 'satorineuron', 'network_db.py'))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
NetworkDB = _mod.NetworkDB


@pytest.fixture
def db():
    """Create a fresh NetworkDB in a temp directory for each test."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, 'test_network.db')
        yield NetworkDB(db_path)


@pytest.fixture
def sample_stream():
    return {
        'stream_name': 'btc-price',
        'nostr_pubkey': 'abc123',
        'name': 'Bitcoin Price',
        'description': 'BTC/USD spot price',
        'cadence_seconds': 3600,
        'price_per_obs': 0,
        'encrypted': False,
        'tags': ['bitcoin', 'price'],
    }


# ── Schema ────────────────────────────────────────────────────────


class TestSchema:

    def test_creates_all_tables(self, db):
        conn = db._get_conn()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = {r['name'] for r in tables}
        assert names >= {
            'subscriptions', 'observations', 'relays',
            'publications', 'predictions', 'data_sources',
        }

    def test_idempotent_schema_init(self, db):
        """Calling _init_schema twice shouldn't fail."""
        db._init_schema()
        conn = db._get_conn()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        assert len(tables) >= 6


# ── Subscriptions ─────────────────────────────────────────────────


class TestSubscriptions:

    def test_subscribe(self, db, sample_stream):
        row_id = db.subscribe(sample_stream, 'wss://relay1.example.com')
        assert row_id > 0

    def test_subscribe_returns_in_active(self, db, sample_stream):
        db.subscribe(sample_stream, 'wss://relay1.example.com')
        active = db.get_active()
        assert len(active) == 1
        assert active[0]['stream_name'] == 'btc-price'
        assert active[0]['provider_pubkey'] == 'abc123'
        assert active[0]['active'] == 1

    def test_subscribe_saves_metadata(self, db, sample_stream):
        db.subscribe(sample_stream, 'wss://relay1.example.com')
        sub = db.get_active()[0]
        assert sub['name'] == 'Bitcoin Price'
        assert sub['description'] == 'BTC/USD spot price'
        assert sub['cadence_seconds'] == 3600
        assert sub['price_per_obs'] == 0
        assert sub['encrypted'] == 0
        assert sub['tags'] == 'bitcoin,price'

    def test_subscribe_upsert(self, db, sample_stream):
        """Subscribing again updates fields instead of duplicating."""
        db.subscribe(sample_stream, 'wss://relay1.example.com')
        sample_stream['name'] = 'BTC Price Updated'
        db.subscribe(sample_stream, 'wss://relay2.example.com')
        active = db.get_active()
        assert len(active) == 1
        assert active[0]['name'] == 'BTC Price Updated'
        assert active[0]['relay_url'] == 'wss://relay2.example.com'

    def test_unsubscribe(self, db, sample_stream):
        db.subscribe(sample_stream, 'wss://relay1.example.com')
        db.unsubscribe('btc-price', 'abc123')
        assert len(db.get_active()) == 0
        assert len(db.get_all()) == 1
        sub = db.get_all()[0]
        assert sub['active'] == 0
        assert sub['unsubscribed_at'] is not None

    def test_resubscribe_after_unsubscribe(self, db, sample_stream):
        db.subscribe(sample_stream, 'wss://relay1.example.com')
        db.unsubscribe('btc-price', 'abc123')
        db.subscribe(sample_stream, 'wss://relay1.example.com')
        active = db.get_active()
        assert len(active) == 1
        assert active[0]['active'] == 1

    def test_is_subscribed(self, db, sample_stream):
        assert not db.is_subscribed('btc-price', 'abc123')
        db.subscribe(sample_stream, 'wss://relay1.example.com')
        assert db.is_subscribed('btc-price', 'abc123')
        db.unsubscribe('btc-price', 'abc123')
        assert not db.is_subscribed('btc-price', 'abc123')

    def test_mark_stale_and_clear(self, db, sample_stream):
        db.subscribe(sample_stream, 'wss://relay1.example.com')
        db.mark_stale('btc-price', 'abc123')
        sub = db.get_active()[0]
        assert sub['stale_since'] is not None
        db.clear_stale('btc-price', 'abc123')
        sub = db.get_active()[0]
        assert sub['stale_since'] is None

    def test_update_relay(self, db, sample_stream):
        db.subscribe(sample_stream, 'wss://relay1.example.com')
        db.mark_stale('btc-price', 'abc123')
        db.update_relay('btc-price', 'abc123', 'wss://relay2.example.com')
        sub = db.get_active()[0]
        assert sub['relay_url'] == 'wss://relay2.example.com'
        assert sub['stale_since'] is None

    def test_should_recheck_stale(self, db):
        assert db.should_recheck_stale(None) is True
        assert db.should_recheck_stale(int(time.time())) is False
        assert db.should_recheck_stale(int(time.time()) - 100000) is True

    def test_subscribe_also_upserts_relay(self, db, sample_stream):
        db.subscribe(sample_stream, 'wss://relay1.example.com')
        relays = db.get_relays()
        assert any(r['relay_url'] == 'wss://relay1.example.com' for r in relays)


# ── Observations ──────────────────────────────────────────────────


class TestObservations:

    def test_save_and_get(self, db):
        db.save_observation('btc-price', 'abc123', '42000.50',
                            'evt1', seq_num=1, observed_at=1000)
        obs = db.get_observations('btc-price', 'abc123')
        assert len(obs) == 1
        assert obs[0]['value'] == '42000.50'
        assert obs[0]['seq_num'] == 1
        assert obs[0]['observed_at'] == 1000
        assert obs[0]['event_id'] == 'evt1'
        assert obs[0]['received_at'] is not None

    def test_dedup_by_event_id(self, db):
        db.save_observation('btc-price', 'abc123', '42000', 'evt1')
        db.save_observation('btc-price', 'abc123', '42001', 'evt1')
        obs = db.get_observations('btc-price', 'abc123')
        assert len(obs) == 1
        assert obs[0]['value'] == '42000'

    def test_no_dedup_without_event_id(self, db):
        """Observations without event_id are always inserted."""
        db.save_observation('btc-price', 'abc123', '42000')
        db.save_observation('btc-price', 'abc123', '42001')
        obs = db.get_observations('btc-price', 'abc123')
        assert len(obs) == 2

    def test_different_event_ids_both_saved(self, db):
        db.save_observation('btc-price', 'abc123', '42000', 'evt1')
        db.save_observation('btc-price', 'abc123', '42001', 'evt2')
        obs = db.get_observations('btc-price', 'abc123')
        assert len(obs) == 2

    def test_get_observations_ordered_newest_first(self, db):
        db.save_observation('btc-price', 'abc123', '100', 'e1')
        time.sleep(1.1)  # ensure different received_at timestamps
        db.save_observation('btc-price', 'abc123', '200', 'e2')
        obs = db.get_observations('btc-price', 'abc123')
        assert obs[0]['value'] == '200'
        assert obs[1]['value'] == '100'

    def test_get_observations_respects_limit(self, db):
        for i in range(10):
            db.save_observation('btc-price', 'abc123', str(i), f'e{i}')
        obs = db.get_observations('btc-price', 'abc123', limit=3)
        assert len(obs) == 3

    def test_get_observations_filters_by_stream(self, db):
        db.save_observation('btc-price', 'abc123', '100', 'e1')
        db.save_observation('eth-price', 'abc123', '200', 'e2')
        obs = db.get_observations('btc-price', 'abc123')
        assert len(obs) == 1
        assert obs[0]['stream_name'] == 'btc-price'

    def test_last_observation_time(self, db):
        assert db.last_observation_time('btc-price', 'abc123') is None
        db.save_observation('btc-price', 'abc123', '100', 'e1')
        t = db.last_observation_time('btc-price', 'abc123')
        assert t is not None
        assert abs(t - int(time.time())) < 2

    def test_is_locally_stale_no_observations(self, db):
        assert db.is_locally_stale('btc-price', 'abc123', 3600) is True

    def test_is_locally_stale_recent_observation(self, db):
        db.save_observation('btc-price', 'abc123', '100', 'e1')
        assert db.is_locally_stale('btc-price', 'abc123', 3600) is False

    def test_is_locally_stale_no_cadence_always_live(self, db):
        """Streams with no cadence are never stale."""
        assert db.is_locally_stale('btc-price', 'abc123', None) is True  # no obs = stale
        db.save_observation('btc-price', 'abc123', '100', 'e1')
        assert db.is_locally_stale('btc-price', 'abc123', None) is False
        assert db.is_locally_stale('btc-price', 'abc123', 0) is False


# ── Relays ────────────────────────────────────────────────────────


class TestRelays:

    def test_upsert_and_get(self, db):
        db.upsert_relay('wss://relay1.example.com')
        relays = db.get_relays()
        assert len(relays) == 1
        assert relays[0]['relay_url'] == 'wss://relay1.example.com'

    def test_upsert_updates_last_active(self, db):
        db.upsert_relay('wss://relay1.example.com')
        first = db.get_relays()[0]['last_active']
        time.sleep(0.01)
        db.upsert_relay('wss://relay1.example.com')
        second = db.get_relays()[0]['last_active']
        assert second >= first

    def test_upsert_no_duplicates(self, db):
        db.upsert_relay('wss://relay1.example.com')
        db.upsert_relay('wss://relay1.example.com')
        assert len(db.get_relays()) == 1

    def test_delete_relay(self, db):
        db.upsert_relay('wss://relay1.example.com')
        db.delete_relay('wss://relay1.example.com')
        assert len(db.get_relays()) == 0

    def test_multiple_relays_ordered(self, db):
        db.upsert_relay('wss://old.example.com')
        time.sleep(1.1)  # ensure different last_active timestamps
        db.upsert_relay('wss://new.example.com')
        relays = db.get_relays()
        assert relays[0]['relay_url'] == 'wss://new.example.com'


# ── Publications ──────────────────────────────────────────────────


class TestPublications:

    def test_add_publication(self, db):
        row_id = db.add_publication('my-stream', name='My Stream')
        assert row_id > 0
        pubs = db.get_active_publications()
        assert len(pubs) == 1
        assert pubs[0]['stream_name'] == 'my-stream'
        assert pubs[0]['active'] == 1
        assert pubs[0]['last_seq_num'] == 0

    def test_add_publication_upsert(self, db):
        db.add_publication('my-stream', name='V1')
        db.add_publication('my-stream', name='V2')
        pubs = db.get_active_publications()
        assert len(pubs) == 1
        assert pubs[0]['name'] == 'V2'

    def test_add_publication_with_source(self, db):
        db.add_publication('btc-price_pred',
                           source_stream_name='btc-price',
                           source_provider_pubkey='abc123')
        pub = db.get_active_publications()[0]
        assert pub['source_stream_name'] == 'btc-price'
        assert pub['source_provider_pubkey'] == 'abc123'

    def test_remove_publication(self, db):
        db.add_publication('my-stream')
        db.remove_publication('my-stream')
        assert len(db.get_active_publications()) == 0
        assert len(db.get_all_publications()) == 1

    def test_is_predicting(self, db):
        assert not db.is_predicting('btc-price', 'abc123')
        db.add_publication('btc-price_pred',
                           source_stream_name='btc-price',
                           source_provider_pubkey='abc123')
        assert db.is_predicting('btc-price', 'abc123')
        db.remove_publication('btc-price_pred')
        assert not db.is_predicting('btc-price', 'abc123')

    def test_mark_published_increments_seq(self, db):
        db.add_publication('my-stream')
        seq1 = db.mark_published('my-stream')
        seq2 = db.mark_published('my-stream')
        seq3 = db.mark_published('my-stream')
        assert seq1 == 1
        assert seq2 == 2
        assert seq3 == 3

    def test_mark_published_updates_timestamp(self, db):
        db.add_publication('my-stream')
        db.mark_published('my-stream')
        pub = db.get_active_publications()[0]
        assert pub['last_published_at'] is not None
        assert abs(pub['last_published_at'] - int(time.time())) < 2

    def test_publication_with_pricing(self, db):
        db.add_publication('paid-stream', price_per_obs=100, encrypted=True)
        pub = db.get_active_publications()[0]
        assert pub['price_per_obs'] == 100
        assert pub['encrypted'] == 1

    def test_publication_no_cadence(self, db):
        db.add_publication('ext-stream', cadence_seconds=None)
        pub = db.get_active_publications()[0]
        assert pub['cadence_seconds'] is None


# ── Predictions ───────────────────────────────────────────────────


class TestPredictions:

    def test_save_and_get(self, db):
        pred_id = db.save_prediction('btc-price', 'abc123', '42500',
                                     observation_seq=5, observed_at=1000)
        assert pred_id > 0
        preds = db.get_predictions('btc-price', 'abc123')
        assert len(preds) == 1
        assert preds[0]['value'] == '42500'
        assert preds[0]['observation_seq'] == 5
        assert preds[0]['published'] == 0

    def test_get_predictions_by_stream_only(self, db):
        db.save_prediction('btc-price', 'abc123', '100')
        db.save_prediction('btc-price', 'def456', '200')
        preds = db.get_predictions('btc-price')
        assert len(preds) == 2

    def test_get_predictions_by_stream_and_pubkey(self, db):
        db.save_prediction('btc-price', 'abc123', '100')
        db.save_prediction('btc-price', 'def456', '200')
        preds = db.get_predictions('btc-price', 'abc123')
        assert len(preds) == 1
        assert preds[0]['value'] == '100'

    def test_mark_prediction_published(self, db):
        pred_id = db.save_prediction('btc-price', 'abc123', '42500')
        db.mark_prediction_published(pred_id)
        preds = db.get_predictions('btc-price', 'abc123')
        assert preds[0]['published'] == 1

    def test_get_unpublished_predictions(self, db):
        p1 = db.save_prediction('btc-price', 'abc123', '100')
        db.save_prediction('btc-price', 'abc123', '200')
        db.mark_prediction_published(p1)
        unpub = db.get_unpublished_predictions()
        assert len(unpub) == 1
        assert unpub[0]['value'] == '200'

    def test_unpublished_ordered_oldest_first(self, db):
        db.save_prediction('btc-price', 'abc123', 'first')
        db.save_prediction('btc-price', 'abc123', 'second')
        unpub = db.get_unpublished_predictions()
        assert unpub[0]['value'] == 'first'
        assert unpub[1]['value'] == 'second'


# ── Data Sources ──────────────────────────────────────────────────


class TestDataSources:

    def test_add_and_get(self, db):
        row_id = db.add_data_source(
            'btc-price', url='https://api.example.com/price',
            cadence_seconds=3600, parser_type='json_path',
            parser_config='data.price', name='BTC Price')
        assert row_id > 0
        ds = db.get_data_source('btc-price')
        assert ds is not None
        assert ds['url'] == 'https://api.example.com/price'
        assert ds['cadence_seconds'] == 3600
        assert ds['parser_type'] == 'json_path'
        assert ds['parser_config'] == 'data.price'

    def test_add_upsert(self, db):
        db.add_data_source('btc-price', url='https://old.com',
                           cadence_seconds=60, parser_type='json_path',
                           parser_config='old')
        db.add_data_source('btc-price', url='https://new.com',
                           cadence_seconds=120, parser_type='json_path',
                           parser_config='new')
        sources = db.get_active_data_sources()
        assert len(sources) == 1
        assert sources[0]['url'] == 'https://new.com'
        assert sources[0]['parser_config'] == 'new'

    def test_add_without_url(self, db):
        """External-push data source with no URL."""
        row_id = db.add_data_source('ext-stream', name='External')
        assert row_id > 0
        ds = db.get_data_source('ext-stream')
        assert ds['url'] == ''
        assert ds['cadence_seconds'] == 0

    def test_remove_data_source(self, db):
        db.add_data_source('btc-price', url='https://example.com',
                           cadence_seconds=60, parser_type='json_path',
                           parser_config='data.price')
        db.remove_data_source('btc-price')
        assert len(db.get_active_data_sources()) == 0
        assert len(db.get_all_data_sources()) == 1

    def test_get_data_source_not_found(self, db):
        assert db.get_data_source('nonexistent') is None

    def test_reactivate_after_remove(self, db):
        db.add_data_source('btc-price', url='https://example.com',
                           cadence_seconds=60, parser_type='json_path',
                           parser_config='x')
        db.remove_data_source('btc-price')
        db.add_data_source('btc-price', url='https://example.com',
                           cadence_seconds=60, parser_type='json_path',
                           parser_config='x')
        assert len(db.get_active_data_sources()) == 1

    def test_data_source_with_headers(self, db):
        db.add_data_source('btc-price', url='https://example.com',
                           cadence_seconds=60, parser_type='json_path',
                           parser_config='x',
                           headers='{"Authorization": "Bearer tok"}')
        ds = db.get_data_source('btc-price')
        assert ds['headers'] == '{"Authorization": "Bearer tok"}'

    def test_data_source_python_parser(self, db):
        db.add_data_source('btc-price', url='https://example.com',
                           cadence_seconds=60, parser_type='python',
                           parser_config='return text.split(",")[0]')
        ds = db.get_data_source('btc-price')
        assert ds['parser_type'] == 'python'


# ── Cross-table interactions ──────────────────────────────────────


class TestCrossTable:

    def test_subscribe_creates_relay(self, db, sample_stream):
        """Subscribing should also upsert the relay."""
        db.subscribe(sample_stream, 'wss://relay1.example.com')
        relays = db.get_relays()
        urls = [r['relay_url'] for r in relays]
        assert 'wss://relay1.example.com' in urls

    def test_prediction_flow(self, db, sample_stream):
        """Full flow: subscribe → predict → save prediction → publish."""
        db.subscribe(sample_stream, 'wss://relay1.example.com')
        db.add_publication('btc-price_pred',
                           source_stream_name='btc-price',
                           source_provider_pubkey='abc123')
        assert db.is_predicting('btc-price', 'abc123')
        pred_id = db.save_prediction('btc-price', 'abc123', '42500',
                                     observation_seq=1)
        db.mark_prediction_published(pred_id)
        seq = db.mark_published('btc-price_pred')
        assert seq == 1
        preds = db.get_predictions('btc-price', 'abc123')
        assert preds[0]['published'] == 1

    def test_data_source_publication_flow(self, db):
        """Data source creates both a data_source and publication record."""
        db.add_data_source('weather', url='https://api.weather.com',
                           cadence_seconds=3600, parser_type='json_path',
                           parser_config='temp')
        db.add_publication('weather', cadence_seconds=3600)
        pubs = db.get_active_publications()
        sources = db.get_active_data_sources()
        assert len(pubs) == 1
        assert len(sources) == 1
        assert pubs[0]['stream_name'] == sources[0]['stream_name']

    def test_external_push_flow(self, db):
        """External data source: no URL, no cadence, just a publication slot."""
        db.add_data_source('ext-stream')
        db.add_publication('ext-stream')
        ds = db.get_data_source('ext-stream')
        assert ds['url'] == ''
        assert ds['cadence_seconds'] == 0
        # Publishing still works
        seq = db.mark_published('ext-stream')
        assert seq == 1

    def test_multiple_streams_isolated(self, db):
        """Operations on one stream don't affect another."""
        db.save_observation('btc-price', 'abc', '100', 'e1')
        db.save_observation('eth-price', 'abc', '200', 'e2')
        db.save_prediction('btc-price', 'abc', '101')
        assert len(db.get_observations('btc-price', 'abc')) == 1
        assert len(db.get_observations('eth-price', 'abc')) == 1
        assert len(db.get_predictions('btc-price', 'abc')) == 1
        assert len(db.get_predictions('eth-price', 'abc')) == 0


# ── Round dedupe (bridge streams) ────────────────────────────────────


class TestRoundDedupe:
    """Bridge streams key idempotency on the on-chain round.

    A bridge restart republishes the same round under a FRESH event id and a
    BUMPED seq_num — both existing guards pass, so without the round check the
    replay reaches the engine as a genuine new observation.
    """

    def test_same_round_fresh_event_and_seq_is_a_duplicate(self, db):
        assert db.save_observation(
            'satori-84532-1', 'bridgepub', '{"raw":"1"}',
            'evt-a', seq_num=1, round_num=41373) is True
        # the replay: same round, everything about the delivery different
        assert db.save_observation(
            'satori-84532-1', 'bridgepub', '{"raw":"1"}',
            'evt-b', seq_num=7, round_num=41373) is False
        assert len(db.get_observations('satori-84532-1', 'bridgepub')) == 1

    def test_distinct_rounds_both_land(self, db):
        assert db.save_observation(
            'satori-84532-1', 'bridgepub', '{"raw":"0"}',
            'e1', seq_num=1, round_num=41373) is True
        assert db.save_observation(
            'satori-84532-1', 'bridgepub', '{"raw":"1"}',
            'e2', seq_num=2, round_num=41374) is True
        assert len(db.get_observations('satori-84532-1', 'bridgepub')) == 2

    def test_round_is_scoped_to_stream_and_publisher(self, db):
        """Round 41373 on stream 1 and round 41373 on stream 2 are different
        observations; so are the same round from two publishers."""
        assert db.save_observation(
            'satori-84532-1', 'bridgepub', '{"raw":"1"}',
            'e1', seq_num=1, round_num=41373) is True
        assert db.save_observation(
            'satori-84532-2', 'bridgepub', '{"raw":"1"}',
            'e2', seq_num=1, round_num=41373) is True
        assert db.save_observation(
            'satori-84532-1', 'otherpub', '{"raw":"1"}',
            'e3', seq_num=1, round_num=41373) is True

    def test_ordinary_streams_unaffected(self, db):
        """No round passed: exactly the pre-existing behaviour, including that
        rows without event ids or seqs are never deduped against each other."""
        assert db.save_observation('btc-price', 'abc', '100') is True
        assert db.save_observation('btc-price', 'abc', '100') is True

    def test_migration_adds_round_to_an_existing_db(self, tmp_path):
        """An existing database (no round column) must migrate on open, keep
        its rows, and dedupe from then on."""
        import sqlite3 as sql
        path = str(tmp_path / 'old.db')
        conn = sql.connect(path)
        conn.execute("""
            CREATE TABLE observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stream_name TEXT NOT NULL,
                provider_pubkey TEXT NOT NULL,
                seq_num INTEGER,
                observed_at INTEGER,
                received_at INTEGER NOT NULL,
                value TEXT,
                event_id TEXT
            )
        """)
        conn.execute(
            "INSERT INTO observations (stream_name, provider_pubkey, seq_num,"
            " received_at, value, event_id) VALUES ('s', 'p', 1, 0, 'v', 'e0')")
        conn.commit()
        conn.close()
        db2 = NetworkDB(path)  # module-level import, same as every other test
        assert len(db2.get_observations('s', 'p')) == 1, 'existing rows kept'
        assert db2.save_observation(
            's', 'p', 'v2', 'e1', seq_num=2, round_num=41373) is True
        assert db2.save_observation(
            's', 'p', 'v2', 'e2', seq_num=3, round_num=41373) is False
