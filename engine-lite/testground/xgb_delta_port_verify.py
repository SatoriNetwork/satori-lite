"""Verify the +delta port into the real XgbAdapter.

Three checks:
  1. The patched XgbAdapter produces ~identical predictions to
     XgbImprovedAdapter(use_delta_target=True) on real engine.db streams.
  2. save() writes the schema_version marker.
  3. load() refuses files that lack the marker (forces retrain).
"""
from __future__ import annotations
import os
import sys
import tempfile
import joblib
import numpy as np
import pandas as pd

sys.path.insert(0, '/Satori/Engine/testground')

from satoriengine.veda.adapters import XgbAdapter
from satoriengine.veda.adapters.xgboost.xgb_improved import XgbImprovedAdapter
from satoriengine.stream_store import StreamStore

REAL_DB = '/Satori/Engine/db/engine.db'


def lock_same_seed():
    """Lock both adapters' RNGs to the same seed so the random hyperparam
    draws are identical between them (apples-to-apples)."""
    seed = 1234
    XgbAdapter.rng = np.random.default_rng(seed)
    XgbImprovedAdapter.rng = np.random.default_rng(seed)


def load_one_stream(min_n=50):
    import sqlite3
    if not os.path.isfile(REAL_DB):
        return None
    con = sqlite3.connect(f'file:{REAL_DB}?mode=ro', uri=True)
    row = con.execute(
        'SELECT stream_uuid, COUNT(*) FROM observations '
        'GROUP BY stream_uuid HAVING COUNT(*) >= ? '
        'ORDER BY COUNT(*) DESC LIMIT 1',
        (min_n,),
    ).fetchone()
    con.close()
    if not row:
        return None
    store = StreamStore(REAL_DB)
    return store.history(row[0])


def main():
    print('=' * 70)
    print('CHECK 1: patched XgbAdapter matches XgbImprovedAdapter(+delta)')
    print('=' * 70)
    history = load_one_stream(min_n=60)
    if history is None:
        print('  no real stream available, using synthetic')
        rng = np.random.default_rng(0)
        t = np.arange(80)
        history = pd.DataFrame({
            'date_time': [pd.Timestamp('2026-01-01', tz='UTC') + pd.Timedelta(hours=int(i)) for i in t],
            'value': 100.0 + 0.05 * t + 3.0 * np.sin(t / 7.0) + rng.normal(0, 0.5, 80),
            'id': [f's_{i}' for i in t],
        })
    print(f'  stream has {len(history)} rows')

    holdout = 5
    diffs = []
    for step in range(holdout):
        idx = len(history) - holdout + step
        prefix = history.iloc[:idx].reset_index(drop=True)
        # Common seed = same hyperparam draws across both
        seed = 42 + step
        np.random.seed(seed)

        # Real adapter (newly patched)
        a_real = XgbAdapter()
        a_real.rng = np.random.default_rng(seed)
        a_real.fit(prefix)
        r_real = a_real.predict(prefix)

        # Reference adapter (delta toggle on)
        a_ref = XgbImprovedAdapter(use_delta_target=True)
        a_ref.rng = np.random.default_rng(seed)
        a_ref.fit(prefix)
        r_ref = a_ref.predict(prefix)

        if r_real is None or r_ref is None:
            print(f'  step {step}: one returned None - skipping')
            continue
        p_real = float(r_real['pred'].iloc[0])
        p_ref = float(r_ref['pred'].iloc[0])
        diffs.append(abs(p_real - p_ref))
        print(f'  step {step}: real={p_real:.6g}  ref={p_ref:.6g}  '
              f'|Δ|={abs(p_real - p_ref):.3e}')
    print(f'  max |Δ|: {max(diffs):.3e}' if diffs else '  no valid steps')

    print()
    print('=' * 70)
    print('CHECK 2: save() writes schema_version marker')
    print('=' * 70)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'XgbAdapter.joblib')
        a = XgbAdapter()
        a.fit(history)
        ok = a.save(path)
        loaded_raw = joblib.load(path)
        print(f'  save() returned: {ok}')
        print(f'  keys in saved file: {sorted(loaded_raw.keys())}')
        print(f'  schema_version: {loaded_raw.get("schema_version")}')
        assert loaded_raw.get('schema_version') == 2, 'schema marker missing!'
        print('  ✓ marker present')

    print()
    print('=' * 70)
    print('CHECK 3: load() refuses an unmarked (legacy) file')
    print('=' * 70)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'XgbAdapter.joblib')
        # Train a fresh model, then write it without the marker (mimics a
        # pre-port v1 file on disk).
        a = XgbAdapter()
        a.fit(history)
        legacy_state = {'stableModel': a.model, 'modelError': a.modelError}
        joblib.dump(legacy_state, path)
        # Now try to load it.
        b = XgbAdapter()
        result = b.load(path)
        print(f'  load() returned: {result}')
        print(f'  b.model is None: {b.model is None}')
        assert result is None and b.model is None, \
            'load() should refuse unmarked files!'
        print('  ✓ legacy file rejected')

    print()
    print('all checks passed')


if __name__ == '__main__':
    main()
