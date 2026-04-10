"""Tests for competition API routes (Phase 1).

Uses a minimal Flask app with a real NetworkDB and MockStartup,
matching the pattern in test_network_routes.py.
"""
import importlib.util
import json
import os
import tempfile
import time
import pytest
from flask import Flask, jsonify, request

_spec = importlib.util.spec_from_file_location(
    'network_db',
    os.path.join(os.path.dirname(__file__),
                 '..', 'neuron-lite', 'satorineuron', 'network_db.py'))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
NetworkDB = _mod.NetworkDB


class MockStartup:
    def __init__(self, db_path):
        self.networkDB = NetworkDB(db_path)
        self.nostrPubkey = 'host_nostr_pubkey'
        self._announce_calls = []
        self._close_calls = []
        self._discover_result = []

    def announceCompetitionSync(self, competition: dict):
        self._announce_calls.append(competition)

    def closeCompetitionSync(self, stream_name: str,
                             stream_provider_pubkey: str):
        self._close_calls.append((stream_name, stream_provider_pubkey))

    def discoverCompetitionsSync(self) -> list:
        return self._discover_result


def create_test_app(startup):
    app = Flask(__name__)
    app.config['SECRET_KEY'] = 'test'
    app.config['TESTING'] = True

    def get_startup():
        return startup

    @app.route('/api/competition', methods=['POST'])
    def api_competition_create():
        s = get_startup()
        if not s:
            return jsonify({'error': 'not ready'}), 503
        data = request.get_json() or {}
        required = ['stream_name', 'stream_provider_pubkey',
                    'pay_per_obs_sats', 'paid_predictors',
                    'competing_predictors', 'scoring_metric']
        for field in required:
            if field not in data:
                return jsonify({'error': f'missing {field}'}), 400
        try:
            pay = int(data['pay_per_obs_sats'])
            paid = int(data['paid_predictors'])
            competing = int(data['competing_predictors'])
        except (ValueError, TypeError):
            return jsonify({'error': 'invalid number fields'}), 400
        s.networkDB.add_competition(
            stream_name=data['stream_name'],
            stream_provider_pubkey=data['stream_provider_pubkey'],
            host_pubkey=s.nostrPubkey,
            pay_per_obs_sats=pay,
            paid_predictors=paid,
            competing_predictors=competing,
            scoring_metric=data['scoring_metric'],
            scoring_params=json.dumps(data.get('scoring_params', {})),
            horizon=int(data.get('horizon', 1)),
            active=1,
            timestamp=int(time.time()),
        )
        s.announceCompetitionSync(data)
        return jsonify({'success': True})

    @app.route('/api/competition/close', methods=['POST'])
    def api_competition_close():
        s = get_startup()
        if not s:
            return jsonify({'error': 'not ready'}), 503
        data = request.get_json() or {}
        stream_name = data.get('stream_name', '').strip()
        provider_pubkey = data.get('stream_provider_pubkey', '').strip()
        if not stream_name or not provider_pubkey:
            return jsonify({'error': 'missing fields'}), 400
        s.networkDB.close_competition(
            stream_name, provider_pubkey, s.nostrPubkey)
        s.closeCompetitionSync(stream_name, provider_pubkey)
        return jsonify({'success': True})

    @app.route('/api/competitions/mine', methods=['GET'])
    def api_competitions_mine():
        s = get_startup()
        if not s:
            return jsonify({'error': 'not ready'}), 503
        rows = s.networkDB.get_competitions_hosted_by(s.nostrPubkey)
        return jsonify({'competitions': rows})

    @app.route('/api/competitions/discover', methods=['GET'])
    def api_competitions_discover():
        s = get_startup()
        if not s:
            return jsonify({'error': 'not ready'}), 503
        return jsonify({'competitions': s.discoverCompetitionsSync()})

    @app.route('/api/competitions', methods=['GET'])
    def api_competitions_all():
        s = get_startup()
        if not s:
            return jsonify({'error': 'not ready'}), 503
        active_only = request.args.get('active', '1') == '1'
        rows = s.networkDB.get_all_competitions(active_only=active_only)
        return jsonify({'competitions': rows})

    return app


@pytest.fixture
def client():
    with tempfile.TemporaryDirectory() as tmpdir:
        startup = MockStartup(os.path.join(tmpdir, 'test.db'))
        app = create_test_app(startup)
        app.startup = startup
        with app.test_client() as c:
            c.startup = startup
            yield c


def post_json(client, url, data):
    return client.post(url, data=json.dumps(data),
                       content_type='application/json')


# ── POST /api/competition ──────────────────────────────────────────

class TestCreateCompetition:

    def test_creates_and_announces(self, client):
        resp = post_json(client, '/api/competition', {
            'stream_name': 'btc-price',
            'stream_provider_pubkey': 'aabbcc',
            'pay_per_obs_sats': 300,
            'paid_predictors': 3,
            'competing_predictors': 5,
            'scoring_metric': 'mae',
        })
        assert resp.status_code == 200
        assert resp.get_json()['success'] is True
        assert len(client.startup._announce_calls) == 1

    def test_missing_field_returns_400(self, client):
        resp = post_json(client, '/api/competition', {
            'stream_name': 'btc-price',
        })
        assert resp.status_code == 400

    def test_persists_to_db(self, client):
        post_json(client, '/api/competition', {
            'stream_name': 'btc-price',
            'stream_provider_pubkey': 'aabbcc',
            'pay_per_obs_sats': 300,
            'paid_predictors': 3,
            'competing_predictors': 5,
            'scoring_metric': 'mae',
        })
        resp = client.get('/api/competitions/mine')
        data = resp.get_json()
        assert len(data['competitions']) == 1
        assert data['competitions'][0]['stream_name'] == 'btc-price'


# ── POST /api/competition/close ────────────────────────────────────

class TestCloseCompetition:

    def test_closes_and_notifies(self, client):
        post_json(client, '/api/competition', {
            'stream_name': 'btc-price',
            'stream_provider_pubkey': 'aabbcc',
            'pay_per_obs_sats': 300,
            'paid_predictors': 3,
            'competing_predictors': 5,
            'scoring_metric': 'mae',
        })
        resp = post_json(client, '/api/competition/close', {
            'stream_name': 'btc-price',
            'stream_provider_pubkey': 'aabbcc',
        })
        assert resp.status_code == 200
        assert len(client.startup._close_calls) == 1

    def test_sets_inactive_in_db(self, client):
        post_json(client, '/api/competition', {
            'stream_name': 'btc-price',
            'stream_provider_pubkey': 'aabbcc',
            'pay_per_obs_sats': 300,
            'paid_predictors': 3,
            'competing_predictors': 5,
            'scoring_metric': 'mae',
        })
        post_json(client, '/api/competition/close', {
            'stream_name': 'btc-price',
            'stream_provider_pubkey': 'aabbcc',
        })
        resp = client.get('/api/competitions?active=1')
        data = resp.get_json()
        assert data['competitions'] == []

    def test_missing_fields_returns_400(self, client):
        resp = post_json(client, '/api/competition/close', {})
        assert resp.status_code == 400


# ── GET /api/competitions/mine ─────────────────────────────────────

class TestMyCompetitions:

    def test_empty(self, client):
        resp = client.get('/api/competitions/mine')
        assert resp.get_json()['competitions'] == []

    def test_only_returns_mine(self, client):
        post_json(client, '/api/competition', {
            'stream_name': 'btc-price',
            'stream_provider_pubkey': 'aabbcc',
            'pay_per_obs_sats': 300,
            'paid_predictors': 3,
            'competing_predictors': 5,
            'scoring_metric': 'mae',
        })
        resp = client.get('/api/competitions/mine')
        data = resp.get_json()
        assert len(data['competitions']) == 1
        assert data['competitions'][0]['host_pubkey'] == 'host_nostr_pubkey'


# ── GET /api/competitions/discover ────────────────────────────────

class TestDiscoverCompetitions:

    def test_returns_discover_result(self, client):
        client.startup._discover_result = [
            {'stream_name': 'btc-price', 'pay_per_obs_sats': 300}
        ]
        resp = client.get('/api/competitions/discover')
        data = resp.get_json()
        assert len(data['competitions']) == 1
        assert data['competitions'][0]['stream_name'] == 'btc-price'
