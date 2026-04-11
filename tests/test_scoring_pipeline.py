"""Tests for Phase 3: scoring module loader and payment trigger."""

import importlib.util
import json
import os
import sys
import tempfile
import time
import pytest

_NEURON_LITE = os.path.join(os.path.dirname(__file__), '..', 'neuron-lite')

# ── Load network_db (avoid importing start.py and its heavy deps) ─────────────

_db_spec = importlib.util.spec_from_file_location(
    'network_db',
    os.path.join(_NEURON_LITE, 'satorineuron', 'network_db.py'))
_db_mod = importlib.util.module_from_spec(_db_spec)
_db_spec.loader.exec_module(_db_mod)
NetworkDB = _db_mod.NetworkDB

# ── Load competition_scoring ──────────────────────────────────────────────────

_cs_spec = importlib.util.spec_from_file_location(
    'competition_scoring',
    os.path.join(_NEURON_LITE, 'satorineuron', 'competition_scoring.py'))
_cs_mod = importlib.util.module_from_spec(_cs_spec)
_cs_spec.loader.exec_module(_cs_mod)
compute_payouts = _cs_mod.compute_payouts

SCORING_DIR = os.path.join(_NEURON_LITE, 'scoring')


# ── Helpers ──────────────────────────────────────────────────────────────────

def _load_scorer(metric: str):
    """Load a scoring module by metric name from the scoring/ directory."""
    path = os.path.join(SCORING_DIR, f'{metric}.py')
    spec = importlib.util.spec_from_file_location(f'scoring_{metric}', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_competition(db, host_pubkey='host01', active=True):
    db.add_competition(
        stream_name='btc-price-usd',
        stream_provider_pubkey='provider01',
        host_pubkey=host_pubkey,
        pay_per_obs_sats=1000,
        paid_predictors=3,
        competing_predictors=10,
        scoring_metric='mae',
        scoring_params='{}',
        horizon=1,
        active=active,
        timestamp=int(time.time()),
    )


def _add_prediction(db, predictor_pubkey, predicted_value, seq_num=1):
    db.save_competition_prediction(
        stream_name='btc-price-usd',
        stream_provider_pubkey='provider01',
        predictor_pubkey=predictor_pubkey,
        host_pubkey='host01',
        seq_num=seq_num,
        predicted_value=str(predicted_value),
        received_at=int(time.time()),
    )


# ── Tests: scoring module interface ──────────────────────────────────────────

class TestMaeScorer:

    def test_mae_module_has_score_function(self):
        mod = _load_scorer('mae')
        assert callable(getattr(mod, 'score', None))

    def test_mae_single_predictor(self):
        mod = _load_scorer('mae')
        payload = {
            'observed_value': 100.0,
            'predictions': [
                {'predictor_pubkey': 'alice', 'predicted_value': 90.0},
            ],
            'pay_per_obs_sats': 1000,
            'paid_predictors': 1,
            'scoring_params': {},
        }
        result = mod.score(payload)
        assert isinstance(result, dict)
        assert 'alice' in result
        assert result['alice'] == 1000  # only predictor gets full pot

    def test_mae_two_predictors_closer_wins_more(self):
        mod = _load_scorer('mae')
        payload = {
            'observed_value': 100.0,
            'predictions': [
                {'predictor_pubkey': 'alice', 'predicted_value': 99.0},  # error 1
                {'predictor_pubkey': 'bob',   'predicted_value': 90.0},  # error 10
            ],
            'pay_per_obs_sats': 1000,
            'paid_predictors': 2,
            'scoring_params': {},
        }
        result = mod.score(payload)
        assert result['alice'] > result['bob']

    def test_mae_paid_predictors_cap(self):
        """Only top paid_predictors receive payment."""
        mod = _load_scorer('mae')
        payload = {
            'observed_value': 100.0,
            'predictions': [
                {'predictor_pubkey': 'a', 'predicted_value': 101.0},
                {'predictor_pubkey': 'b', 'predicted_value': 102.0},
                {'predictor_pubkey': 'c', 'predicted_value': 150.0},
            ],
            'pay_per_obs_sats': 1000,
            'paid_predictors': 2,
            'scoring_params': {},
        }
        result = mod.score(payload)
        paid = [k for k, v in result.items() if v > 0]
        assert len(paid) == 2
        assert 'c' not in result or result.get('c', 0) == 0

    def test_mae_no_predictions_returns_empty(self):
        mod = _load_scorer('mae')
        payload = {
            'observed_value': 100.0,
            'predictions': [],
            'pay_per_obs_sats': 1000,
            'paid_predictors': 3,
            'scoring_params': {},
        }
        result = mod.score(payload)
        assert result == {}

    def test_mae_total_does_not_exceed_budget(self):
        mod = _load_scorer('mae')
        payload = {
            'observed_value': 100.0,
            'predictions': [
                {'predictor_pubkey': 'a', 'predicted_value': 99.0},
                {'predictor_pubkey': 'b', 'predicted_value': 101.0},
                {'predictor_pubkey': 'c', 'predicted_value': 105.0},
            ],
            'pay_per_obs_sats': 1000,
            'paid_predictors': 3,
            'scoring_params': {},
        }
        result = mod.score(payload)
        assert sum(result.values()) <= 1000


# ── Tests: compute_payouts pipeline ─────────────────────────────────────────

def _competition_row(host_pubkey='host01', active=True):
    return {
        'stream_name': 'btc-price-usd',
        'stream_provider_pubkey': 'provider01',
        'host_pubkey': host_pubkey,
        'pay_per_obs_sats': 1000,
        'paid_predictors': 3,
        'competing_predictors': 10,
        'scoring_metric': 'mae',
        'scoring_params': '{}',
        'horizon': 1,
        'active': 1 if active else 0,
    }


def _prediction_row(predictor_pubkey, predicted_value, seq_num=1):
    return {
        'predictor_pubkey': predictor_pubkey,
        'predicted_value': str(predicted_value),
        'seq_num': seq_num,
        'stream_name': 'btc-price-usd',
        'stream_provider_pubkey': 'provider01',
        'host_pubkey': 'host01',
        'received_at': int(time.time()),
    }


class TestComputePayouts:

    def test_triggers_payment_for_active_competition(self):
        comp = _competition_row(active=True)
        preds = [
            _prediction_row('alice', 99.0),
            _prediction_row('bob', 95.0),
        ]
        result = compute_payouts(comp, preds, observed_value=100.0)
        assert len(result) > 0
        assert all(v > 0 for v in result.values())

    def test_skips_inactive_competition(self):
        comp = _competition_row(active=False)
        preds = [_prediction_row('alice', 99.0)]
        result = compute_payouts(comp, preds, observed_value=100.0)
        assert result == {}

    def test_skips_empty_predictions(self):
        comp = _competition_row(active=True)
        result = compute_payouts(comp, [], observed_value=100.0)
        assert result == {}

    def test_unknown_scorer_returns_empty(self):
        comp = {**_competition_row(), 'scoring_metric': 'nonexistent_metric'}
        preds = [_prediction_row('alice', 99.0)]
        result = compute_payouts(comp, preds, observed_value=100.0)
        assert result == {}

    def test_total_within_budget(self):
        comp = _competition_row()
        preds = [
            _prediction_row('a', 99.0),
            _prediction_row('b', 101.0),
            _prediction_row('c', 105.0),
        ]
        result = compute_payouts(comp, preds, observed_value=100.0)
        assert sum(result.values()) <= comp['pay_per_obs_sats']

    def test_better_predictor_gets_more(self):
        comp = _competition_row()
        preds = [
            _prediction_row('close', 100.5),   # error 0.5
            _prediction_row('far', 110.0),      # error 10.0
        ]
        result = compute_payouts(comp, preds, observed_value=100.0)
        assert result.get('close', 0) > result.get('far', 0)
