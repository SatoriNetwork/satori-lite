"""ETS warm-start / cached-fit bench.

Compares the new cached ETSAdapter (warm-start via `start_params` + cached
HoltWintersResults reused via `.append(refit=False)`) against a frozen copy of
the original "refit from scratch on every predict" implementation.

For each real stream in /Satori/Engine/db/engine.db we run two scenarios:

  1. PRODUCEPREDICTION SIMULATION (the engine's hot path):
     fit(data) -> predict(data) -> predict(data + 1 synthetic row)
     This is what `Engine.producePrediction` does per stream per poll, with
     the second call being autoregression on augmented data. We measure
     wall-clock time and capture both forecast values for old vs new.

  2. WALK-FORWARD BACKTEST (accuracy regression):
     hold out last 20% of each stream; for each step fit on the prefix and
     predict one step. Report MAE/MAPE per implementation.

Run:
  ./playground-ets

Skips the bench cleanly if engine.db isn't mounted (e.g. running outside the
dev container without engine.db present).
"""

from __future__ import annotations

import copy
import os
import sys
import time
import warnings
from typing import Union

import numpy as np
import pandas as pd

# Pull the new cached adapter from the engine.
from satoriengine.veda.adapters.ets.ets_model import ETSAdapter as ETSCached
from satoriengine.stream_store import StreamStore
from satoriengine.veda.adapters._rng import make_rng
from satoriengine.veda.adapters.interface import ModelAdapter, TrainingResult

# Optional adapters - XGB and Starter for the cross-adapter accuracy comparison.
try:
    from satoriengine.veda.adapters import XgbAdapter, StarterAdapter
except Exception as _e:
    XgbAdapter = None
    StarterAdapter = None

try:
    from satoriengine.veda.adapters.xgboost.xgb_improved import XgbImprovedAdapter
except Exception as _e:
    XgbImprovedAdapter = None


REAL_DB = '/Satori/Engine/db/engine.db'


# ---------------------------------------------------------------------------
# Frozen original implementation — verbatim copy of the pre-cache adapter so
# we can run both side by side without git-stash gymnastics.
# ---------------------------------------------------------------------------
class ETSOriginal(ModelAdapter):

    @staticmethod
    def condition(*args, **kwargs) -> float:
        return 1.0

    def __init__(self, uid=None, modelPath=None, **kwargs):
        super().__init__()
        self.uid = uid
        self.modelPath = modelPath
        self._lastSeries = None
        self.modelError = None
        self._fitParams = self._drawFitParams()

    @staticmethod
    def _drawFitParams() -> dict:
        rng = make_rng()
        trend = rng.choice(['add', None])
        damped = bool(rng.integers(0, 2)) if trend == 'add' else False
        init = rng.choice(['estimated', 'heuristic'])
        return {'trend': trend, 'damped_trend': damped, 'initialization_method': init}

    def fit(self, data: pd.DataFrame, **kwargs) -> TrainingResult:
        self._fitParams = self._drawFitParams()
        series = self._extractSeries(data)
        self._lastSeries = series
        self.modelError = self._rollingMae(series, params=self._fitParams)
        return TrainingResult(1, self)

    def score(self, series=None, **kwargs):
        if series is not None:
            return self._rollingMae(series, params=self._fitParams)
        return self.modelError if self.modelError is not None else float('inf')

    def compare(self, other=None, **kwargs):
        return True

    def predict(self, data: pd.DataFrame, **kwargs):
        series = self._extractSeries(data)
        if series is None or len(series) == 0:
            return None
        pred = self._forecastOne(series, params=self._fitParams)
        return self._wrapPrediction(data, pred)

    @staticmethod
    def _forecastOne(series, params=None):
        if len(series) < 5 or np.nanstd(series) < 1e-12:
            return float(series[-1])
        params = params or {'trend': 'add', 'damped_trend': False, 'initialization_method': 'estimated'}
        try:
            from statsmodels.tsa.holtwinters import ExponentialSmoothing
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                m = ExponentialSmoothing(
                    series,
                    trend=params['trend'],
                    damped_trend=params['damped_trend'],
                    seasonal=None,
                    initialization_method=params['initialization_method'],
                ).fit(optimized=True, use_brute=False,
                      minimize_kwargs={'options': {'maxiter': 50}})
                pred = float(m.forecast(1)[0])
            return pred if np.isfinite(pred) else float(series[-1])
        except Exception:
            return float(series[-1])

    @classmethod
    def _rollingMae(cls, series, horizon=3, params=None):
        if series is None or len(series) < 10:
            return float('inf')
        n = len(series)
        start = max(5, n - horizon)
        errs = []
        for i in range(start, n):
            pred = cls._forecastOne(series[:i], params=params)
            errs.append(abs(pred - float(series[i])))
        return float(np.mean(errs)) if errs else float('inf')

    @staticmethod
    def _extractSeries(data):
        if data is None or len(data) == 0:
            return None
        if 'value' in data.columns:
            s = pd.to_numeric(data['value'], errors='coerce')
        elif data.shape[1] >= 2:
            s = pd.to_numeric(data.iloc[:, 1], errors='coerce')
        else:
            return None
        return s.dropna().to_numpy(dtype=np.float64)

    @staticmethod
    def _wrapPrediction(data, pred):
        try:
            if 'date_time' in data.columns:
                times = pd.to_datetime(data['date_time'])
                last = times.iloc[-1]
                diff = times.diff().median() if len(times) >= 2 else pd.Timedelta(hours=1)
                next_ts = last + diff
            else:
                next_ts = pd.Timestamp.now() + pd.Timedelta(hours=1)
        except Exception:
            next_ts = pd.Timestamp.now() + pd.Timedelta(hours=1)
        return pd.DataFrame({'date_time': [next_ts], 'pred': [pred]})


# ---------------------------------------------------------------------------
# Determinism helper: both adapters call `_drawFitParams` which uses make_rng
# (non-deterministic). For an apples-to-apples comparison we monkey-patch
# both classes to draw from a seeded RNG, in lock-step.
# ---------------------------------------------------------------------------
def _make_locked_param_drawer(seed: int):
    rng = np.random.default_rng(seed)
    def _draw():
        trend = rng.choice(['add', None])
        damped = bool(rng.integers(0, 2)) if trend == 'add' else False
        init = rng.choice(['estimated', 'heuristic'])
        return {'trend': trend, 'damped_trend': damped, 'initialization_method': init}
    return _draw


def _lock_param_draws(seed: int):
    """Patch both adapters so both fits draw the *same* params in the same
    order. Without this, the two runs use different hyperparams and the
    'identical predictions' assertion is meaningless."""
    drawer = _make_locked_param_drawer(seed)
    ETSOriginal._drawFitParams = staticmethod(drawer)
    # Make a separate generator for the new adapter but with the same seed
    # so the param sequences match.
    drawer2 = _make_locked_param_drawer(seed)
    ETSCached._drawFitParams = staticmethod(drawer2)


# ---------------------------------------------------------------------------
# Scenario 1: produce-prediction simulation (fit + augmented predict).
# ---------------------------------------------------------------------------
def simulate_produce_prediction(history: pd.DataFrame, cls):
    """Mirror `Engine.producePrediction` for one stream: fit, predict, then
    re-predict on data + 1 synthetic row at the forecast time. Returns
    (first_value, second_value, elapsed_seconds)."""
    t0 = time.perf_counter()
    adapter = cls()
    adapter.fit(history)
    first = adapter.predict(history)
    if first is None or 'pred' not in first.columns:
        return None, None, time.perf_counter() - t0
    first_val = float(first['pred'].iloc[0])
    # Build augmented frame exactly like _createAugmentedData does.
    aug_ts = first['date_time'].iloc[0]
    aug_row = pd.DataFrame({
        'date_time': [aug_ts],
        'value': [first_val],
        'id': ['synthetic']
    })
    augmented = pd.concat([history, aug_row], ignore_index=True)
    second = adapter.predict(augmented)
    second_val = (
        float(second['pred'].iloc[0])
        if second is not None and 'pred' in second.columns
        else None
    )
    return first_val, second_val, time.perf_counter() - t0


def scenario_produce_prediction(rows):
    print('\n' + '=' * 80)
    print('SCENARIO 1: producePrediction simulation (fit + autoregressive predict x2)')
    print('=' * 80)
    print(f'{"stream":>10} {"n":>5}  '
          f'{"old.1st":>12} {"new.1st":>12} {"Δ1":>9}  '
          f'{"old.2nd":>12} {"new.2nd":>12} {"Δ2":>9}  '
          f'{"old.t":>7} {"new.t":>7} {"speed":>7}')

    totals = {'old_t': 0.0, 'new_t': 0.0, 'streams': 0,
              'delta_first': [], 'delta_second': []}
    for stream_uuid, history in rows:
        _lock_param_draws(seed=hash(stream_uuid) & 0xFFFFFFFF)
        old_1, old_2, old_t = simulate_produce_prediction(history, ETSOriginal)
        _lock_param_draws(seed=hash(stream_uuid) & 0xFFFFFFFF)
        new_1, new_2, new_t = simulate_produce_prediction(history, ETSCached)

        d1 = (new_1 - old_1) if (old_1 is not None and new_1 is not None) else float('nan')
        d2 = (new_2 - old_2) if (old_2 is not None and new_2 is not None) else float('nan')
        speed = (old_t / new_t) if new_t > 0 else float('nan')

        print(f'{stream_uuid[:10]:>10} {len(history):>5}  '
              f'{_fmt(old_1):>12} {_fmt(new_1):>12} {_fmt(d1):>9}  '
              f'{_fmt(old_2):>12} {_fmt(new_2):>12} {_fmt(d2):>9}  '
              f'{old_t:>7.3f} {new_t:>7.3f} {speed:>6.2f}x')

        totals['old_t'] += old_t
        totals['new_t'] += new_t
        totals['streams'] += 1
        if np.isfinite(d1): totals['delta_first'].append(abs(d1))
        if np.isfinite(d2): totals['delta_second'].append(abs(d2))

    print('-' * 80)
    print(f'  streams={totals["streams"]}  '
          f'total old={totals["old_t"]:.2f}s  total new={totals["new_t"]:.2f}s  '
          f'speedup={totals["old_t"]/max(totals["new_t"],1e-9):.2f}x')
    if totals['delta_first']:
        print(f'  |Δ first|  mean={np.mean(totals["delta_first"]):.6g}  '
              f'max={np.max(totals["delta_first"]):.6g}')
    if totals['delta_second']:
        print(f'  |Δ second| mean={np.mean(totals["delta_second"]):.6g}  '
              f'max={np.max(totals["delta_second"]):.6g}')


# ---------------------------------------------------------------------------
# Scenario 2: walk-forward backtest for accuracy regression.
# ---------------------------------------------------------------------------
def scenario_walk_forward(rows, holdout_frac=0.20):
    print('\n' + '=' * 80)
    print(f'SCENARIO 2: walk-forward backtest  (holdout {int(holdout_frac*100)}% per stream)')
    print('=' * 80)

    abs_err = {'old': [], 'new': []}
    pct_err = {'old': [], 'new': []}
    elapsed = {'old': 0.0, 'new': 0.0}

    for stream_uuid, history in rows:
        if len(history) < 20:
            continue
        holdout = max(1, int(len(history) * holdout_frac))
        for impl_name, cls in (('old', ETSOriginal), ('new', ETSCached)):
            _lock_param_draws(seed=hash(stream_uuid) & 0xFFFFFFFF)
            t0 = time.perf_counter()
            for step in range(holdout):
                idx = len(history) - holdout + step
                prefix = history.iloc[:idx].reset_index(drop=True)
                actual = float(history.iloc[idx]['value'])
                if not np.isfinite(actual):
                    continue
                adapter = cls()
                adapter.fit(prefix)
                result = adapter.predict(prefix)
                if result is None or 'pred' not in result.columns:
                    continue
                pred = float(result['pred'].iloc[0])
                if not np.isfinite(pred):
                    continue
                err = abs(pred - actual)
                scale = max(abs(actual), 1e-9)
                abs_err[impl_name].append(err)
                pct_err[impl_name].append(err / scale)
            elapsed[impl_name] += time.perf_counter() - t0

    def _stats(name):
        if not abs_err[name]:
            return f'  {name:>4}: no predictions'
        return (f'  {name:>4}: MAE={np.mean(abs_err[name]):.6g}  '
                f'MAPE={np.mean(pct_err[name]):.4%}  '
                f'samples={len(abs_err[name])}  '
                f'wall={elapsed[name]:.2f}s')

    print(_stats('old'))
    print(_stats('new'))
    if abs_err['old'] and abs_err['new']:
        old_mae = np.mean(abs_err['old'])
        new_mae = np.mean(abs_err['new'])
        delta = (new_mae - old_mae) / old_mae if old_mae > 0 else 0.0
        print(f'  MAE drift: {delta:+.4%} (positive = new is worse)')
        speedup = elapsed['old'] / max(elapsed['new'], 1e-9)
        print(f'  speedup:   {speedup:.2f}x')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _fmt(x):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return 'NA'
    return f'{x:.6g}'


def load_streams(max_streams=15, min_history=30):
    import sqlite3
    if not os.path.isfile(REAL_DB):
        print(f'  real engine.db not found at {REAL_DB}')
        return []
    con = sqlite3.connect(f'file:{REAL_DB}?mode=ro', uri=True)
    rows = con.execute(
        'SELECT stream_uuid, COUNT(*) AS n FROM observations '
        'GROUP BY stream_uuid HAVING n >= ? ORDER BY n DESC LIMIT ?',
        (min_history, max_streams),
    ).fetchall()
    con.close()
    store = StreamStore(REAL_DB)
    out = []
    for uuid, _ in rows:
        df = store.history(uuid)
        if len(df) >= min_history:
            out.append((uuid, df))
    return out


def scenario_size_sweep(sizes=(10, 100, 1000), repeats=10):
    """Synthetic series of varying length — the real engine.db only has
    ~80-90-row streams, so this is the only way to see how the new code
    scales with longer history.

    For each size we generate `repeats` deterministic series and run BOTH
    scenarios on each:
      - producePrediction (fit + 2 autoregressive predicts), single adapter instance
      - 5-step walk-forward, fresh adapter per step (mimics scenario 2)
    Reports per-size totals + speedup and prediction drift.
    """
    print('\n' + '=' * 80)
    print(f'SCENARIO 3: synthetic size sweep  sizes={sizes}  repeats per size={repeats}')
    print('=' * 80)
    print(f'{"size":>6} {"phase":>14}  '
          f'{"old.t":>9} {"new.t":>9} {"speedup":>9}  '
          f'{"|Δ| mean":>12} {"|Δ| max":>12}')

    rng = np.random.default_rng(123)

    def synth(n: int, seed: int) -> pd.DataFrame:
        r = np.random.default_rng(seed)
        # Drift + noise + mild seasonality — enough variance that ETS engages.
        t = np.arange(n)
        values = (
            100.0
            + 0.05 * t
            + 3.0 * np.sin(t / 7.0)
            + r.normal(0.0, 0.5, n)
        )
        start = pd.Timestamp('2026-01-01', tz='UTC')
        dts = [start + pd.Timedelta(hours=int(i)) for i in t]
        return pd.DataFrame({
            'date_time': dts,
            'value': values,
            'id': [f's{seed}_{i}' for i in t],
        })

    for n in sizes:
        # ---- 3a: producePrediction simulation (one adapter, fit + 2 predicts)
        old_t = new_t = 0.0
        d1, d2 = [], []
        for k in range(repeats):
            seed = int(rng.integers(0, 10_000_000))
            history = synth(n, seed)
            _lock_param_draws(seed=seed)
            o1, o2, ot = simulate_produce_prediction(history, ETSOriginal)
            _lock_param_draws(seed=seed)
            n1, n2, nt = simulate_produce_prediction(history, ETSCached)
            old_t += ot
            new_t += nt
            if o1 is not None and n1 is not None and np.isfinite(o1) and np.isfinite(n1):
                d1.append(abs(n1 - o1))
            if o2 is not None and n2 is not None and np.isfinite(o2) and np.isfinite(n2):
                d2.append(abs(n2 - o2))
        speedup = old_t / new_t if new_t > 0 else float('nan')
        d1_mean = np.mean(d1) if d1 else float('nan')
        d1_max = np.max(d1) if d1 else float('nan')
        d2_mean = np.mean(d2) if d2 else float('nan')
        d2_max = np.max(d2) if d2 else float('nan')
        print(f'{n:>6} {"produce(1st)":>14}  '
              f'{old_t:>9.3f} {new_t:>9.3f} {speedup:>8.2f}x  '
              f'{d1_mean:>12.3e} {d1_max:>12.3e}')
        print(f'{"":>6} {"produce(2nd)":>14}  '
              f'{"":>9} {"":>9} {"":>9}  '
              f'{d2_mean:>12.3e} {d2_max:>12.3e}')

        # ---- 3b: walk-forward, fresh adapter per step (scenario 2 shape)
        holdout = max(1, min(5, n // 4))  # cap at 5 steps so 1000-row runs finish
        old_wf = new_wf = 0.0
        err_old, err_new = [], []
        for k in range(repeats):
            seed = int(rng.integers(0, 10_000_000))
            history = synth(n + holdout, seed)
            for impl_name, cls, bucket, accum in (
                ('old', ETSOriginal, err_old, 'old'),
                ('new', ETSCached, err_new, 'new'),
            ):
                _lock_param_draws(seed=seed)
                t0 = time.perf_counter()
                for step in range(holdout):
                    idx = n + step
                    prefix = history.iloc[:idx].reset_index(drop=True)
                    actual = float(history.iloc[idx]['value'])
                    adapter = cls()
                    adapter.fit(prefix)
                    result = adapter.predict(prefix)
                    if result is None or 'pred' not in result.columns:
                        continue
                    pred = float(result['pred'].iloc[0])
                    if np.isfinite(pred):
                        bucket.append(abs(pred - actual))
                elapsed = time.perf_counter() - t0
                if accum == 'old':
                    old_wf += elapsed
                else:
                    new_wf += elapsed
        wf_speed = old_wf / new_wf if new_wf > 0 else float('nan')
        mae_o = np.mean(err_old) if err_old else float('nan')
        mae_n = np.mean(err_new) if err_new else float('nan')
        drift = (
            (mae_n - mae_o) / mae_o
            if (err_old and err_new and mae_o > 0) else float('nan')
        )
        print(f'{"":>6} {"walk-fwd":>14}  '
              f'{old_wf:>9.3f} {new_wf:>9.3f} {wf_speed:>8.2f}x  '
              f'{"MAE old=":>12}{mae_o:.4g} {"new=":>4}{mae_n:.4g} drift={drift:+.2%}')


def scenario_cross_adapter(sizes=(10, 100, 1000), repeats=10, holdout=5):
    """Apples-to-apples accuracy + timing across Starter, ETS (new cached),
    and XGB on synthetic series of each size. Each (adapter, size, repeat)
    runs a walk-forward of `holdout` steps with a fresh adapter per step.

    Same synthetic generator as scenario 3 - drift + noise + mild seasonality.
    """
    if XgbAdapter is None or StarterAdapter is None:
        print('XGB/Starter adapter unavailable - skipping cross-adapter scenario.')
        return

    print('\n' + '=' * 80)
    print(f'SCENARIO 4: cross-adapter accuracy  sizes={sizes}  repeats={repeats}  '
          f'walk-fwd steps={holdout}')
    print('=' * 80)
    print(f'{"size":>6} {"adapter":>10}  {"wall":>9}  {"MAE":>12}  {"MAPE":>10}  '
          f'{"samples":>8}  {"failures":>9}')

    rng_master = np.random.default_rng(7)

    def synth(n: int, seed: int) -> pd.DataFrame:
        r = np.random.default_rng(seed)
        t = np.arange(n)
        values = (
            100.0
            + 0.05 * t
            + 3.0 * np.sin(t / 7.0)
            + r.normal(0.0, 0.5, n)
        )
        start = pd.Timestamp('2026-01-01', tz='UTC')
        dts = [start + pd.Timedelta(hours=int(i)) for i in t]
        return pd.DataFrame({
            'date_time': dts,
            'value': values,
            'id': [f'x{seed}_{i}' for i in t],
        })

    # Pre-generate the SAME series per (size, repeat) so all three adapters
    # see identical inputs at each step.
    series_by_size: dict[int, list[pd.DataFrame]] = {}
    for n in sizes:
        series_by_size[n] = [
            synth(n + holdout, int(rng_master.integers(0, 10_000_000)))
            for _ in range(repeats)
        ]

    adapters = [
        ('Starter', StarterAdapter),
        ('ETS',     ETSCached),
        ('XGB',     XgbAdapter),
    ]

    for n in sizes:
        for name, cls in adapters:
            errs = []
            pct = []
            failures = 0
            t0 = time.perf_counter()
            for history in series_by_size[n]:
                # Lock the ETS param draw so its random structural choices
                # don't pollute the across-size comparison.
                _lock_param_draws(seed=int(history.iloc[0]['value'] * 1e6) % (2**32))
                for step in range(holdout):
                    idx = n + step
                    prefix = history.iloc[:idx].reset_index(drop=True)
                    actual = float(history.iloc[idx]['value'])
                    if not np.isfinite(actual):
                        continue
                    try:
                        adapter = cls()
                        if name in ('XGB', 'ETS'):
                            adapter.fit(prefix)
                        result = adapter.predict(prefix)
                        if result is None or 'pred' not in result.columns:
                            failures += 1
                            continue
                        pred = float(result['pred'].iloc[0])
                        if not np.isfinite(pred):
                            failures += 1
                            continue
                        errs.append(abs(pred - actual))
                        pct.append(abs(pred - actual) / max(abs(actual), 1e-9))
                    except Exception:
                        failures += 1
                        continue
            elapsed = time.perf_counter() - t0
            mae = np.mean(errs) if errs else float('nan')
            mape = np.mean(pct) if pct else float('nan')
            print(f'{n:>6} {name:>10}  {elapsed:>8.2f}s  '
                  f'{mae:>12.4g}  {mape:>9.2%}  '
                  f'{len(errs):>8d}  {failures:>9d}')
        print()  # blank line between sizes


def scenario_xgb_diagnostic(n=200, repeats=8, holdout=10):
    """Diagnose XGB underperformance hypotheses by manipulating the data.

    Three data regimes, identical noise, identical seasonality:
      A. trended (default)  : 100 + 0.05*t + 3*sin(t/7) + N(0,0.5)
      B. detrended          :        100 + 3*sin(t/7) + N(0,0.5)   (no drift)
      C. detrended + delta  : same data, but predict y - y_prev not y     (XGB-friendly target)

    Hypothesis: XGB fails on (A) because trees can't extrapolate beyond
    [min(y_train), max(y_train)]. Removing the trend (B) should bring XGB
    in line with ETS. Predicting deltas (C) on the trended series should
    also fix it, because the target becomes stationary even though the
    levels drift.
    """
    if XgbAdapter is None:
        print('XGB unavailable.')
        return

    print('\n' + '=' * 80)
    print(f'SCENARIO 5: XGB diagnostic  n={n}  repeats={repeats}  holdout={holdout}')
    print('=' * 80)
    print(f'{"regime":>22} {"adapter":>10}  {"MAE":>10}  {"MAPE":>10}  '
          f'{"extrapolation":>14}')

    rng = np.random.default_rng(1337)

    def synth(n_total: int, seed: int, trend: bool) -> pd.DataFrame:
        r = np.random.default_rng(seed)
        t = np.arange(n_total)
        slope = 0.05 if trend else 0.0
        values = (
            100.0
            + slope * t
            + 3.0 * np.sin(t / 7.0)
            + r.normal(0.0, 0.5, n_total)
        )
        start = pd.Timestamp('2026-01-01', tz='UTC')
        dts = [start + pd.Timedelta(hours=int(i)) for i in t]
        return pd.DataFrame({
            'date_time': dts,
            'value': values,
            'id': [f'd{seed}_{i}' for i in t],
        })

    def run(regime_label, df_builder, predict_delta=False):
        """Returns (MAE_xgb, MAPE_xgb, MAE_ets, MAPE_ets, extrap_fraction).

        extrap_fraction = fraction of holdout points where the actual exceeds
        max(train_labels) - tree models can't predict above this."""
        err_xgb, err_ets, pct_xgb, pct_ets = [], [], [], []
        extrap_hits = 0
        extrap_total = 0
        for k in range(repeats):
            seed = int(rng.integers(0, 10_000_000))
            history = df_builder(n + holdout, seed)
            _lock_param_draws(seed=seed)
            for step in range(holdout):
                idx = n + step
                prefix = history.iloc[:idx].reset_index(drop=True)
                actual = float(history.iloc[idx]['value'])
                if not np.isfinite(actual):
                    continue

                # Track extrapolation: would the train-label range cover actual?
                max_train_label = float(prefix['value'].iloc[1:].max())
                min_train_label = float(prefix['value'].iloc[1:].min())
                extrap_total += 1
                if actual > max_train_label or actual < min_train_label:
                    extrap_hits += 1

                # XGB
                try:
                    if predict_delta:
                        # Train target: y[t+1] - y[t]. Then add the last seen
                        # value to the prediction. This makes the target
                        # stationary so tree extrapolation isn't a blocker.
                        prefix_delta = prefix.copy()
                        prefix_delta['value'] = prefix_delta['value'].diff().fillna(0.0)
                        a = XgbAdapter()
                        a.fit(prefix_delta)
                        r_pred = a.predict(prefix_delta)
                        if r_pred is not None and 'pred' in r_pred.columns:
                            delta_pred = float(r_pred['pred'].iloc[0])
                            xgb_pred = float(prefix['value'].iloc[-1]) + delta_pred
                            err_xgb.append(abs(xgb_pred - actual))
                            pct_xgb.append(abs(xgb_pred - actual) / max(abs(actual), 1e-9))
                    else:
                        a = XgbAdapter()
                        a.fit(prefix)
                        r_pred = a.predict(prefix)
                        if r_pred is not None and 'pred' in r_pred.columns:
                            xgb_pred = float(r_pred['pred'].iloc[0])
                            err_xgb.append(abs(xgb_pred - actual))
                            pct_xgb.append(abs(xgb_pred - actual) / max(abs(actual), 1e-9))
                except Exception:
                    pass

                # ETS (always on raw data, no delta transform)
                try:
                    a = ETSCached()
                    a.fit(prefix)
                    r_pred = a.predict(prefix)
                    if r_pred is not None and 'pred' in r_pred.columns:
                        ets_pred = float(r_pred['pred'].iloc[0])
                        err_ets.append(abs(ets_pred - actual))
                        pct_ets.append(abs(ets_pred - actual) / max(abs(actual), 1e-9))
                except Exception:
                    pass

        mae_x = np.mean(err_xgb) if err_xgb else float('nan')
        mae_e = np.mean(err_ets) if err_ets else float('nan')
        mp_x = np.mean(pct_xgb) if pct_xgb else float('nan')
        mp_e = np.mean(pct_ets) if pct_ets else float('nan')
        ex_frac = extrap_hits / extrap_total if extrap_total else 0.0
        print(f'{regime_label:>22} {"XGB":>10}  {mae_x:>10.4g}  {mp_x:>9.2%}  {ex_frac:>13.0%}')
        print(f'{regime_label:>22} {"ETS":>10}  {mae_e:>10.4g}  {mp_e:>9.2%}  {ex_frac:>13.0%}')

    run('A: trended (raw)', lambda n_, s_: synth(n_, s_, trend=True), predict_delta=False)
    run('B: detrended (raw)', lambda n_, s_: synth(n_, s_, trend=False), predict_delta=False)
    run('C: trended (Δ-target)', lambda n_, s_: synth(n_, s_, trend=True), predict_delta=True)


def scenario_xgb_delta_real_streams(max_streams=20, holdout_frac=0.20, min_history=30):
    """Validate +delta on REAL streams from engine.db. The synthetic generator
    has smooth drift + mild sin + low noise; real Satori streams are chaotic.

    Walk-forward 20% holdout per stream. For each step we run BOTH baseline
    XGB and XGB+delta on the same prefix and report per-stream MAE + winners.
    """
    if XgbImprovedAdapter is None:
        print('XgbImprovedAdapter unavailable.')
        return
    rows = load_streams(max_streams=max_streams, min_history=min_history)
    if not rows:
        print('engine.db unavailable - skipping real-stream XGB delta validation.')
        return

    print('\n' + '=' * 80)
    print(f'SCENARIO 7: XGB +delta on REAL engine.db streams  '
          f'({len(rows)} streams, holdout {int(holdout_frac*100)}%)')
    print('=' * 80)
    print(f'{"stream":>10} {"n":>4}  '
          f'{"base MAE":>12} {"+delta MAE":>12}  {"delta vs base":>14}  {"winner":>8}')

    base_total, delta_total = [], []
    base_pct, delta_pct = [], []
    wins_base, wins_delta = 0, 0

    for stream_uuid, history in rows:
        if len(history) < min_history:
            continue
        holdout = max(1, int(len(history) * holdout_frac))
        base_errs, delta_errs = [], []
        base_pcts, delta_pcts = [], []
        for step in range(holdout):
            idx = len(history) - holdout + step
            prefix = history.iloc[:idx].reset_index(drop=True)
            actual = float(history.iloc[idx]['value'])
            if not np.isfinite(actual):
                continue
            scale = max(abs(actual), 1e-9)
            for flags, errs, pcts in (
                (dict(),                       base_errs, base_pcts),
                (dict(use_delta_target=True),  delta_errs, delta_pcts),
            ):
                try:
                    a = XgbImprovedAdapter(**flags)
                    a.fit(prefix)
                    r = a.predict(prefix)
                    if r is None or 'pred' not in r.columns:
                        continue
                    pred = float(r['pred'].iloc[0])
                    if not np.isfinite(pred):
                        continue
                    errs.append(abs(pred - actual))
                    pcts.append(abs(pred - actual) / scale)
                except Exception:
                    pass
        if not base_errs or not delta_errs:
            continue
        b_mae = np.mean(base_errs)
        d_mae = np.mean(delta_errs)
        diff_pct = (d_mae - b_mae) / b_mae if b_mae > 0 else float('nan')
        winner = '+delta' if d_mae < b_mae else 'base'
        if d_mae < b_mae:
            wins_delta += 1
        else:
            wins_base += 1
        base_total.extend(base_errs)
        delta_total.extend(delta_errs)
        base_pct.extend(base_pcts)
        delta_pct.extend(delta_pcts)
        print(f'{stream_uuid[:10]:>10} {len(history):>4}  '
              f'{b_mae:>12.4g} {d_mae:>12.4g}  '
              f'{diff_pct:>+13.2%}   {winner:>8}')

    print('-' * 80)
    if base_total and delta_total:
        bm = np.mean(base_total)
        dm = np.mean(delta_total)
        bp = np.mean(base_pct)
        dp = np.mean(delta_pct)
        improvement = (dm - bm) / bm if bm > 0 else float('nan')
        print(f'  pooled  base MAE={bm:.4g} (MAPE {bp:.2%})  '
              f'+delta MAE={dm:.4g} (MAPE {dp:.2%})')
        print(f'  pooled  +delta vs base = {improvement:+.2%}')
        print(f'  streams: +delta wins={wins_delta}  base wins={wins_base}')


def scenario_xgb_toggle_sweep(sizes=(50, 200, 1000), repeats=5, holdout=5):
    """Measure each XGB improvement individually + all-on, so we can see what
    each change contributes.

    Configurations:
      baseline    no toggles (matches the original XgbAdapter)
      +delta      use_delta_target only
      +lags       adaptive_lags only
      +t          t_feature only
      +tight      tight_hyperparams only
      ALL         all four toggles on

    Identical synthetic streams used across all configs for each (size, repeat).
    Reports MAE / MAPE / wall-clock per (size, config).
    """
    if XgbImprovedAdapter is None:
        print('XgbImprovedAdapter unavailable.')
        return

    print('\n' + '=' * 80)
    print(f'SCENARIO 6: XGB toggle sweep  sizes={sizes}  repeats={repeats}  '
          f'walk-fwd steps={holdout}')
    print('=' * 80)
    print(f'{"size":>6} {"config":>10}  {"wall":>9}  {"MAE":>10}  {"MAPE":>10}  '
          f'{"vs base":>10}')

    rng_master = np.random.default_rng(11)

    def synth(n_total: int, seed: int) -> pd.DataFrame:
        r = np.random.default_rng(seed)
        t = np.arange(n_total)
        values = (
            100.0
            + 0.05 * t
            + 3.0 * np.sin(t / 7.0)
            + r.normal(0.0, 0.5, n_total)
        )
        start = pd.Timestamp('2026-01-01', tz='UTC')
        dts = [start + pd.Timedelta(hours=int(i)) for i in t]
        return pd.DataFrame({
            'date_time': dts,
            'value': values,
            'id': [f'g{seed}_{i}' for i in t],
        })

    configs = [
        ('baseline', dict()),
        ('+delta',   dict(use_delta_target=True)),
        ('+lags',    dict(adaptive_lags=True)),
        ('+t',       dict(t_feature=True)),
        ('+tight',   dict(tight_hyperparams=True)),
        ('ALL',      dict(use_delta_target=True, adaptive_lags=True,
                          t_feature=True, tight_hyperparams=True)),
    ]

    series_by_size: dict[int, list[pd.DataFrame]] = {}
    for n in sizes:
        series_by_size[n] = [
            synth(n + holdout, int(rng_master.integers(0, 10_000_000)))
            for _ in range(repeats)
        ]

    for n in sizes:
        baseline_mae = None
        for label, flags in configs:
            errs, pct = [], []
            failures = 0
            t0 = time.perf_counter()
            for history in series_by_size[n]:
                for step in range(holdout):
                    idx = n + step
                    prefix = history.iloc[:idx].reset_index(drop=True)
                    actual = float(history.iloc[idx]['value'])
                    if not np.isfinite(actual):
                        continue
                    try:
                        adapter = XgbImprovedAdapter(**flags)
                        adapter.fit(prefix)
                        result = adapter.predict(prefix)
                        if result is None or 'pred' not in result.columns:
                            failures += 1
                            continue
                        pred = float(result['pred'].iloc[0])
                        if not np.isfinite(pred):
                            failures += 1
                            continue
                        errs.append(abs(pred - actual))
                        pct.append(abs(pred - actual) / max(abs(actual), 1e-9))
                    except Exception:
                        failures += 1
                        continue
            elapsed = time.perf_counter() - t0
            mae = np.mean(errs) if errs else float('nan')
            mape = np.mean(pct) if pct else float('nan')
            if label == 'baseline':
                baseline_mae = mae
                vs = ''
            elif baseline_mae and baseline_mae > 0 and np.isfinite(mae):
                vs = f'{(mae - baseline_mae) / baseline_mae:+.2%}'
            else:
                vs = ''
            print(f'{n:>6} {label:>10}  {elapsed:>8.2f}s  '
                  f'{mae:>10.4g}  {mape:>9.2%}  {vs:>10}')
        print()


def main():
    print('ETS warm-start / cached-fit bench')
    print('=' * 80)
    rows = load_streams(max_streams=15, min_history=30)
    if rows:
        print(f'Loaded {len(rows)} streams from {REAL_DB}')
        scenario_produce_prediction(rows)
        scenario_walk_forward(rows)
    else:
        print('engine.db unavailable - skipping scenarios 1 & 2 (need real streams).')
    scenario_size_sweep(sizes=(10, 100, 1000), repeats=10)
    scenario_cross_adapter(sizes=(10, 100, 1000), repeats=10, holdout=5)
    scenario_xgb_diagnostic(n=200, repeats=8, holdout=10)
    scenario_xgb_toggle_sweep(sizes=(50, 200, 1000), repeats=5, holdout=5)
    scenario_xgb_delta_real_streams(max_streams=20, holdout_frac=0.20, min_history=30)
    print('\ndone')


if __name__ == '__main__':
    main()
