"""Pure feature-engineering functions for the Jordan-1 multivariate adapter.

Everything here is a stateless pandas/numpy function: no engine singletons, no
sqlite, no torch. That keeps the feature schema independently unit-testable
(roadmap step 1 in ``docs/engine/Jordan-1_MULTIVARIATE.md``) and keeps the
adapter free to own peer ordering, persistence, and the TimesFM plumbing.

Feature schema (Jordan-1 section 1):

    target lags   pct-change of the target at lags [1, 2, 3, 5, 8]
    p{k}_delta_0  peer k pct-change, t-1 -> t   (observed, aligned)
    p{k}_delta_1  peer k pct-change, t-2 -> t-1 (observed, aligned)
    y             target LEVEL diff, t -> t+1   (label; last row NaN)

Unlike the older ``MULTIVARIATE.md`` v1 design there is NO ``p{k}_next`` and no
``shift(-1)`` on any peer column -- peer features are symmetric between train
and serve, so the head trains on exactly what it consumes live.

LEAKAGE INVARIANT: every feature at row t is a function of observations with
timestamp <= t only. Only the label ``y`` looks one step forward.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# Target pct-change lags. Exported so the adapter can persist / validate the
# feature column order deterministically alongside the saved head.
FEATURE_LAGS = [1, 2, 3, 5, 8]

# Denominator floor for pct-change: guards divide-by-(near)-zero so a stream
# that dips through 0 produces NaN (dropped / zero-filled) rather than +/-inf.
_EPS = 1e-9

# Publisher clock-skew guard: peer/target rows stamped further than this into
# the future (wall clock) are dropped at load time, so merge_asof(backward)
# can never treat a skewed "future" observation as available at time t.
_FUTURE_TOLERANCE_SECONDS = 60.0

# Fallback staleness tolerance when the target cadence cannot be measured
# (< 2 usable rows). Tests always supply enough rows to measure a real cadence.
_DEFAULT_CADENCE_SECONDS = 3600.0

# Winsorization guards untrusted third-party peer publishers: peer delta columns
# are clipped to their causal (expanding, <= t) 1%/99% quantiles. min_periods
# below this, the bound is undefined and the value is left unclipped.
_WINSOR_MIN_PERIODS = 10
_WINSOR_LOWER_Q = 0.01
_WINSOR_UPPER_Q = 0.99


def featureColumns(numPeers: int) -> list[str]:
    """Deterministic feature column order for ``numPeers`` aligned peers.

    Matches the column order produced by :func:`buildFrame` exactly (target
    lags first, then each peer's two deltas in peer order). The adapter persists
    this list with the head and asserts the inference row matches it.
    """
    cols = [f'lag_{lag}' for lag in FEATURE_LAGS]
    for k in range(numPeers):
        cols.append(f'p{k}_delta_0')
        cols.append(f'p{k}_delta_1')
    return cols


def _medianCadenceSeconds(dateTime: pd.Series) -> float | None:
    """Median spacing (seconds) of a sorted datetime series, or None."""
    diffs = dateTime.diff().dropna().dt.total_seconds()
    diffs = diffs[diffs > 0]
    if diffs.empty:
        return None
    return float(np.median(diffs))


def _cleanFrame(df: pd.DataFrame, now: pd.Timestamp) -> pd.DataFrame:
    """Return a clean ``[date_time, value]`` frame ready for merge_asof.

    Parse/sort timestamps, drop clock-skewed future rows, coerce values numeric,
    drop NaN rows, then collapse duplicate timestamps with groupby-mean (the
    ``xgbDataPreprocess`` pattern; merge_asof requires sorted unique keys).
    """
    if df is None or len(df) == 0 or 'date_time' not in df or 'value' not in df:
        return pd.DataFrame({'date_time': pd.Series([], dtype='datetime64[ns, UTC]'),
                             'value': pd.Series([], dtype=float)})
    out = df[['date_time', 'value']].copy()
    out['date_time'] = pd.to_datetime(out['date_time'], utc=True, errors='coerce')
    out['value'] = pd.to_numeric(out['value'], errors='coerce')
    out = out.dropna(subset=['date_time', 'value'])
    # Clock-skew lookahead guard: drop observations from the future.
    out = out[out['date_time'] <= now + pd.Timedelta(seconds=_FUTURE_TOLERANCE_SECONDS)]
    if out.empty:
        return out
    # Collapse duplicate timestamps (groupby mean) -> sorted unique keys.
    out = out.groupby('date_time', as_index=False)['value'].mean()
    out = out.sort_values('date_time').reset_index(drop=True)
    return out


def alignPeers(
    target: pd.DataFrame,
    peers: dict[str, pd.DataFrame] | list[tuple[str, pd.DataFrame]],
    stalenessSeconds: float | None = None,
) -> tuple[pd.DataFrame, float]:
    """Align peer streams onto the target's time grid, backward-as-of.

    Args:
        target: frame with at least ``['date_time', 'value']`` (whatever
            ``StreamStore.history()`` returns: ``['date_time', 'value', 'id']``).
        peers: ordered mapping / list of ``(uuid, frame)``. **Caller order is
            preserved** -- peer k gets column ``p{k}`` from the k-th entry. The
            adapter owns peer ordering; this function never sorts peers.
        stalenessSeconds: merge_asof tolerance. Default = 3x the target's median
            cadence; peer values older than this become NaN.

    Returns:
        ``(aligned, staleness)`` where ``aligned`` has columns
        ``['date_time', 'target', 'p0', 'p1', ...]`` on the (cleaned) target
        grid, and ``staleness`` is the tolerance actually used (so the caller
        can persist it).
    """
    now = pd.Timestamp.now(tz='UTC')
    items = list(peers.items()) if isinstance(peers, dict) else list(peers)

    tgt = _cleanFrame(target, now)

    if stalenessSeconds is None:
        cadence = _medianCadenceSeconds(tgt['date_time'])
        stalenessSeconds = 3.0 * (cadence if cadence is not None
                                  else _DEFAULT_CADENCE_SECONDS)

    aligned = pd.DataFrame({'date_time': tgt['date_time'].values})
    aligned['date_time'] = pd.to_datetime(aligned['date_time'], utc=True)
    aligned['target'] = tgt['value'].values

    tolerance = pd.Timedelta(seconds=float(stalenessSeconds))
    for k, (_uuid, peerDf) in enumerate(items):
        col = f'p{k}'
        clean = _cleanFrame(peerDf, now)
        if clean.empty or aligned.empty:
            aligned[col] = np.nan
            continue
        merged = pd.merge_asof(
            aligned[['date_time']],
            clean.rename(columns={'value': col}),
            on='date_time',
            direction='backward',
            tolerance=tolerance)
        aligned[col] = merged[col].values

    return aligned, float(stalenessSeconds)


def _pctChange(series: pd.Series, periods: int) -> pd.Series:
    """Epsilon-guarded pct-change ``(s - s.shift) / s.shift`` over ``periods``.

    Denominators with magnitude <= _EPS become NaN (rather than producing
    +/-inf), and any residual infinities are replaced with NaN.
    """
    prev = series.shift(periods)
    denom = prev.where(prev.abs() > _EPS)
    pct = (series - prev) / denom
    return pct.replace([np.inf, -np.inf], np.nan)


def _winsorize(col: pd.Series) -> pd.Series:
    """Clip a peer delta to its causal expanding 1%/99% quantiles.

    The bounds at row t use only rows 0..t (expanding), so winsorization never
    peeks ahead -- it preserves the leakage invariant. Rows before
    ``_WINSOR_MIN_PERIODS`` have no defined bound and are left unclipped.
    """
    lower = col.expanding(min_periods=_WINSOR_MIN_PERIODS).quantile(_WINSOR_LOWER_Q)
    upper = col.expanding(min_periods=_WINSOR_MIN_PERIODS).quantile(_WINSOR_UPPER_Q)
    return col.clip(lower=lower, upper=upper)


def buildFrame(aligned: pd.DataFrame, winsorize: bool = True) -> pd.DataFrame:
    """Build the feature/label matrix from an aligned frame.

    Args:
        aligned: output of :func:`alignPeers` -- ``['date_time', 'target',
            'p0', ...]``.
        winsorize: clip peer delta columns to expanding 1%/99% quantiles
            (default True; peers are untrusted publishers).

    Returns:
        Frame with columns ``['date_time'] + featureColumns(numPeers) + ['y']``.
        Target lag columns keep leading NaNs (insufficient history; XGBoost
        handles NaN natively). Peer delta columns are NaN->0.0 filled ("stale /
        missing peer = no change" -- the head trains on the same fallback it
        serves). The final row's ``y`` is NaN and is kept: it is the inference
        row.
    """
    peerCols = sorted(
        [c for c in aligned.columns if c.startswith('p') and c[1:].isdigit()],
        key=lambda c: int(c[1:]))
    numPeers = len(peerCols)

    out = pd.DataFrame({'date_time': aligned['date_time'].values})
    out['date_time'] = pd.to_datetime(out['date_time'], utc=True)

    target = aligned['target']

    # Target lag features (pct-change). Leading NaNs left for the head.
    for lag in FEATURE_LAGS:
        out[f'lag_{lag}'] = _pctChange(target, lag).values

    # Peer delta features. delta_0 = t-1 -> t, delta_1 = t-2 -> t-1.
    for k in range(numPeers):
        peer = aligned[f'p{k}']
        delta0 = _pctChange(peer, 1)
        delta1 = _pctChange(peer, 1).shift(1)
        if winsorize:
            delta0 = _winsorize(delta0)
            delta1 = _winsorize(delta1)
        # Stale / missing peer -> "no change".
        out[f'p{k}_delta_0'] = delta0.fillna(0.0).values
        out[f'p{k}_delta_1'] = delta1.fillna(0.0).values

    # Label: target LEVEL diff t -> t+1. Last row NaN (the inference row).
    out['y'] = (target.shift(-1) - target).values

    # Reorder to the canonical column layout.
    out = out[['date_time'] + featureColumns(numPeers) + ['y']]
    return out


def inferenceRow(frame: pd.DataFrame) -> pd.DataFrame:
    """Return the last row's feature columns (no ``y``, no ``date_time``).

    One-row frame ready for ``head.predict``. Column order matches
    :func:`featureColumns`.
    """
    featCols = [c for c in frame.columns if c not in ('date_time', 'y')]
    return frame[featCols].iloc[[-1]].reset_index(drop=True)
