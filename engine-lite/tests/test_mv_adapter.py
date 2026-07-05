"""Unit tests for the Jordan-1 ``MultivariateAdapter``
(``adapters/multivariate/multivariate.py``).

Covers condition() gates + TTL cache, first fit, the retrain random-swap
accept/reject paths (ledger, cooldown, baseline restore), predict arithmetic
and the None fallbacks, the 2-step autoregression, save/load round trip +
schema/corruption refusal, deepcopy safety, zero-eligible-peers -> -1, and
determinism.

The adapter reaches the StreamStore through the module-level ``_getStore``
accessor (design §7 of ``docs/engine/MULTIVARIATE.md``): every test patches it
at a temp-dir StreamStore, so nothing touches a real engine db. Unlike the
sibling feature/head/peer_search tests this imports the real package (the
``__init__`` StarterAdapter bug is fixed as part of this task).

Runs under pytest or standalone (``python test_mv_adapter.py``) since the image
ships no pytest.
"""

import copy
import os
import tempfile

import joblib
import numpy as np
import pandas as pd

from satoriengine.stream_store import StreamStore
from satoriengine.veda.adapters.multivariate import multivariate as mv
from satoriengine.veda.adapters.multivariate import heads as mv_heads


# --------------------------------------------------------------------------- #
# data helpers
# --------------------------------------------------------------------------- #

_START = 1_700_000_000.0
_STEP = 3600.0


def _append(store, uuid, values):
    df = pd.DataFrame({
        'epoch': [_START + _STEP * i for i in range(len(values))],
        'value': [float(v) for v in values],
        'id': [f'{uuid}_{i}' for i in range(len(values))],
    })
    store.append(uuid, df)


def _signal_data(n=80, seed=0):
    """target whose next-step LEVEL diff equals 5 * good's pct-change, plus
    a few uncorrelated noise streams. So p0_delta_0 of `good` is a near-perfect
    predictor of y, while noise streams carry no signal."""
    rng = np.random.default_rng(seed)
    good = 100.0 * np.cumprod(1 + rng.normal(0, 0.02, n))
    gd = np.zeros(n)
    gd[1:] = (good[1:] - good[:-1]) / good[:-1]
    target = np.zeros(n)
    target[0] = 50.0
    for t in range(n - 1):
        target[t + 1] = target[t] + 5.0 * gd[t]
    noise1 = 100.0 + np.cumsum(rng.normal(size=n))
    noise2 = 200.0 + np.cumsum(rng.normal(size=n))
    noise3 = 300.0 + np.cumsum(rng.normal(size=n))
    badnoise = 400.0 + np.cumsum(rng.normal(size=n))
    return {
        'target': target, 'good': good,
        'noise1': noise1, 'noise2': noise2, 'noise3': noise3,
        'badnoise': badnoise,
    }


def _patch(store):
    """Point the adapter's store accessor at `store` and clear the TTL cache."""
    mv._getStore = lambda: store
    mv._resetCountCache()


def _new_adapter(uid='target'):
    return mv.MultivariateAdapter(uid=uid)


# --------------------------------------------------------------------------- #
# condition()
# --------------------------------------------------------------------------- #

def test_condition_too_few_rows_returns_zero():
    with tempfile.TemporaryDirectory() as tmp:
        store = StreamStore(os.path.join(tmp, 'e.db'))
        _append(store, 'a', range(40))
        _append(store, 'b', range(40))
        _patch(store)
        assert mv.MultivariateAdapter.condition(data=[0] * 59) == 0.0
        store.close()


def test_condition_too_few_qualifying_streams_returns_zero():
    with tempfile.TemporaryDirectory() as tmp:
        store = StreamStore(os.path.join(tmp, 'e.db'))
        _append(store, 'a', range(40))       # only 1 stream with >= 30 rows
        _append(store, 'b', range(5))
        _patch(store)
        assert mv.MultivariateAdapter.condition(data=[0] * 60) == 0.0
        store.close()


def test_condition_both_pass_returns_one():
    with tempfile.TemporaryDirectory() as tmp:
        store = StreamStore(os.path.join(tmp, 'e.db'))
        _append(store, 'a', range(40))
        _append(store, 'b', range(40))
        _patch(store)
        assert mv.MultivariateAdapter.condition(data=[0] * 60) == 1.0
        store.close()


def test_condition_store_exception_returns_zero():
    def _boom():
        raise RuntimeError('store down')
    mv._getStore = _boom
    mv._resetCountCache()
    assert mv.MultivariateAdapter.condition(data=[0] * 60) == 0.0


def test_condition_ttl_cache_calls_count_once():
    class _CountingStore:
        def __init__(self):
            self.calls = 0
        def count_streams_with_min_rows(self, m):
            self.calls += 1
            return 5
    cs = _CountingStore()
    mv._getStore = lambda: cs
    mv._resetCountCache()
    assert mv.MultivariateAdapter.condition(data=[0] * 60) == 1.0
    assert mv.MultivariateAdapter.condition(data=[0] * 60) == 1.0
    assert cs.calls == 1, f'TTL cache should call count once, got {cs.calls}'


# --------------------------------------------------------------------------- #
# first fit
# --------------------------------------------------------------------------- #

def _fit_good_only(tmp, n=80):
    """Fresh adapter, store with target + good only -> peer set is exactly
    ['good'] (only eligible candidate). Returns (store, adapter)."""
    store = StreamStore(os.path.join(tmp, 'e.db'))
    d = _signal_data(n)
    _append(store, 'target', d['target'])
    _append(store, 'good', d['good'])
    _patch(store)
    adapter = _new_adapter()
    result = adapter.fit(store.history('target'))
    return store, adapter, result


def test_first_fit_success_and_ledger_initial():
    with tempfile.TemporaryDirectory() as tmp:
        store = StreamStore(os.path.join(tmp, 'e.db'))
        d = _signal_data(80)
        for k in ('target', 'good', 'noise1', 'noise2', 'noise3'):
            _append(store, k, d[k])
        _patch(store)
        adapter = _new_adapter()
        result = adapter.fit(store.history('target'))
        assert result.status == 1
        assert adapter.head is not None
        assert 0 < len(adapter.peer_uuids) <= adapter.top_k
        assert np.isfinite(adapter.modelError)
        assert len(adapter.swap_ledger) == 1
        entry = adapter.swap_ledger[0]
        assert entry['reason'] == 'initial'
        assert entry['swapped_in'] is None and entry['swapped_out'] is None
        assert entry['kept'] is True
        store.close()


def test_zero_eligible_peers_returns_minus_one():
    with tempfile.TemporaryDirectory() as tmp:
        store = StreamStore(os.path.join(tmp, 'e.db'))
        d = _signal_data(80)
        _append(store, 'target', d['target'])
        _append(store, 'tiny', range(5))   # below peer_min_rows -> not eligible
        _patch(store)
        adapter = _new_adapter()
        result = adapter.fit(store.history('target'))
        assert result.status == -1
        assert adapter.head is None
        store.close()


# --------------------------------------------------------------------------- #
# retrain: swap accepted
# --------------------------------------------------------------------------- #

def test_retrain_swap_accepted_swaps_in_good_peer():
    with tempfile.TemporaryDirectory() as tmp:
        store = StreamStore(os.path.join(tmp, 'e.db'))
        d = _signal_data(80)
        for k in ('target', 'good', 'noise1', 'noise2', 'noise3'):
            _append(store, k, d[k])
        _patch(store)
        adapter = _new_adapter()
        # Seed a noise-only peer set so the only eligible candidate is `good`.
        adapter.peer_uuids = ['noise1', 'noise2', 'noise3']
        adapter.peer_added_at = {'noise1': 0, 'noise2': 0, 'noise3': 0}
        adapter.head = mv_heads.XgbHead()   # truthy -> triggers retrain path
        result = adapter.fit(store.history('target'))
        assert result.status == 1
        assert 'good' in adapter.peer_uuids
        last = adapter.swap_ledger[-1]
        assert last['reason'] == 'margin'
        assert last['kept'] is True
        assert last['swapped_in'] == 'good'
        assert last['new_test_mae'] < last['prev_test_mae']
        store.close()


# --------------------------------------------------------------------------- #
# retrain: swap rejected (baseline restored + cooldown)
# --------------------------------------------------------------------------- #

def test_retrain_swap_rejected_restores_baseline_and_cools_down():
    with tempfile.TemporaryDirectory() as tmp:
        store = StreamStore(os.path.join(tmp, 'e.db'))
        d = _signal_data(80)
        for k in ('target', 'good', 'badnoise'):
            _append(store, k, d[k])
        _patch(store)
        hist = store.history('target')

        # Baseline peer set = ['good'] (low MAE). Only candidate = badnoise.
        # weakest peer is `good` itself -> swap loses the signal -> reject.
        adapter = _new_adapter()
        adapter.peer_uuids = ['good']
        adapter.peer_added_at = {'good': 0}
        adapter.head = mv_heads.XgbHead()
        result = adapter.fit(hist)
        assert result.status == 1
        assert adapter.peer_uuids == ['good'], 'baseline peer set must be restored'
        last = adapter.swap_ledger[-1]
        assert last['reason'] == 'no_improvement'
        assert last['kept'] is False
        assert 'badnoise' in adapter.cooldown

        # Baseline restored -> predictions identical to an independent baseline
        # fit of ['good'] (same data, same fixed head seed).
        base = _new_adapter()
        base.peer_uuids = ['good']
        base.peer_added_at = {'good': 0}
        base.head = mv_heads.XgbHead()
        # Empty the pool so no swap is attempted: cool down every other stream.
        base.cooldown = {'badnoise': len(hist)}
        base.fit(hist)
        p_rejected = adapter.predict(hist)
        p_baseline = base.predict(hist)
        assert np.isclose(p_rejected['pred'].iloc[0], p_baseline['pred'].iloc[0])
        store.close()


# --------------------------------------------------------------------------- #
# predict
# --------------------------------------------------------------------------- #

def test_predict_returns_frame_with_pred_and_date_time():
    with tempfile.TemporaryDirectory() as tmp:
        store, adapter, _ = _fit_good_only(tmp)
        hist = store.history('target')
        out = adapter.predict(hist)
        assert isinstance(out, pd.DataFrame)
        assert list(out.columns) == ['date_time', 'pred']
        assert len(out) == 1
        assert np.isfinite(out['pred'].iloc[0])
        # pred = lastObservedValue + head_delta
        lastVal = float(hist['value'].iloc[-1])
        assert abs(out['pred'].iloc[0] - lastVal) < 1e6  # sane magnitude
        # next timestamp is one cadence past the last observation
        expected = pd.to_datetime(hist['date_time'].iloc[-1]) + pd.Timedelta(seconds=_STEP)
        assert pd.Timestamp(out['date_time'].iloc[0]) == expected
        store.close()


def test_predict_none_when_no_model():
    with tempfile.TemporaryDirectory() as tmp:
        store = StreamStore(os.path.join(tmp, 'e.db'))
        d = _signal_data(80)
        _append(store, 'target', d['target'])
        _patch(store)
        adapter = _new_adapter()
        assert adapter.predict(store.history('target')) is None
        store.close()


def test_predict_none_on_feature_column_mismatch():
    with tempfile.TemporaryDirectory() as tmp:
        store, adapter, _ = _fit_good_only(tmp)
        adapter.feature_columns = ['bogus_column']
        assert adapter.predict(store.history('target')) is None
        store.close()


# --------------------------------------------------------------------------- #
# 2-step autoregression
# --------------------------------------------------------------------------- #

def test_two_step_autoregression_holds_peers_at_last_observed():
    with tempfile.TemporaryDirectory() as tmp:
        store, adapter, _ = _fit_good_only(tmp)
        hist = store.history('target')
        pred1 = adapter.predict(hist)
        assert isinstance(pred1, pd.DataFrame)

        # Engine's _createAugmentedData: append a synthetic newer row.
        synth = pd.DataFrame({
            'date_time': [pd.to_datetime(hist['date_time'].iloc[-1]) + pd.Timedelta(seconds=_STEP)],
            'value': [pred1['pred'].iloc[0]],
            'id': ['synthetic'],
        })
        aug = pd.concat([hist, synth], ignore_index=True)
        pred2 = adapter.predict(aug)
        assert isinstance(pred2, pd.DataFrame)
        assert np.isfinite(pred2['pred'].iloc[0])
        # Second forecast is one cadence further out than the first.
        assert pd.Timestamp(pred2['date_time'].iloc[0]) > pd.Timestamp(pred1['date_time'].iloc[0])
        # Peers held at last observed -> their deltas collapse to ~0 for the
        # synthetic row, so the two predictions differ (the peer signal that
        # drove pred1's delta is gone at the augmented step).
        assert not np.isclose(pred1['pred'].iloc[0], pred2['pred'].iloc[0])
        store.close()


# --------------------------------------------------------------------------- #
# save / load
# --------------------------------------------------------------------------- #

def test_save_load_round_trip_predictions_identical():
    with tempfile.TemporaryDirectory() as tmp:
        store, adapter, _ = _fit_good_only(tmp)
        hist = store.history('target')
        path = os.path.join(tmp, 'MultivariateAdapter.joblib')
        assert adapter.save(path) is True

        reloaded = _new_adapter()
        assert reloaded.load(path) is reloaded
        p1 = adapter.predict(hist)
        p2 = reloaded.predict(hist)
        assert np.isclose(p1['pred'].iloc[0], p2['pred'].iloc[0])
        assert reloaded.peer_uuids == adapter.peer_uuids
        assert reloaded.feature_columns == adapter.feature_columns
        store.close()


def test_load_refuses_schema_version_1():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'old.joblib')
        joblib.dump({'schema_version': 1, 'head_name': 'xgboost'}, path)
        adapter = _new_adapter()
        assert adapter.load(path) is None


def test_load_corrupt_file_returns_none():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'corrupt.joblib')
        with open(path, 'wb') as f:
            f.write(b'not a joblib file at all')
        adapter = _new_adapter()
        assert adapter.load(path) is None


# --------------------------------------------------------------------------- #
# deepcopy
# --------------------------------------------------------------------------- #

def test_deepcopy_predicts_identically():
    with tempfile.TemporaryDirectory() as tmp:
        store, adapter, _ = _fit_good_only(tmp)
        hist = store.history('target')
        clone = copy.deepcopy(adapter)
        p1 = adapter.predict(hist)
        p2 = clone.predict(hist)
        assert np.isclose(p1['pred'].iloc[0], p2['pred'].iloc[0])
        store.close()


# --------------------------------------------------------------------------- #
# determinism
# --------------------------------------------------------------------------- #

def test_determinism_same_context_same_fit():
    with tempfile.TemporaryDirectory() as tmp:
        store = StreamStore(os.path.join(tmp, 'e.db'))
        d = _signal_data(80)
        for k in ('target', 'good', 'noise1', 'noise2', 'noise3'):
            _append(store, k, d[k])
        _patch(store)
        hist = store.history('target')
        a1 = _new_adapter()
        a1.fit(hist)
        a2 = _new_adapter()
        a2.fit(hist)
        assert a1.peer_uuids == a2.peer_uuids
        assert np.isclose(a1.modelError, a2.modelError)
        p1 = a1.predict(hist)
        p2 = a2.predict(hist)
        assert np.isclose(p1['pred'].iloc[0], p2['pred'].iloc[0])
        store.close()


if __name__ == '__main__':
    import traceback

    tests = [v for k, v in sorted(globals().items())
             if k.startswith('test_') and callable(v)]
    passed = failed = 0
    for fn in tests:
        try:
            fn()
            passed += 1
            print(f'PASS {fn.__name__}')
        except Exception:
            failed += 1
            print(f'FAIL {fn.__name__}')
            traceback.print_exc()
    print(f'\n{passed} passed, {failed} failed, {len(tests)} total')
    raise SystemExit(1 if failed else 0)
