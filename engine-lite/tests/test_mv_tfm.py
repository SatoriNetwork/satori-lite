"""Unit tests for the opt-in TimesFM-on-target feature (``tfm_delta``) of the
Jordan-1 multivariate adapter (``docs/engine/Jordan-1_MULTIVARIATE.md`` §2).

Two layers, mirroring the sibling suites:

* the pure ``features.tfmDeltaForRows`` / ``features.tfmDelta`` layer is loaded
  straight from ``features.py`` (importlib), driven by a FAKE forecaster -- no
  torch, no timesfm, no model download;
* the adapter layer imports the real ``multivariate`` module and patches the two
  module-level seams (``_getStore`` for the store, ``_targetForecaster`` for the
  TimesFM callable) so nothing ever loads torch or downloads the ~800 MB model.

Runs under pytest or standalone (``python test_mv_tfm.py``) since the image ships
no pytest.
"""

import copy
import importlib.util
import os
import tempfile

import numpy as np
import pandas as pd


# --------------------------------------------------------------------------- #
# pure layer: load features.py directly (no package __init__, no torch)
# --------------------------------------------------------------------------- #

_HERE = os.path.dirname(os.path.abspath(__file__))
_FEATURES_PATH = os.path.join(_HERE, '..', 'adapters', 'multivariate', 'features.py')
_spec = importlib.util.spec_from_file_location('mv_features_tfm', _FEATURES_PATH)
features = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(features)

tfmDeltaForRows = features.tfmDeltaForRows
tfmDelta = features.tfmDelta


class RecordingForecaster:
    """Fake ``forecaster(inputs, horizon)`` that records each call.

    ``fn(context, horizon_index) -> float`` produces the point forecast for
    horizon step ``horizon_index`` given ``context`` (the series up to and
    including row t). Records ``(contexts, horizon)`` per call so tests can
    assert exactly which rows were (re)forecast.
    """

    def __init__(self, fn):
        self.fn = fn
        self.calls = []

    def __call__(self, inputs, horizon):
        self.calls.append(([list(c) for c in inputs], horizon))
        return [[float(self.fn(list(c), h)) for h in range(horizon)] for c in inputs]


def test_tfmdelta_sub_context_rows_zero_and_one_batched_call():
    vals = list(range(1, 41))  # 40 nonzero values
    fc = RecordingForecaster(lambda c, h: c[-1] * 1.1)
    cache = tfmDeltaForRows(vals, {}, min_context=32, forecaster=fc)
    for t in range(32):
        assert cache[t] == 0.0, f'row {t} < min_context must be 0.0'
    for t in range(32, 40):
        assert abs(cache[t] - 0.1) < 1e-9
    # ONE batched call, contexts exactly rows 32..39 (context = values[0..t]).
    assert len(fc.calls) == 1
    contexts, horizon = fc.calls[0]
    assert horizon == 1
    assert len(contexts) == 8
    assert contexts[0] == vals[:33]    # row 32 -> values[0..32] inclusive
    assert contexts[-1] == vals[:40]   # row 39 -> values[0..39] inclusive


def test_tfmdelta_incremental_only_new_rows_forecast():
    vals = list(range(1, 41))
    fc1 = RecordingForecaster(lambda c, h: c[-1] * 1.1)
    cache = tfmDeltaForRows(vals, {}, min_context=32, forecaster=fc1)
    assert len(fc1.calls) == 1

    vals2 = list(range(1, 46))  # 5 new rows accumulated
    fc2 = RecordingForecaster(lambda c, h: c[-1] * 1.1)
    cache2 = tfmDeltaForRows(vals2, cache, min_context=32, forecaster=fc2)
    assert len(fc2.calls) == 1
    contexts, _ = fc2.calls[0]
    assert len(contexts) == 5, 'only the 5 newly-accumulated rows are forecast'
    assert contexts[0] == vals2[:41]   # row 40
    assert contexts[-1] == vals2[:45]  # row 44
    # Previously-cached rows are byte-for-byte unchanged.
    for t in range(40):
        assert cache2[t] == cache[t]


def test_tfmdelta_determinism_cache_never_recomputed():
    vals = list(range(1, 41))
    fc = RecordingForecaster(lambda c, h: c[-1] * 1.1)
    cache = tfmDeltaForRows(vals, {}, min_context=32, forecaster=fc)
    # A second pass with a forecaster that would give a WILDLY different answer
    # must not recompute anything, because every row is already cached.
    fc2 = RecordingForecaster(lambda c, h: 999999.0)
    cache2 = tfmDeltaForRows(vals, cache, min_context=32, forecaster=fc2)
    assert len(fc2.calls) == 0
    assert cache2 == cache


def test_tfmdelta_non_finite_forecast_becomes_zero():
    vals = list(range(1, 41))
    fc = RecordingForecaster(lambda c, h: float('inf'))
    cache = tfmDeltaForRows(vals, {}, min_context=32, forecaster=fc)
    for t in range(32, 40):
        assert cache[t] == 0.0


def test_tfmdelta_forecaster_failure_becomes_zero():
    def _boom(inputs, horizon):
        raise RuntimeError('OOM')
    vals = list(range(1, 41))
    cache = tfmDeltaForRows(vals, {}, min_context=32, forecaster=_boom)
    for t in range(40):
        assert cache[t] == 0.0


def test_tfmdelta_none_forecaster_all_zero():
    vals = list(range(1, 41))
    cache = tfmDeltaForRows(vals, {}, min_context=32, forecaster=None)
    assert all(cache[t] == 0.0 for t in range(40))


def test_tfmdelta_math_and_epsilon_guards():
    # Exact delta math on a constant series.
    vals = [10.0] * 40
    fc = RecordingForecaster(lambda c, h: 11.0)
    cache = tfmDeltaForRows(vals, {}, min_context=32, forecaster=fc)
    for t in range(32, 40):
        assert abs(cache[t] - 0.1) < 1e-9

    # Zero level -> epsilon guard -> 0.0.
    zeros = [0.0] * 40
    fcz = RecordingForecaster(lambda c, h: 5.0)
    cache0 = tfmDeltaForRows(zeros, {}, min_context=32, forecaster=fcz)
    for t in range(32, 40):
        assert cache0[t] == 0.0

    # Scalar helper direct.
    assert tfmDelta(5.0, 0.0) == 0.0
    assert tfmDelta(float('nan'), 10.0) == 0.0
    assert tfmDelta(float('inf'), 10.0) == 0.0
    assert abs(tfmDelta(11.0, 10.0) - 0.1) < 1e-9
    assert abs(tfmDelta([11.0, 12.0], 10.0) - 0.1) < 1e-9  # sequence -> element 0


# --------------------------------------------------------------------------- #
# adapter layer: real module, patched store + forecaster seams
# --------------------------------------------------------------------------- #

from satoriengine.stream_store import StreamStore                       # noqa: E402
from satoriengine.veda.adapters.multivariate import multivariate as mv  # noqa: E402
from satoriengine.veda.adapters.multivariate import heads as mv_heads   # noqa: E402

_START = 1_700_000_000.0
_STEP = 3600.0
_ORIG_LOAD_CONFIG = mv._loadMultivariateConfig


def _append(store, uuid, values):
    df = pd.DataFrame({
        'epoch': [_START + _STEP * i for i in range(len(values))],
        'value': [float(v) for v in values],
        'id': [f'{uuid}_{i}' for i in range(len(values))],
    })
    store.append(uuid, df)


def _signal_data(n=80, seed=0):
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
    return {'target': target, 'good': good,
            'noise1': noise1, 'noise2': noise2, 'noise3': noise3}


def _patch_store(store):
    mv._getStore = lambda: store
    mv._resetCountCache()
    mv._resetTfmPredictCache()


def _patch_config(**overrides):
    cfg = dict(mv._DEFAULTS)
    cfg.update(overrides)
    mv._loadMultivariateConfig = lambda: dict(cfg)


def _reset_config():
    mv._loadMultivariateConfig = _ORIG_LOAD_CONFIG


def _install_forecaster(fn):
    """Patch mv._targetForecaster with a fake; return a counter of inner calls.

    ``fn(context, horizon_index) -> float``. The factory (mv._targetForecaster)
    is called to obtain the callable; only actual forecast invocations bump the
    counter, so tests can assert the shared-cache "one call" property.
    """
    counter = {'n': 0}

    def factory():
        def forecast(inputs, horizon):
            counter['n'] += 1
            return [[float(fn(list(c), h)) for h in range(horizon)] for c in inputs]
        return forecast

    mv._targetForecaster = factory
    return counter


def _new_tfm_adapter(uid='target', min_context=32):
    a = mv.MultivariateAdapter(uid=uid)
    a.use_tfm_on_target = True
    a.tfm_min_context = min_context
    return a


# ---- fit with the flag on ------------------------------------------------- #

def test_fit_flag_on_tfm_delta_in_columns_and_gains_and_persists():
    with tempfile.TemporaryDirectory() as tmp:
        store = StreamStore(os.path.join(tmp, 'e.db'))
        d = _signal_data(80)
        for k in ('target', 'good', 'noise1', 'noise2', 'noise3'):
            _append(store, k, d[k])
        _patch_store(store)
        _patch_config(use_tfm_on_target=True)
        counter = _install_forecaster(lambda c, h: c[-1] * 1.1)

        adapter = _new_tfm_adapter(min_context=32)
        result = adapter.fit(store.history('target'))
        assert result.status == 1
        assert adapter.feature_columns[-1] == 'tfm_delta'
        assert 'tfm_delta' in adapter.head.featureGains()
        # First fit forecasts once (batched); cache filled for rows >= 32.
        assert counter['n'] == 1
        assert len(adapter.tfm_delta_cache) >= 1
        assert any(v != 0.0 for v in adapter.tfm_delta_cache.values())

        # Cache + columns survive a save/load round trip (config flag still on).
        path = os.path.join(tmp, 'MultivariateAdapter.joblib')
        assert adapter.save(path) is True
        reloaded = mv.MultivariateAdapter(uid='target')
        assert reloaded.load(path) is reloaded
        assert reloaded.feature_columns == adapter.feature_columns
        assert reloaded.tfm_delta_cache == adapter.tfm_delta_cache
        assert reloaded.use_tfm_on_target is True
        store.close()
        _reset_config()


def test_fit_flag_on_timesfm_failing_trains_with_zero_tfm_delta():
    with tempfile.TemporaryDirectory() as tmp:
        store = StreamStore(os.path.join(tmp, 'e.db'))
        d = _signal_data(80)
        for k in ('target', 'good', 'noise1', 'noise2', 'noise3'):
            _append(store, k, d[k])
        _patch_store(store)
        _patch_config(use_tfm_on_target=True)
        mv._targetForecaster = lambda: None  # TimesFM unavailable

        adapter = _new_tfm_adapter(min_context=32)
        result = adapter.fit(store.history('target'))
        assert result.status == 1, 'fit must succeed even with TimesFM disabled'
        assert adapter.feature_columns[-1] == 'tfm_delta'
        assert all(v == 0.0 for v in adapter.tfm_delta_cache.values())
        store.close()
        _reset_config()


def test_swap_holds_tfm_delta_constant_across_baseline_and_candidate():
    with tempfile.TemporaryDirectory() as tmp:
        store = StreamStore(os.path.join(tmp, 'e.db'))
        d = _signal_data(80)
        for k in ('target', 'good', 'noise1', 'noise2', 'noise3'):
            _append(store, k, d[k])
        _patch_store(store)
        _patch_config(use_tfm_on_target=True)
        counter = _install_forecaster(lambda c, h: c[-1] * 1.1)

        adapter = _new_tfm_adapter(min_context=32)
        # Seed a retrain: noise-only peer set, truthy head -> triggers the swap.
        adapter.peer_uuids = ['noise1', 'noise2', 'noise3']
        adapter.peer_added_at = {'noise1': 0, 'noise2': 0, 'noise3': 0}
        adapter.head = mv_heads.XgbHead()
        result = adapter.fit(store.history('target'))
        assert result.status == 1
        # Baseline forecasts once; the candidate training reuses the cache, so
        # the forecaster is NOT called a second time -> tfm_delta is identical
        # across both trainings (attributable swap, Jordan-1 §3).
        assert counter['n'] == 1
        store.close()
        _reset_config()


# ---- predict with the flag on --------------------------------------------- #

def _fit_good_tfm(tmp, forecaster_fn):
    store = StreamStore(os.path.join(tmp, 'e.db'))
    d = _signal_data(80)
    _append(store, 'target', d['target'])
    _append(store, 'good', d['good'])
    _patch_store(store)
    _patch_config(use_tfm_on_target=True)
    counter = _install_forecaster(forecaster_fn)
    adapter = _new_tfm_adapter(min_context=32)
    adapter.fit(store.history('target'))
    return store, adapter, counter


def test_predict_first_vs_augmented_use_h1_vs_h2_and_share_one_call():
    with tempfile.TemporaryDirectory() as tmp:
        # Horizon-independent absolute forecasts: h0 -> 111, h1 -> 122.
        store, adapter, _ = _fit_good_tfm(tmp, lambda c, h: {0: 111.0, 1: 122.0}[h])
        hist = store.history('target')
        L = float(hist['value'].iloc[-1])

        mv._resetTfmPredictCache()
        counter = _install_forecaster(lambda c, h: {0: 111.0, 1: 122.0}[h])

        # First (real) step -> horizon-1 forecast (111) over the real last level.
        d1 = adapter._predictTfmDelta(hist, store)
        assert abs(d1 - (111.0 - L) / L) < 1e-9

        # Augmented step: one synthetic newer row; level = its value; the SAME
        # cached forecast is reused -> horizon-2 forecast (122).
        synthVal = 77.0
        synth = pd.DataFrame({
            'date_time': [pd.to_datetime(hist['date_time'].iloc[-1]) + pd.Timedelta(seconds=_STEP)],
            'value': [synthVal],
            'id': ['synthetic'],
        })
        aug = pd.concat([hist, synth], ignore_index=True)
        d2 = adapter._predictTfmDelta(aug, store)
        assert abs(d2 - (122.0 - synthVal) / synthVal) < 1e-9
        assert d2 != d1
        # Shared cache: exactly ONE forecaster call across both predict steps.
        assert counter['n'] == 1
        store.close()
        _reset_config()


def test_predict_full_frame_has_tfm_delta_column_and_is_finite():
    with tempfile.TemporaryDirectory() as tmp:
        store, adapter, _ = _fit_good_tfm(tmp, lambda c, h: c[-1] * 1.05)
        hist = store.history('target')
        mv._resetTfmPredictCache()
        _install_forecaster(lambda c, h: c[-1] * 1.05)
        out = adapter.predict(hist)
        assert isinstance(out, pd.DataFrame)
        assert list(out.columns) == ['date_time', 'pred']
        assert np.isfinite(out['pred'].iloc[0])
        assert adapter.feature_columns[-1] == 'tfm_delta'
        store.close()
        _reset_config()


def test_predict_tfm_failure_falls_back_to_zero_delta():
    with tempfile.TemporaryDirectory() as tmp:
        store, adapter, _ = _fit_good_tfm(tmp, lambda c, h: c[-1] * 1.05)
        hist = store.history('target')
        mv._resetTfmPredictCache()
        mv._targetForecaster = lambda: None  # TimesFM gone at predict time
        d = adapter._predictTfmDelta(hist, store)
        assert d == 0.0
        # Prediction still proceeds.
        out = adapter.predict(hist)
        assert isinstance(out, pd.DataFrame) and np.isfinite(out['pred'].iloc[0])
        store.close()
        _reset_config()


# ---- condition() RAM gate ------------------------------------------------- #

def test_condition_ram_gate_only_when_flag_on():
    with tempfile.TemporaryDirectory() as tmp:
        store = StreamStore(os.path.join(tmp, 'e.db'))
        _append(store, 'a', range(40))
        _append(store, 'b', range(40))
        _patch_store(store)

        # Flag ON: RAM below 2 GB gates out; ample RAM passes.
        _patch_config(use_tfm_on_target=True)
        mv._resetCountCache()
        assert mv.MultivariateAdapter.condition(data=[0] * 60, availableRamGigs=1.5) == 0.0
        mv._resetCountCache()
        assert mv.MultivariateAdapter.condition(data=[0] * 60, availableRamGigs=4.0) == 1.0

        # Flag OFF (default): no RAM gate, low RAM still passes.
        _patch_config(use_tfm_on_target=False)
        mv._resetCountCache()
        assert mv.MultivariateAdapter.condition(data=[0] * 60, availableRamGigs=1.5) == 1.0
        store.close()
        _reset_config()


# ---- load() cache invalidation -------------------------------------------- #

def _save_flag_on_model(tmp, min_context):
    store = StreamStore(os.path.join(tmp, 'e.db'))
    d = _signal_data(80)
    for k in ('target', 'good', 'noise1'):
        _append(store, k, d[k])
    _patch_store(store)
    _patch_config(use_tfm_on_target=True, tfm_min_context=min_context)
    _install_forecaster(lambda c, h: c[-1] * 1.1)
    adapter = _new_tfm_adapter(min_context=min_context)
    adapter.fit(store.history('target'))
    path = os.path.join(tmp, 'm.joblib')
    assert adapter.save(path) is True
    return store, path, adapter


def test_load_refuses_on_flag_flip():
    with tempfile.TemporaryDirectory() as tmp:
        store, path, saved = _save_flag_on_model(tmp, min_context=32)
        # Config now says flag OFF -> regime changed -> clean-retrain refusal.
        _patch_config(use_tfm_on_target=False)
        reloaded = mv.MultivariateAdapter(uid='target')
        assert reloaded.load(path) is None
        store.close()
        _reset_config()


def test_load_clears_cache_on_min_context_change():
    with tempfile.TemporaryDirectory() as tmp:
        store, path, saved = _save_flag_on_model(tmp, min_context=32)
        assert len(saved.tfm_delta_cache) > 0
        # Same flag, different min_context -> keep the head, drop the cache and
        # adopt the new min_context (predict recomputes live; next fit rebuilds).
        _patch_config(use_tfm_on_target=True, tfm_min_context=16)
        reloaded = mv.MultivariateAdapter(uid='target')
        assert reloaded.load(path) is reloaded
        assert reloaded.tfm_delta_cache == {}
        assert reloaded.tfm_min_context == 16
        store.close()
        _reset_config()


def test_load_keeps_cache_when_flag_and_min_context_unchanged():
    with tempfile.TemporaryDirectory() as tmp:
        store, path, saved = _save_flag_on_model(tmp, min_context=32)
        _patch_config(use_tfm_on_target=True, tfm_min_context=32)
        reloaded = mv.MultivariateAdapter(uid='target')
        assert reloaded.load(path) is reloaded
        assert reloaded.tfm_delta_cache == saved.tfm_delta_cache
        assert reloaded.tfm_min_context == 32
        store.close()
        _reset_config()


# ---- flag OFF (default) parity -------------------------------------------- #

def test_flag_off_default_no_tfm_delta_and_forecaster_never_called():
    with tempfile.TemporaryDirectory() as tmp:
        store = StreamStore(os.path.join(tmp, 'e.db'))
        d = _signal_data(80)
        for k in ('target', 'good', 'noise1', 'noise2', 'noise3'):
            _append(store, k, d[k])
        _patch_store(store)
        _reset_config()  # true defaults: use_tfm_on_target False
        counter = _install_forecaster(lambda c, h: c[-1] * 1.1)

        adapter = mv.MultivariateAdapter(uid='target')  # flag off by default
        assert adapter.use_tfm_on_target is False
        result = adapter.fit(store.history('target'))
        assert result.status == 1
        assert 'tfm_delta' not in adapter.feature_columns
        assert adapter.tfm_delta_cache == {}
        assert counter['n'] == 0, 'flag off must never touch the forecaster'
        out = adapter.predict(store.history('target'))
        assert isinstance(out, pd.DataFrame)
        assert counter['n'] == 0
        store.close()


def test_flag_off_deepcopy_safe_with_module_seams():
    with tempfile.TemporaryDirectory() as tmp:
        store = StreamStore(os.path.join(tmp, 'e.db'))
        d = _signal_data(80)
        _append(store, 'target', d['target'])
        _append(store, 'good', d['good'])
        _patch_store(store)
        _patch_config(use_tfm_on_target=True)
        _install_forecaster(lambda c, h: c[-1] * 1.1)
        adapter = _new_tfm_adapter()
        adapter.fit(store.history('target'))
        # No torch model / forecaster closure leaked onto the instance.
        clone = copy.deepcopy(adapter)
        assert clone.feature_columns == adapter.feature_columns
        store.close()
        _reset_config()


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
