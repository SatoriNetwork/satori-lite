"""Tests for Phase 4: accountability tooling — payment tracking, leaderboard, stats."""

import importlib.util
import os
import tempfile
import time
import pytest

_NEURON_LITE = os.path.join(os.path.dirname(__file__), '..', 'neuron-lite')

_db_spec = importlib.util.spec_from_file_location(
    'network_db',
    os.path.join(_NEURON_LITE, 'satorineuron', 'network_db.py'))
_db_mod = importlib.util.module_from_spec(_db_spec)
_db_spec.loader.exec_module(_db_mod)
NetworkDB = _db_mod.NetworkDB


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield NetworkDB(os.path.join(tmpdir, 'test.db'))


def _add_competition(db, host_pubkey='host01', pay_per_obs_sats=1000, active=True):
    db.add_competition(
        stream_name='btc-price-usd',
        stream_provider_pubkey='provider01',
        host_pubkey=host_pubkey,
        pay_per_obs_sats=pay_per_obs_sats,
        paid_predictors=3,
        competing_predictors=10,
        scoring_metric='mae',
        scoring_params='{}',
        horizon=1,
        active=active,
        timestamp=int(time.time()),
    )


def _record(db, predictor, sats, seq_num=1):
    db.record_competition_payment(
        stream_name='btc-price-usd',
        stream_provider_pubkey='provider01',
        predictor_pubkey=predictor,
        seq_num=seq_num,
        sats_paid=sats,
        paid_at=int(time.time()),
    )


# ── Schema ───────────────────────────────────────────────────────────────────

class TestSchema:

    def test_competition_payments_table_exists(self, db):
        conn = db._get_conn()
        tables = {r['name'] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert 'competition_payments' in tables


# ── record_competition_payment ────────────────────────────────────────────────

class TestRecordPayment:

    def test_record_and_retrieve(self, db):
        _record(db, 'alice', 600, seq_num=1)
        rows = db.get_competition_payments(
            'btc-price-usd', 'provider01')
        assert len(rows) == 1
        assert rows[0]['predictor_pubkey'] == 'alice'
        assert rows[0]['sats_paid'] == 600

    def test_multiple_payments_same_seq(self, db):
        _record(db, 'alice', 600, seq_num=1)
        _record(db, 'bob', 400, seq_num=1)
        rows = db.get_competition_payments('btc-price-usd', 'provider01')
        assert len(rows) == 2

    def test_multiple_seq_nums(self, db):
        _record(db, 'alice', 600, seq_num=1)
        _record(db, 'alice', 650, seq_num=2)
        rows = db.get_competition_payments('btc-price-usd', 'provider01')
        assert len(rows) == 2

    def test_empty_returns_empty_list(self, db):
        rows = db.get_competition_payments('btc-price-usd', 'provider01')
        assert rows == []


# ── get_competition_leaderboard ───────────────────────────────────────────────

class TestLeaderboard:

    def test_leaderboard_totals_per_predictor(self, db):
        _record(db, 'alice', 600, seq_num=1)
        _record(db, 'alice', 650, seq_num=2)
        _record(db, 'bob', 400, seq_num=1)
        board = db.get_competition_leaderboard('btc-price-usd', 'provider01')
        by_key = {r['predictor_pubkey']: r for r in board}
        assert by_key['alice']['total_sats'] == 1250
        assert by_key['alice']['prediction_count'] == 2
        assert by_key['bob']['total_sats'] == 400

    def test_leaderboard_sorted_descending(self, db):
        _record(db, 'alice', 100)
        _record(db, 'bob', 900)
        board = db.get_competition_leaderboard('btc-price-usd', 'provider01')
        assert board[0]['predictor_pubkey'] == 'bob'

    def test_leaderboard_empty(self, db):
        board = db.get_competition_leaderboard('btc-price-usd', 'provider01')
        assert board == []


# ── get_host_payment_stats ────────────────────────────────────────────────────

class TestHostPaymentStats:

    def test_stats_returns_correct_totals(self, db):
        _add_competition(db, pay_per_obs_sats=1000)
        _record(db, 'alice', 600, seq_num=1)
        _record(db, 'bob', 400, seq_num=1)
        _record(db, 'alice', 700, seq_num=2)
        _record(db, 'bob', 300, seq_num=2)
        stats = db.get_host_payment_stats(
            'btc-price-usd', 'provider01', 'host01')
        assert stats['total_paid_sats'] == 2000
        assert stats['scored_observations'] == 2
        assert stats['avg_paid_per_obs'] == 1000.0
        assert stats['announced_per_obs'] == 1000

    def test_stats_no_payments(self, db):
        _add_competition(db, pay_per_obs_sats=1000)
        stats = db.get_host_payment_stats(
            'btc-price-usd', 'provider01', 'host01')
        assert stats['total_paid_sats'] == 0
        assert stats['scored_observations'] == 0

    def test_stats_no_competition(self, db):
        stats = db.get_host_payment_stats(
            'btc-price-usd', 'provider01', 'nobody')
        assert stats is None


# ── Routes ────────────────────────────────────────────────────────────────────

import json
from flask import Flask, jsonify, request as flask_request


def _create_test_app(db):
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.config['SECRET_KEY'] = 'test'

    @app.route('/api/competition/leaderboard')
    def leaderboard():
        stream_name = flask_request.args.get('stream_name')
        provider_pubkey = flask_request.args.get('provider_pubkey')
        if not stream_name or not provider_pubkey:
            return jsonify({'error': 'stream_name and provider_pubkey required'}), 400
        return jsonify(db.get_competition_leaderboard(stream_name, provider_pubkey))

    @app.route('/api/competition/stats')
    def stats():
        stream_name = flask_request.args.get('stream_name')
        provider_pubkey = flask_request.args.get('provider_pubkey')
        host_pubkey = flask_request.args.get('host_pubkey')
        if not stream_name or not provider_pubkey or not host_pubkey:
            return jsonify({'error': 'missing params'}), 400
        result = db.get_host_payment_stats(stream_name, provider_pubkey, host_pubkey)
        if result is None:
            return jsonify({'error': 'not found'}), 404
        return jsonify(result)

    return app


@pytest.fixture
def client(db):
    _add_competition(db, pay_per_obs_sats=1000)
    _record(db, 'alice', 600, seq_num=1)
    _record(db, 'bob', 400, seq_num=1)
    return _create_test_app(db).test_client()


class TestLeaderboardRoute:

    def test_leaderboard_returns_200(self, client):
        resp = client.get(
            '/api/competition/leaderboard'
            '?stream_name=btc-price-usd&provider_pubkey=provider01')
        assert resp.status_code == 200

    def test_leaderboard_contains_predictors(self, client):
        resp = client.get(
            '/api/competition/leaderboard'
            '?stream_name=btc-price-usd&provider_pubkey=provider01')
        data = json.loads(resp.data)
        pubkeys = [r['predictor_pubkey'] for r in data]
        assert 'alice' in pubkeys
        assert 'bob' in pubkeys

    def test_leaderboard_missing_params_returns_400(self, client):
        resp = client.get('/api/competition/leaderboard')
        assert resp.status_code == 400


class TestHostStatsRoute:

    def test_stats_returns_200(self, client):
        resp = client.get(
            '/api/competition/stats'
            '?stream_name=btc-price-usd&provider_pubkey=provider01'
            '&host_pubkey=host01')
        assert resp.status_code == 200

    def test_stats_has_expected_fields(self, client):
        resp = client.get(
            '/api/competition/stats'
            '?stream_name=btc-price-usd&provider_pubkey=provider01'
            '&host_pubkey=host01')
        data = json.loads(resp.data)
        assert 'total_paid_sats' in data
        assert 'announced_per_obs' in data
        assert 'scored_observations' in data
