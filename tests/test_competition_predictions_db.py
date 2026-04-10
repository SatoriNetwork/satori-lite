"""Tests for competition_predictions table in NetworkDB (Phase 2)."""

import importlib.util
import os
import tempfile
import time
import pytest

_spec = importlib.util.spec_from_file_location(
    'network_db',
    os.path.join(os.path.dirname(__file__),
                 '..', 'neuron-lite', 'satorineuron', 'network_db.py'))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
NetworkDB = _mod.NetworkDB


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield NetworkDB(os.path.join(tmpdir, 'test.db'))


@pytest.fixture
def sample():
    return dict(
        stream_name='btc-price-usd',
        stream_provider_pubkey='aabbcc',
        predictor_pubkey='ddeeff',
        host_pubkey='112233',
        seq_num=42,
        predicted_value='67450.25',
        received_at=int(time.time()),
    )


class TestSchema:

    def test_competition_predictions_table_exists(self, db):
        conn = db._get_conn()
        tables = {r['name'] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert 'competition_predictions' in tables


class TestSaveCompetitionPrediction:

    def test_save_and_get(self, db, sample):
        db.save_competition_prediction(**sample)
        rows = db.get_competition_predictions(
            sample['stream_name'],
            sample['stream_provider_pubkey'],
            sample['seq_num'])
        assert len(rows) == 1
        assert rows[0]['predictor_pubkey'] == 'ddeeff'
        assert rows[0]['predicted_value'] == '67450.25'

    def test_multiple_predictors_same_seq(self, db, sample):
        db.save_competition_prediction(**sample)
        second = {**sample, 'predictor_pubkey': 'ffffff'}
        db.save_competition_prediction(**second)
        rows = db.get_competition_predictions(
            sample['stream_name'],
            sample['stream_provider_pubkey'],
            sample['seq_num'])
        assert len(rows) == 2

    def test_get_empty(self, db):
        rows = db.get_competition_predictions('x', 'y', 99)
        assert rows == []

    def test_get_latest_per_predictor(self, db, sample):
        """If a predictor submits twice for same seq, latest wins."""
        db.save_competition_prediction(**sample)
        updated = {**sample, 'predicted_value': '68000.00',
                   'received_at': sample['received_at'] + 1}
        db.save_competition_prediction(**updated)
        rows = db.get_competition_predictions(
            sample['stream_name'],
            sample['stream_provider_pubkey'],
            sample['seq_num'])
        assert len(rows) == 1
        assert rows[0]['predicted_value'] == '68000.00'

    def test_different_seq_nums_separate(self, db, sample):
        db.save_competition_prediction(**sample)
        seq2 = {**sample, 'seq_num': 43}
        db.save_competition_prediction(**seq2)
        rows42 = db.get_competition_predictions(
            sample['stream_name'], sample['stream_provider_pubkey'], 42)
        rows43 = db.get_competition_predictions(
            sample['stream_name'], sample['stream_provider_pubkey'], 43)
        assert len(rows42) == 1
        assert len(rows43) == 1
