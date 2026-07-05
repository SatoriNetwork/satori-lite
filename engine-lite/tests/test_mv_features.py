"""Unit tests for the Jordan-1 multivariate feature functions.

Covers the leakage invariant, staleness / merge_asof-backward behavior, the
data-quality guards from ``docs/engine/MULTIVARIATE.md`` section 5.5 (duplicate
timestamps, future-stamp drop, zero/near-zero epsilon guard, winsorization),
the label definition, and deterministic column ordering.

``features.py`` is loaded directly from its file path so the tests never touch
``adapters/__init__.py`` or ``adapters/multivariate/__init__.py`` (the latter
still carries the StarterAdapter copy-paste bug owned by a later task).

Runs under pytest (``python -m pytest``) or standalone
(``python test_mv_features.py``) since the image ships no pytest.
"""

import importlib.util
import os

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_FEATURES_PATH = os.path.join(
    _HERE, '..', 'adapters', 'multivariate', 'features.py')
_spec = importlib.util.spec_from_file_location('mv_features', _FEATURES_PATH)
features = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(features)

alignPeers = features.alignPeers
buildFrame = features.buildFrame
inferenceRow = features.inferenceRow
featureColumns = features.featureColumns
FEATURE_LAGS = features.FEATURE_LAGS

_BASE = pd.Timestamp('2024-01-01T00:00:00', tz='UTC')


def _frame(values, start=_BASE, freq_seconds=3600):
    """Build a StreamStore-shaped frame on a regular grid."""
    n = len(values)
    times = [start + pd.Timedelta(seconds=freq_seconds * i) for i in range(n)]
    return pd.DataFrame({
        'date_time': times,
        'value': list(values),
        'id': [f'h{i}' for i in range(n)],
    })


def _frame_at(times, values):
    """Build a frame from explicit timestamps."""
    return pd.DataFrame({
        'date_time': list(times),
        'value': list(values),
        'id': [f'h{i}' for i in range(len(values))],
    })


# --------------------------------------------------------------------------- #
# leakage
# --------------------------------------------------------------------------- #

def test_leakage_future_perturbation_does_not_change_past_features():
    n = 40
    t = 20
    rng = np.random.default_rng(0)
    tgt_vals = 100 + np.cumsum(rng.normal(size=n))
    peer_vals = 50 + np.cumsum(rng.normal(size=n))

    target = _frame(tgt_vals)
    peer = _frame(peer_vals)
    aligned, _ = alignPeers(target, {'peerA': peer})
    base = buildFrame(aligned)

    # Perturb every value strictly after row t, in both target and peer.
    tgt2 = tgt_vals.copy()
    peer2 = peer_vals.copy()
    tgt2[t + 1:] += 1000.0
    peer2[t + 1:] *= 5.0
    aligned2, _ = alignPeers(_frame(tgt2), {'peerA': _frame(peer2)})
    pert = buildFrame(aligned2)

    featCols = [c for c in base.columns if c not in ('date_time', 'y')]
    a = base[featCols].iloc[:t + 1].to_numpy(dtype=float)
    b = pert[featCols].iloc[:t + 1].to_numpy(dtype=float)
    assert np.allclose(a, b, equal_nan=True), \
        'features at rows <= t changed when only future values were perturbed'


# --------------------------------------------------------------------------- #
# staleness
# --------------------------------------------------------------------------- #

def test_staleness_old_peer_becomes_nan_then_zero_feature():
    # Hourly target, 20 rows. Peer stops after hour 4; default tolerance is
    # 3x median cadence = 3h, so target rows > 3h past the last peer obs go NaN.
    target = _frame(np.arange(1, 21, dtype=float))
    peer = _frame(np.arange(1, 6, dtype=float))  # only first 5 hours
    aligned, tol = alignPeers(target, {'p': peer})
    assert tol == 3 * 3600

    # Row 8 (hour 8) is 4h past the last peer obs (hour 4) -> stale -> NaN.
    assert np.isnan(aligned['p0'].iloc[8])
    frame = buildFrame(aligned)
    assert frame['p0_delta_0'].iloc[8] == 0.0
    assert frame['p0_delta_1'].iloc[8] == 0.0
    # No NaN survives in the peer delta columns anywhere.
    assert not frame['p0_delta_0'].isna().any()
    assert not frame['p0_delta_1'].isna().any()


def test_merge_asof_backward_never_uses_future_peer_value():
    # Target at 09:00 and 10:00. Peer observed at 09:00 (=1.0) and 10:30 (=999).
    # merge_asof backward at 10:00 must pick the 09:00 value, never 10:30.
    t0 = pd.Timestamp('2024-06-01T09:00:00', tz='UTC')
    t1 = pd.Timestamp('2024-06-01T10:00:00', tz='UTC')
    target = _frame_at([t0, t1], [10.0, 11.0])
    peer = _frame_at(
        [t0, pd.Timestamp('2024-06-01T10:30:00', tz='UTC')], [1.0, 999.0])
    # Wide tolerance so staleness is not the reason 999 is excluded.
    aligned, _ = alignPeers(target, {'p': peer}, stalenessSeconds=100000)
    assert aligned['p0'].iloc[1] == 1.0
    assert 999.0 not in set(aligned['p0'].dropna().tolist())


# --------------------------------------------------------------------------- #
# duplicate + future-stamp data-quality guards
# --------------------------------------------------------------------------- #

def test_duplicate_timestamps_collapsed_by_mean():
    tx = pd.Timestamp('2024-06-01T09:00:00', tz='UTC')
    target = _frame_at([tx], [5.0])
    # Two peer rows at the SAME timestamp -> collapse to mean (15.0).
    peer = _frame_at([tx, tx], [10.0, 20.0])
    aligned, _ = alignPeers(target, {'p': peer}, stalenessSeconds=100000)
    assert aligned['p0'].iloc[0] == 15.0


def test_future_stamped_peer_rows_dropped():
    tx = pd.Timestamp('2024-06-01T09:00:00', tz='UTC')
    future = pd.Timestamp.now(tz='UTC') + pd.Timedelta(days=2)
    target = _frame_at([tx], [5.0])
    peer = _frame_at([tx, future], [1.0, 99999.0])
    aligned, _ = alignPeers(target, {'p': peer}, stalenessSeconds=10**9)
    assert 99999.0 not in set(aligned['p0'].dropna().tolist())
    assert aligned['p0'].iloc[0] == 1.0


# --------------------------------------------------------------------------- #
# zero / near-zero epsilon guard
# --------------------------------------------------------------------------- #

def test_zero_and_near_zero_values_produce_no_inf_and_no_peer_nan():
    n = 30
    # Target strictly positive (target lags stay finite for a clean assertion).
    target = _frame(np.linspace(1.0, 30.0, n))
    # Peer laced with zeros and near-zero values -> divide-by-~0 in pct-change.
    peer_vals = np.linspace(-2.0, 2.0, n)
    peer_vals[10] = 0.0
    peer_vals[11] = 1e-13
    peer_vals[12] = -1e-13
    peer = _frame(peer_vals)
    aligned, _ = alignPeers(target, {'p': peer})
    frame = buildFrame(aligned)

    featCols = [c for c in frame.columns if c not in ('date_time', 'y')]
    vals = frame[featCols].to_numpy(dtype=float)
    assert not np.isinf(vals).any(), 'infinity leaked into features'
    # Peer deltas are NaN->0 filled: never NaN.
    assert not frame['p0_delta_0'].isna().any()
    assert not frame['p0_delta_1'].isna().any()
    # Target lags finite once enough history exists (target has no zero cross).
    lagCols = [f'lag_{lag}' for lag in FEATURE_LAGS]
    tail = frame[lagCols].iloc[max(FEATURE_LAGS):]
    assert np.isfinite(tail.to_numpy(dtype=float)).all()


# --------------------------------------------------------------------------- #
# winsorization
# --------------------------------------------------------------------------- #

def test_winsorization_bounds_extreme_peer_delta():
    n = 60
    rng = np.random.default_rng(1)
    # Well-behaved peer with small multiplicative wiggles ...
    peer_vals = 100 * np.cumprod(1 + rng.normal(0, 0.01, size=n))
    # ... then a single enormous spike well past the winsor warmup window.
    peer_vals[45] *= 20.0
    target = _frame(100 + np.cumsum(rng.normal(size=n)))
    peer = _frame(peer_vals)
    aligned, _ = alignPeers(target, {'p': peer})

    clipped = buildFrame(aligned, winsorize=True)['p0_delta_0']
    raw = buildFrame(aligned, winsorize=False)['p0_delta_0']
    assert raw.abs().max() > clipped.abs().max(), 'winsorize did not clip'
    # The spike row specifically is pulled in.
    assert abs(clipped.iloc[45]) < abs(raw.iloc[45])


# --------------------------------------------------------------------------- #
# label
# --------------------------------------------------------------------------- #

def test_label_is_next_step_level_diff_and_last_is_nan():
    vals = np.array([1.0, 3.0, 6.0, 10.0, 15.0, 21.0], dtype=float)
    target = _frame(vals)
    aligned, _ = alignPeers(target, {})
    frame = buildFrame(aligned)
    lvl = aligned['target'].to_numpy(dtype=float)
    for i in range(len(vals) - 1):
        assert np.isclose(frame['y'].iloc[i], lvl[i + 1] - lvl[i])
    assert np.isnan(frame['y'].iloc[-1]), 'last-row label must be NaN'
    assert len(frame) == len(vals), 'inference row must be kept'


# --------------------------------------------------------------------------- #
# column ordering / inference row
# --------------------------------------------------------------------------- #

def test_feature_columns_matches_build_frame_order():
    n = 20
    target = _frame(100 + np.arange(n, dtype=float))
    peers = {
        'a': _frame(10 + np.arange(n, dtype=float)),
        'b': _frame(20 + np.arange(n, dtype=float)),
        'c': _frame(30 + np.arange(n, dtype=float)),
    }
    aligned, _ = alignPeers(target, peers)
    frame = buildFrame(aligned)
    produced = [c for c in frame.columns if c not in ('date_time', 'y')]
    assert produced == featureColumns(3)


def test_inference_row_shape_and_columns():
    n = 20
    target = _frame(100 + np.arange(n, dtype=float))
    aligned, _ = alignPeers(target, {'a': _frame(5 + np.arange(n, dtype=float))})
    frame = buildFrame(aligned)
    row = inferenceRow(frame)
    assert len(row) == 1
    assert list(row.columns) == featureColumns(1)
    assert 'y' not in row.columns and 'date_time' not in row.columns
    assert np.isfinite(row.to_numpy(dtype=float)).all()


def test_peer_order_is_caller_order_not_sorted():
    n = 15
    target = _frame(100 + np.arange(n, dtype=float))
    # Deliberately reverse-sorted uuids: p0 must map to 'zeta', not 'alpha'.
    peers = [('zeta', _frame(np.full(n, 7.0))),
             ('alpha', _frame(np.full(n, 9.0)))]
    aligned, _ = alignPeers(target, peers)
    assert aligned['p0'].iloc[-1] == 7.0
    assert aligned['p1'].iloc[-1] == 9.0


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
