"""Tests for bounty API routes (Phase 1).

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

    def announceBountySync(self, bounty: dict):
        self._announce_calls.append(bounty)

    def closeBountySync(self, stream_name: str,
                             stream_provider_pubkey: str):
        self._close_calls.append((stream_name, stream_provider_pubkey))

    def discoverBountiesSync(self) -> list:
        return self._discover_result


def create_test_app(startup):
    app = Flask(__name__)
    app.config['SECRET_KEY'] = 'test'
    app.config['TESTING'] = True

    def get_startup():
        return startup

    @app.route('/api/bounty', methods=['POST'])
    def api_bounty_create():
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
        s.networkDB.add_bounty(
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
        s.announceBountySync(data)
        return jsonify({'success': True})

    @app.route('/api/bounty/close', methods=['POST'])
    def api_bounty_close():
        s = get_startup()
        if not s:
            return jsonify({'error': 'not ready'}), 503
        data = request.get_json() or {}
        stream_name = data.get('stream_name', '').strip()
        provider_pubkey = data.get('stream_provider_pubkey', '').strip()
        if not stream_name or not provider_pubkey:
            return jsonify({'error': 'missing fields'}), 400
        s.networkDB.close_bounty(
            stream_name, provider_pubkey, s.nostrPubkey)
        s.closeBountySync(stream_name, provider_pubkey)
        return jsonify({'success': True})

    @app.route('/api/bounties/mine', methods=['GET'])
    def api_bounties_mine():
        s = get_startup()
        if not s:
            return jsonify({'error': 'not ready'}), 503
        rows = s.networkDB.get_bounties_hosted_by(s.nostrPubkey)
        return jsonify({'bounties': rows})

    @app.route('/api/bounties/discover', methods=['GET'])
    def api_bounties_discover():
        s = get_startup()
        if not s:
            return jsonify({'error': 'not ready'}), 503
        return jsonify({'bounties': s.discoverBountiesSync()})

    @app.route('/api/bounties', methods=['GET'])
    def api_bounties_all():
        s = get_startup()
        if not s:
            return jsonify({'error': 'not ready'}), 503
        active_only = request.args.get('active', '1') == '1'
        rows = s.networkDB.get_all_bounties(active_only=active_only)
        return jsonify({'bounties': rows})

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


# ── POST /api/bounty ──────────────────────────────────────────

class TestCreateBounty:

    def test_creates_and_announces(self, client):
        resp = post_json(client, '/api/bounty', {
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
        resp = post_json(client, '/api/bounty', {
            'stream_name': 'btc-price',
        })
        assert resp.status_code == 400

    def test_persists_to_db(self, client):
        post_json(client, '/api/bounty', {
            'stream_name': 'btc-price',
            'stream_provider_pubkey': 'aabbcc',
            'pay_per_obs_sats': 300,
            'paid_predictors': 3,
            'competing_predictors': 5,
            'scoring_metric': 'mae',
        })
        resp = client.get('/api/bounties/mine')
        data = resp.get_json()
        assert len(data['bounties']) == 1
        assert data['bounties'][0]['stream_name'] == 'btc-price'


# ── POST /api/bounty/close ────────────────────────────────────

class TestCloseBounty:

    def test_closes_and_notifies(self, client):
        post_json(client, '/api/bounty', {
            'stream_name': 'btc-price',
            'stream_provider_pubkey': 'aabbcc',
            'pay_per_obs_sats': 300,
            'paid_predictors': 3,
            'competing_predictors': 5,
            'scoring_metric': 'mae',
        })
        resp = post_json(client, '/api/bounty/close', {
            'stream_name': 'btc-price',
            'stream_provider_pubkey': 'aabbcc',
        })
        assert resp.status_code == 200
        assert len(client.startup._close_calls) == 1

    def test_sets_inactive_in_db(self, client):
        post_json(client, '/api/bounty', {
            'stream_name': 'btc-price',
            'stream_provider_pubkey': 'aabbcc',
            'pay_per_obs_sats': 300,
            'paid_predictors': 3,
            'competing_predictors': 5,
            'scoring_metric': 'mae',
        })
        post_json(client, '/api/bounty/close', {
            'stream_name': 'btc-price',
            'stream_provider_pubkey': 'aabbcc',
        })
        resp = client.get('/api/bounties?active=1')
        data = resp.get_json()
        assert data['bounties'] == []

    def test_missing_fields_returns_400(self, client):
        resp = post_json(client, '/api/bounty/close', {})
        assert resp.status_code == 400


# ── GET /api/bounties/mine ─────────────────────────────────────

class TestMyBounties:

    def test_empty(self, client):
        resp = client.get('/api/bounties/mine')
        assert resp.get_json()['bounties'] == []

    def test_only_returns_mine(self, client):
        post_json(client, '/api/bounty', {
            'stream_name': 'btc-price',
            'stream_provider_pubkey': 'aabbcc',
            'pay_per_obs_sats': 300,
            'paid_predictors': 3,
            'competing_predictors': 5,
            'scoring_metric': 'mae',
        })
        resp = client.get('/api/bounties/mine')
        data = resp.get_json()
        assert len(data['bounties']) == 1
        assert data['bounties'][0]['host_pubkey'] == 'host_nostr_pubkey'


# ── GET /api/bounties/discover ────────────────────────────────

class TestDiscoverBounties:

    def test_returns_discover_result(self, client):
        client.startup._discover_result = [
            {'stream_name': 'btc-price', 'pay_per_obs_sats': 300}
        ]
        resp = client.get('/api/bounties/discover')
        data = resp.get_json()
        assert len(data['bounties']) == 1
        assert data['bounties'][0]['stream_name'] == 'btc-price'
