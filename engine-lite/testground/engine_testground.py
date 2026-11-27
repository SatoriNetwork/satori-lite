"""Engine testing ground — efficient central-path reference pipeline.

Models the WHOLE central prediction flow, each stage done the efficient way:

    ingest -> normalize -> persist -> load history -> engine -> batch output

It runs the adapters directly — no neuron, no relay, no web UI, no server
connection, no training threads. The modules it wires together
(``satoriengine.data_helper``, ``satoriengine.stream_store``) are written
port-ready: the proven design moves into the real neuron/engine/storage later.

Run (one-off container, same dev image, neuron never starts):

    ./playground                       # uses the bundled sample
    ./playground <path-to-batch.json>  # a different captured batch
"""

from __future__ import annotations

import os
import sys
import json

import numpy as np
import pandas as pd

from satoriengine.data_helper import (
    normalize_central_observation,
    batch_to_stream_frames,
)
from satoriengine.stream_store import StreamStore

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE = os.path.join(HERE, 'central_batch_sample.json')
# Dedicated playground database — NEVER the real engine-lite/db/engine.db.
PLAYGROUND_DB = os.path.join(HERE, 'playground.db')

SYNTHETIC_UUID = 'synthetic-0000-0000-0000-000000000000'


def rule(title: str) -> None:
    print('\n' + '=' * 70)
    print(title)
    print('=' * 70)


def fresh_store() -> StreamStore:
    """Recreate the playground db from scratch so every run is deterministic."""
    for suffix in ('', '-wal', '-shm', '-journal'):
        path = PLAYGROUND_DB + suffix
        if os.path.exists(path):
            os.remove(path)
    return StreamStore(PLAYGROUND_DB)


def load_batch(path: str) -> list[dict]:
    with open(path) as f:
        return json.load(f)


def synthetic_batches(stream_uuid: str, n: int = 40) -> list[list[dict]]:
    """A sequence of single-observation batches for one stream.

    Models the real world: each 11h poll delivers one new observation per
    stream. Accumulating them is the only way a stream crosses the >10-row
    threshold and reaches the XGB path (a single real batch is 1 row/stream).
    """
    rng = np.random.default_rng(42)
    start = pd.Timestamp('2026-05-01', tz='UTC')
    batches = []
    for i in range(n):
        epoch = (start + pd.Timedelta(hours=i)).timestamp()
        value = 100 + 0.5 * i + 5 * np.sin(i / 6) + rng.normal(0, 0.5)
        batches.append([{
            'value': str(value),
            'observed_at': str(epoch),
            'hash': '',
            'id': 900000 + i,
            'stream_uuid': stream_uuid,
            'stream': {'uuid': stream_uuid, 'name': 'synthetic_stream'},
        }])
    return batches


def ingest(store: StreamStore, batches: list[list[dict]]) -> set[str]:
    """ingest -> normalize -> persist, for a sequence of raw Central batches."""
    total_obs = sum(len(b) for b in batches)
    inserted = 0
    streams: set[str] = set()
    for batch in batches:
        frames = batch_to_stream_frames(batch)  # normalize + group per stream
        for stream_uuid, frame in frames.items():
            inserted += store.append(stream_uuid, frame)  # persist
            streams.add(stream_uuid)
    print(f'  {len(batches)} batch(es), {total_obs} observation(s) '
          f'-> {inserted} new row(s) across {len(streams)} stream(s)')
    return streams


def pick_adapter(history: pd.DataFrame, adapters: list):
    """Mimic StreamModel.chooseAdapter: first adapter whose condition fires
    (the >10-row rule — XgbAdapter for >10 rows, StarterAdapter otherwise)."""
    for adapter_cls in adapters:
        if adapter_cls.condition(data=history, cpu=0, availableRamGigs=8.0) == 1:
            return adapter_cls
    return adapters[-1]


def run_engine(store: StreamStore, stream_uuid: str, adapters: list) -> dict | None:
    """load history -> adapter pick -> fit/predict, for one stream."""
    history = store.history(stream_uuid)          # single epoch->datetime conv
    chosen = pick_adapter(history, adapters)
    print(f'  stream {stream_uuid[:8]}  history={len(history)} row(s)  '
          f'->  {chosen.__name__}')
    try:
        adapter = chosen()
        if chosen.__name__ == 'XgbAdapter':
            adapter.fit(history)                  # XGB must train before predict
        result = adapter.predict(history)
        if result is not None and 'pred' in result.columns:
            value = float(result['pred'].iloc[0])
            print(f'  predicted next value: {value}')
            return {'stream_uuid': stream_uuid,
                    'adapter': chosen.__name__,
                    'value': value}
        print('  prediction: None')
    except Exception as e:
        import traceback
        print(f'  {chosen.__name__} failed: {e}')
        traceback.print_exc()
    return None


def main() -> None:
    batch_path = sys.argv[1] if len(sys.argv) > 1 else SAMPLE

    rule('Engine testing ground — efficient central-path pipeline')
    print(f'batch file   : {batch_path}')
    print(f'playground db: {PLAYGROUND_DB}  (fresh each run)')

    store = fresh_store()
    batch = load_batch(batch_path)

    rule('1. Ingest — raw observation[0] from Central')
    print(json.dumps(batch[0], indent=2, sort_keys=True))

    rule('2. Normalize — data_helper.normalize_central_observation')
    print(json.dumps(
        {k: str(v) for k, v in normalize_central_observation(batch[0]).items()},
        indent=2, sort_keys=True))

    rule('3. Persist — normalize + group + StreamStore.append (real batch)')
    real_streams = ingest(store, [batch])

    # Adapters imported lazily so steps 1-3 still run if xgboost is missing.
    from satoriengine.veda.adapters import XgbAdapter, StarterAdapter
    adapters = [XgbAdapter, StarterAdapter]

    predictions: list[dict] = []

    rule('4. Engine — real stream (1 observation -> StarterAdapter)')
    p = run_engine(store, sorted(real_streams)[0], adapters)
    if p:
        predictions.append(p)

    rule('5. Persist — 40 synthetic single-obs batches (one stream)')
    print('  (models 40 successive polls accumulating into one stream)')
    ingest(store, synthetic_batches(SYNTHETIC_UUID))
    print(f'  StreamStore.row_count = {store.row_count(SYNTHETIC_UUID)}')

    rule('6. Engine — synthetic stream (accumulated history -> XgbAdapter)')
    p = run_engine(store, SYNTHETIC_UUID, adapters)
    if p:
        predictions.append(p)

    rule('7. Output — prediction batch ready for submission')
    print(f'{len(predictions)} prediction(s) collected (no network call):')
    print(json.dumps(predictions, indent=2))

    rule('8. Variance — fit/predict each adapter N times on identical data')
    variance_check(store)

    rule('9. Backtest — walk-forward accuracy on real engine.db streams')
    backtest_real_streams()

    store.close()
    rule('done')


REAL_DB = '/Satori/Engine/db/engine.db'


def backtest_real_streams(
    max_streams: int = 1000,
    holdout_frac: float = 0.20,
    min_history: int = 20,
) -> None:
    """
    Read real streams from the neuron's engine.db (read-only path), do a
    walk-forward backtest with an 80/20 split: hold out the final 20% of
    each stream's history as the test set, then for each test point fit
    on the prefix and predict one step ahead.

    Reports per-adapter mean absolute error and MAPE for cross-stream
    comparability, plus per-stream winner counts.
    """
    import time
    from satoriengine.stream_store import StreamStore
    from satoriengine.veda.adapters import XgbAdapter, StarterAdapter
    try:
        from satoriengine.veda.adapters.ets.ets_model import ETSAdapter
    except Exception as e:
        print(f'  ETSAdapter unavailable: {e}')
        ETSAdapter = None

    if not os.path.isfile(REAL_DB):
        print(f'  real engine.db not found at {REAL_DB} - skipping backtest')
        return

    store = StreamStore(REAL_DB)
    import sqlite3
    con = sqlite3.connect(f'file:{REAL_DB}?mode=ro', uri=True)
    rows = con.execute(
        'SELECT stream_uuid, COUNT(*) AS n FROM observations '
        'GROUP BY stream_uuid HAVING n >= ? ORDER BY n DESC LIMIT ?',
        (min_history, max_streams),
    ).fetchall()
    con.close()

    adapters: list[tuple[str, type]] = [
        ('Starter', StarterAdapter),
        ('XGB', XgbAdapter),
    ]
    if ETSAdapter is not None:
        adapters.append(('ETS', ETSAdapter))

    # Per-adapter accumulators across all (stream, step) pairs.
    abs_err: dict[str, list[float]] = {name: [] for name, _ in adapters}
    pct_err: dict[str, list[float]] = {name: [] for name, _ in adapters}
    wins: dict[str, int] = {name: 0 for name, _ in adapters}

    print(f'  streams={len(rows)}  holdout={int(holdout_frac*100)}% per stream  '
          f'(min history >= {min_history})')

    t0 = time.time()
    for stream_idx, (stream_uuid, n) in enumerate(rows, 1):
        df = store.history(stream_uuid)
        if len(df) < min_history:
            continue
        holdout = max(1, int(len(df) * holdout_frac))
        per_stream_err: dict[str, list[float]] = {name: [] for name, _ in adapters}
        for step in range(holdout):
            test_idx = len(df) - holdout + step
            prefix = df.iloc[:test_idx].reset_index(drop=True)
            actual = float(df.iloc[test_idx]['value'])
            if not np.isfinite(actual):
                continue
            scale = max(abs(actual), 1e-9)
            for name, cls in adapters:
                try:
                    adapter = cls()
                    if name in ('XGB', 'ETS'):
                        adapter.fit(prefix)
                    result = adapter.predict(prefix)
                    if result is None or 'pred' not in result.columns:
                        continue
                    pred = float(result['pred'].iloc[0])
                    if not np.isfinite(pred):
                        continue
                    err = abs(pred - actual)
                    abs_err[name].append(err)
                    pct_err[name].append(err / scale)
                    per_stream_err[name].append(err)
                except Exception:
                    pass
        # Per-stream winner: lowest mean abs error across the holdout steps.
        means = {n: float(np.mean(e)) for n, e in per_stream_err.items() if e}
        if means:
            winner = min(means, key=means.get)
            wins[winner] += 1
        print(f'  [{stream_idx:3d}/{len(rows)}] {stream_uuid[:8]}  '
              f'n={n:3d} holdout={holdout:2d}  '
              + '  '.join(f'{n_}={(np.mean(e) if e else float("nan")):.4g}'
                          for n_, e in per_stream_err.items()))

    elapsed = time.time() - t0
    print(f'\n  --- aggregate over {len(rows)} streams, 80/20 walk-forward '
          f'(took {elapsed:.1f}s) ---')
    print(f'  {"adapter":<8} {"MAE":>14} {"MAPE":>10} {"wins":>6}')
    for name, _ in adapters:
        mae = float(np.mean(abs_err[name])) if abs_err[name] else float('nan')
        mape = float(np.mean(pct_err[name]) * 100) if pct_err[name] else float('nan')
        print(f'  {name:<8} {mae:>14.6g} {mape:>9.2f}% {wins[name]:>6}')

    store.close()


def variance_check(store: StreamStore, n_runs: int = 8) -> None:
    """
    Each adapter draws principled per-fit hyperparameters from a wall-clock
    RNG (engine-lite/adapters/_rng.py). Running the same adapter N times on
    the exact same history should yield N different (but valid) predictions,
    proving that 1000 nodes on identical data will produce a diverse
    ensemble rather than echoing one another.
    """
    import time
    from satoriengine.veda.adapters import XgbAdapter, StarterAdapter
    try:
        from satoriengine.veda.adapters.ets.ets_model import ETSAdapter
    except Exception as e:
        print(f'  ETSAdapter unavailable: {e}')
        ETSAdapter = None

    history = store.history(SYNTHETIC_UUID)
    print(f'  using synthetic stream ({len(history)} rows), {n_runs} runs per adapter')

    adapters = [('Starter', StarterAdapter), ('XGB', XgbAdapter)]
    if ETSAdapter is not None:
        adapters.append(('ETS', ETSAdapter))

    for label, cls in adapters:
        preds: list[float] = []
        for _ in range(n_runs):
            try:
                adapter = cls()
                if label in ('XGB', 'ETS'):
                    adapter.fit(history)
                result = adapter.predict(history)
                if result is not None and 'pred' in result.columns:
                    preds.append(float(result['pred'].iloc[0]))
            except Exception as e:
                print(f'  {label} run failed: {e}')
            # Force the wall-clock microsecond to advance so successive
            # runs land on distinct seeds (otherwise tight loops can hit
            # the same microsecond // 100 bucket).
            time.sleep(0.001)
        if not preds:
            print(f'  {label}: no predictions produced')
            continue
        arr = np.array(preds)
        spread = float(arr.max() - arr.min())
        unique = len(set(round(p, 9) for p in preds))
        status = 'OK' if unique > 1 else 'IDENTICAL (no variance!)'
        print(f'  {label:<8} unique={unique}/{n_runs}  spread={spread:.6f}  '
              f'mean={arr.mean():.4f}  [{status}]')
        print(f'           samples: {[round(p, 4) for p in preds]}')


if __name__ == '__main__':
    main()
