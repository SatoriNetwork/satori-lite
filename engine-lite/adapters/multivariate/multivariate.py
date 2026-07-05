"""Jordan-1 multivariate adapter: XGBoost head over peer-stream features with
random-swap peer search.

Spec: ``docs/engine/Jordan-1_MULTIVARIATE.md`` (retrain step §3, persisted
schema §4, condition/fit/predict §5) and ``docs/engine/MULTIVARIATE.md`` §3.4
(adapter architecture, deepcopy rules, engine hooks) / §5.5 (edge cases and the
Starter fallback safety net).

TimesFM (``use_tfm_on_target`` / ``tfm_delta``, Jordan-1 §2) is opt-in and off
by default. When on, TimesFM produces a rolling one-step forecast of the target
that becomes one extra feature column (``tfm_delta``) the head weighs against
target lags and peer deltas; training uses an incremental per-row cache and
inference makes one horizon-2 call shared across the 2-step autoregression. The
torch model is reached ONLY through the module-level :func:`_targetForecaster`
(wrapping ``TimesFmAdapter._ensureModel`` + ``_inference_lock``) and is never
stored on the instance, preserving deepcopy safety. The persisted schema (§4)
carries ``use_tfm_on_target``, ``tfm_delta_cache`` and ``tfm_min_context``
without bumping ``schema_version``.

Deepcopy safety (``engine.py``'s ``self.stable = copy.deepcopy(self.pilot)``):
the instance holds ONLY picklable state (a head, uuid lists, gain/ledger/
cooldown dicts, config scalars). The StreamStore is reached at call time
through the module-level :func:`_getStore` accessor — never stored on ``self``,
never a live sqlite connection. Tests patch :func:`_getStore` (design §7).
"""

from __future__ import annotations

import copy  # noqa: F401  (documents the deepcopy contract; engine does the copy)
import hashlib
import math
import os
import random
import threading
import time
from typing import Union

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import mean_absolute_error

from satoriengine.veda.adapters.interface import ModelAdapter, TrainingResult
from satoriengine.veda.adapters.multivariate import features as _features
from satoriengine.veda.adapters.multivariate import heads as _heads
from satoriengine.veda.adapters.multivariate import peer_search as _peer_search


# On-disk schema. v2 (Jordan-1 §4) already reserves the tfm_* fields; Task 7
# only fills them, so it need not bump this and force a needless retrain.
_MV_SCHEMA_VERSION = 2

# condition() gates (Jordan-1 §5 / MULTIVARIATE.md §3.4).
_MIN_TARGET_ROWS = 60
_MIN_STREAM_ROWS = 30
_MIN_QUALIFYING_STREAMS = 2

# Smallest training matrix we will fit/score a head on. condition() already
# floors the target at 60 rows, so this is just defensive.
_MIN_TRAIN_ROWS = 10

# Config defaults (Jordan-1 §5). Read from neuron config engine.multivariate
# when present (see _loadMultivariateConfig); otherwise these apply. The engine
# constructs adapters as ``adapter(uid=...)`` with no config kwargs, so there is
# no per-instance config plumbing — Task 6 need add none.
_DEFAULTS = {
    'head': 'xgboost',
    'top_k': 5,
    'peer_min_rows': 30,
    'keep_margin': 0.01,
    'cooldown_rows': 100,
    'max_candidates': 50,
    'max_ledger_entries': 200,
    'warm_start': False,
    'use_tfm_on_target': False,
    'tfm_min_context': 32,
}

# Warm-start correlation top-K knobs (MULTIVARIATE.md §3.2). Opt-in only.
_WARM_MIN_OVERLAP = 30
_WARM_MAX_ABS_CORR = 0.995


# --------------------------------------------------------------------------- #
# store access + condition TTL cache (module level so tests can patch/reset)
# --------------------------------------------------------------------------- #

def _getStore():
    """Return the process-wide shared StreamStore.

    The single indirection every store read in this module goes through, so a
    test points the adapter at a temp StreamStore by patching this one function
    (design §7 of MULTIVARIATE.md). Kept out of ``self`` for deepcopy safety.
    """
    from storage.manager import EngineStorageManager
    return EngineStorageManager.getInstance().stream_store


_COUNT_TTL_SECONDS = 60.0
_countCacheLock = threading.Lock()
_countCache = {'ts': -1e18, 'value': 0}


def _resetCountCache() -> None:
    """Drop the TTL cache (tests call this so a stale count never leaks in)."""
    with _countCacheLock:
        _countCache['ts'] = -1e18
        _countCache['value'] = 0


def _countStreamsWithMinRows(min_rows: int = _MIN_STREAM_ROWS) -> int:
    """Streams with >= ``min_rows`` rows, behind a ~60s TTL cache.

    condition() runs on every observation, so this single SQL count is cached
    to keep it cheap (Jordan-1 §5). The store is fetched through _getStore each
    time so patching still works when the cache is cold.
    """
    now = time.monotonic()
    with _countCacheLock:
        if now - _countCache['ts'] < _COUNT_TTL_SECONDS:
            return _countCache['value']
    value = _getStore().count_streams_with_min_rows(min_rows)
    with _countCacheLock:
        _countCache['ts'] = now
        _countCache['value'] = value
    return value


# --------------------------------------------------------------------------- #
# TimesFM-on-target (tfm_delta) plumbing (Jordan-1 §2). Module level so tests
# patch/reset it without ever loading torch or downloading the model, exactly
# like _getStore. The torch model is NEVER stored on the adapter instance
# (deepcopy safety): it is reached only through TimesFmAdapter's class-level
# shared model + inference lock, at call time.
# --------------------------------------------------------------------------- #

def _targetForecaster():
    """Return a ``forecaster(inputs, horizon) -> list[list[float]]`` backed by
    the shared TimesFM model, or ``None`` when TimesFM is unavailable.

    ``inputs`` is a list of context series; the return is one row per input,
    each a ``horizon``-long list of point forecasts. Never raises: an import
    failure, a failed model load (OOM / missing weights), or a forecast error
    surfaces as ``None`` / a propagating-free path so the head trains and serves
    as though TimesFM were disabled (Jordan-1 §2 "On failure").
    """
    try:
        from satoriengine.veda.adapters.timesfm.timesfm_adapter import TimesFmAdapter
        model = TimesFmAdapter._ensureModel()
        if model is None:
            return None
    except Exception:
        return None

    def _forecast(inputs, horizon):
        with TimesFmAdapter._inference_lock:
            point, _ = model.forecast(horizon=horizon, inputs=list(inputs))
        arr = np.asarray(point, dtype=float)
        return [[float(arr[i, h]) for h in range(horizon)]
                for i in range(arr.shape[0])]

    return _forecast


# Inference-time forecast cache, keyed by target uuid -> (last_real_epoch,
# horizon-2 forecast). Bounded to one entry per uuid (latest epoch only), so
# both predict calls of _runForecast's 2-step autoregression share ONE TimesFM
# call (Jordan-1 §2 "At inference").
_tfmPredictCacheLock = threading.Lock()
_tfmPredictCache: dict = {}


def _resetTfmPredictCache() -> None:
    """Drop the inference forecast cache (tests call this between cases)."""
    with _tfmPredictCacheLock:
        _tfmPredictCache.clear()


def _predictForecastCached(target_uuid, last_epoch: float, context_values):
    """Horizon-2 target forecast for ``last_epoch``, cached per target uuid.

    Returns the cached ``[f1, f2]`` when the uuid's stored epoch matches;
    otherwise issues ONE TimesFM call and caches it (overwriting any older epoch
    for this uuid, so the cache stays O(number of live targets)). ``None`` when
    TimesFM is unavailable or the call fails/returns too few points.
    """
    with _tfmPredictCacheLock:
        cached = _tfmPredictCache.get(target_uuid)
        if cached is not None and cached[0] == last_epoch:
            return cached[1]
    forecaster = _targetForecaster()
    if forecaster is None:
        return None
    try:
        out = forecaster([list(context_values)], 2)
    except Exception:
        out = None
    if not out or out[0] is None or len(out[0]) < 2:
        return None
    fc = out[0]
    with _tfmPredictCacheLock:
        _tfmPredictCache[target_uuid] = (last_epoch, fc)
    return fc


class MultivariateAdapter(ModelAdapter):

    @staticmethod
    def condition(*args, **kwargs) -> float:
        """Cheap per-observation gate. 1.0 only when every check passes.

        - target rows < 60 -> 0.0
        - fewer than 2 streams with >= 30 rows -> 0.0 (TTL-cached count)
        - any store exception -> 0.0 (never break adapter selection)
        - if config use_tfm_on_target: available RAM < 2 GB -> 0.0

        The RAM gate mirrors TimesFmAdapter.condition (the shared model is
        ~1.5 GB resident): it only fires when the CONFIG flag is on, so the
        default (flag off) behaviour is unchanged. ``availableRamGigs`` arrives
        as an engine kwarg (engine.py: ``p.condition(..., availableRamGigs=...)``).
        """
        try:
            data = kwargs.get('data', [])
            if data is None or len(data) < _MIN_TARGET_ROWS:
                return 0.0
            if _countStreamsWithMinRows(_MIN_STREAM_ROWS) < _MIN_QUALIFYING_STREAMS:
                return 0.0
            if _loadMultivariateConfig().get('use_tfm_on_target'):
                ram = kwargs.get('availableRamGigs')
                if isinstance(ram, (int, float)) and ram < 2.0:
                    return 0.0
            return 1.0
        except Exception:
            return 0.0

    def __init__(self, uid: Union[str, None] = None, **kwargs):
        super().__init__()
        self.uid: Union[str, None] = uid
        cfg = _loadMultivariateConfig()
        self.head_name: str = cfg['head']
        self.top_k: int = int(cfg['top_k'])
        self.peer_min_rows: int = int(cfg['peer_min_rows'])
        self.keep_margin: float = float(cfg['keep_margin'])
        self.cooldown_rows: int = int(cfg['cooldown_rows'])
        self.max_candidates: int = int(cfg['max_candidates'])
        self.max_ledger_entries: int = int(cfg['max_ledger_entries'])
        self.warm_start: bool = bool(cfg['warm_start'])

        # Trained/persisted state (Jordan-1 §4). All picklable, deepcopy-safe.
        self.head = None
        self.peer_uuids: list = []
        self.peer_gains: dict = {}
        self.feature_columns: list = []
        self.staleness_seconds: Union[float, None] = None
        self.modelError: Union[float, None] = None
        self.selected_at_rows: int = 0
        self.swap_ledger: list = []
        self.cooldown: dict = {}
        self.retired_peers: dict = {}
        self.peer_added_at: dict = {}

        # Task 7 (tfm_delta) placeholders — carried so §4 schema is stable.
        self.use_tfm_on_target: bool = bool(cfg['use_tfm_on_target'])
        self.tfm_delta_cache: dict = {}
        self.tfm_min_context: int = int(cfg['tfm_min_context'])

    # ------------------------------------------------------------------ #
    # persistence (Jordan-1 §4)
    # ------------------------------------------------------------------ #

    def save(self, modelpath: str, **kwargs) -> bool:
        """Persist the fitted adapter as the exact §4 schema via joblib."""
        if self.head is None:
            return False
        try:
            os.makedirs(os.path.dirname(modelpath), exist_ok=True)
            state = {
                'schema_version': _MV_SCHEMA_VERSION,
                'head_name': self.head_name,
                'head_state': self.head.state(),
                'peer_uuids': list(self.peer_uuids),
                'peer_gains': dict(self.peer_gains),
                'feature_columns': list(self.feature_columns),
                'staleness_seconds': float(self.staleness_seconds or 0.0),
                'modelError': float(self.modelError if self.modelError is not None else np.inf),
                'selected_at_rows': int(self.selected_at_rows),
                'swap_ledger': list(self.swap_ledger),
                'cooldown': dict(self.cooldown),
                'retired_peers': dict(self.retired_peers),
                'use_tfm_on_target': bool(self.use_tfm_on_target),
                'tfm_delta_cache': dict(self.tfm_delta_cache),
                'tfm_min_context': int(self.tfm_min_context),
                # Extra bookkeeping key (allowed; §4 keys are all present above).
                'peer_added_at': dict(self.peer_added_at),
            }
            joblib.dump(state, modelpath)
            return True
        except Exception:
            return False

    def load(self, modelPath: str, **kwargs) -> Union[None, "MultivariateAdapter"]:
        """Load persisted state. Wrong/missing schema or corruption -> None.

        Returning None forces a clean retrain (same contract as XGB's schema
        gate); the engine keeps serving the previous stable model meanwhile.
        """
        try:
            saved = joblib.load(modelPath)
            if not isinstance(saved, dict) or saved.get('schema_version') != _MV_SCHEMA_VERSION:
                return None
            head_cls = _heads.HEAD_REGISTRY.get(saved['head_name'])
            if head_cls is None:
                return None

            # Cache invalidation (Jordan-1 §4). Flipping use_tfm_on_target changes
            # the feature-column regime, so the persisted head/feature_columns no
            # longer match -> refuse the load (None) for a clean retrain, the same
            # mechanism load() already uses for a schema mismatch. Changing
            # tfm_min_context while the flag stays on keeps the columns but voids
            # the level-indexed cache -> drop it (predict recomputes live; the
            # next fit rebuilds the training cache under the new min_context).
            cfg = _loadMultivariateConfig()
            cfgFlag = bool(cfg.get('use_tfm_on_target', False))
            cfgMinCtx = int(cfg.get('tfm_min_context', 32))
            savedFlag = bool(saved.get('use_tfm_on_target', False))
            savedMinCtx = int(saved.get('tfm_min_context', 32))
            if savedFlag != cfgFlag:
                return None

            self.head = head_cls.fromState(saved['head_state'])
            self.head_name = saved['head_name']
            self.peer_uuids = list(saved['peer_uuids'])
            self.peer_gains = dict(saved['peer_gains'])
            self.feature_columns = list(saved['feature_columns'])
            self.staleness_seconds = float(saved['staleness_seconds'])
            self.modelError = float(saved['modelError'])
            self.selected_at_rows = int(saved['selected_at_rows'])
            self.swap_ledger = list(saved['swap_ledger'])
            self.cooldown = dict(saved['cooldown'])
            self.retired_peers = dict(saved['retired_peers'])
            self.use_tfm_on_target = savedFlag
            self.tfm_delta_cache = dict(saved.get('tfm_delta_cache', {}))
            self.tfm_min_context = savedMinCtx
            if savedFlag and savedMinCtx != cfgMinCtx:
                self.tfm_delta_cache = {}
                self.tfm_min_context = cfgMinCtx
            self.peer_added_at = dict(saved.get('peer_added_at', {}))
            return self
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # scoring (engine's compare loop)
    # ------------------------------------------------------------------ #

    def score(self, *args, **kwargs) -> float:
        """Lower is better: the retained 80/20 test MAE, inf if unfit."""
        if self.modelError is None or not math.isfinite(self.modelError):
            return float(np.inf)
        return float(self.modelError)

    def compare(self, other: Union[ModelAdapter, None] = None, **kwargs) -> bool:
        """True if this (pilot) should replace ``other`` (stable)."""
        if not isinstance(other, self.__class__):
            return True
        return self.score() < other.score()

    # ------------------------------------------------------------------ #
    # fit (Jordan-1 §3 retrain step)
    # ------------------------------------------------------------------ #

    def fit(self, data: pd.DataFrame, **kwargs) -> TrainingResult:
        try:
            store = _getStore()
            target = data
            currentRows = len(target) if target is not None else 0
            rng = random.Random(self._seed(currentRows))

            # Candidate pool: all store uuids, capped max_candidates largest-first;
            # load only those, measure their variance for the flat-stream filter.
            allUuids = store.stream_uuids()
            rowCounts = {u: store.row_count(u) for u in allUuids}
            ranked = sorted(allUuids, key=lambda u: rowCounts.get(u, 0), reverse=True)
            ranked = ranked[:self.max_candidates]
            loaded: dict = {}
            variances: dict = {}
            for u in ranked:
                if u == self.uid:
                    continue
                hist = store.history(u)
                loaded[u] = hist
                try:
                    variances[u] = float(np.nanvar(
                        pd.to_numeric(hist['value'], errors='coerce').to_numpy(dtype=float)))
                except Exception:
                    variances[u] = 0.0

            isRetrain = bool(self.peer_uuids) and self.head is not None

            # Build the TimesFM forecaster ONCE per fit (loads the shared model
            # at most once) and thread it through so tfm_delta is computed once
            # and held constant across the swap's baseline + candidate trainings
            # (Jordan-1 §3); the growing per-row cache does the rest. Kept a
            # local -- never stored on self -- so the instance stays deepcopy-safe.
            forecaster = _targetForecaster() if self.use_tfm_on_target else None

            if not isRetrain:
                return self._firstFit(
                    target, currentRows, rng, ranked, rowCounts, variances, loaded,
                    forecaster)
            return self._retrainFit(
                target, currentRows, rng, ranked, rowCounts, variances, loaded,
                forecaster)
        except Exception:
            return TrainingResult(-1, self)

    def _firstFit(self, target, currentRows, rng, ranked, rowCounts, variances,
                  loaded, forecaster=None):
        pool = _peer_search.eligiblePool(
            ranked, self.uid, rowCounts, self.cooldown, currentRows,
            peer_min_rows=self.peer_min_rows, exclude=(),
            cooldown_rows=self.cooldown_rows, variances=variances)
        if self.warm_start:
            peerUuids = self._warmStartPeers(target, pool, loaded)
        else:
            peerUuids = _peer_search.initialPeers(pool, self.top_k, rng)
        if not peerUuids:
            return TrainingResult(-1, self)

        trained = self._trainOn(target, peerUuids, loaded, forecaster)
        if trained is None:
            return TrainingResult(-1, self)
        head, mae, featCols, staleness = trained

        self.head = head
        self.peer_uuids = peerUuids
        self.peer_gains = head.featureGains()
        self.feature_columns = featCols
        self.staleness_seconds = staleness
        self.modelError = mae
        self.selected_at_rows = currentRows
        self.peer_added_at = {u: currentRows for u in peerUuids}
        self._appendLedger(currentRows, None, None, np.inf, mae, True, 'initial')
        return self._result(mae)

    def _retrainFit(self, target, currentRows, rng, ranked, rowCounts, variances,
                    loaded, forecaster=None):
        # (1) baseline on the current peer set.
        baseline = self._trainOn(target, self.peer_uuids, loaded, forecaster)
        if baseline is None:
            return TrainingResult(-1, self)
        baseHead, maeBase, baseFeatCols, baseStaleness = baseline
        baseGains = baseHead.featureGains()

        # Candidate pool excludes the current set.
        pool = _peer_search.eligiblePool(
            ranked, self.uid, rowCounts, self.cooldown, currentRows,
            peer_min_rows=self.peer_min_rows, exclude=self.peer_uuids,
            cooldown_rows=self.cooldown_rows, variances=variances)
        candidate = _peer_search.pickCandidate(pool, rng)

        if candidate is None:
            # No swap possible: keep the freshly-trained baseline, no ledger.
            self._commitBaseline(baseHead, maeBase, baseGains, baseFeatCols,
                                  baseStaleness, currentRows)
            return self._result(maeBase)

        # (2)-(4) weakest -> candidate, REBUILD the matrix (indices shift), retrain.
        weakest = _peer_search.weakestPeer(
            self.peer_uuids, baseGains, self.peer_added_at)
        newUuids = [candidate if u == weakest else u for u in self.peer_uuids]
        swapped = self._trainOn(target, newUuids, loaded, forecaster)
        maeNew = swapped[1] if swapped is not None else float(np.inf)

        # (5) accept iff mae_new beats mae_base by keep_margin.
        if swapped is not None and _peer_search.acceptSwap(
                maeBase, maeNew, self.keep_margin):
            newHead, _, newFeatCols, newStaleness = swapped
            self.head = newHead
            self.peer_uuids = newUuids
            self.peer_gains = newHead.featureGains()
            self.feature_columns = newFeatCols
            self.staleness_seconds = newStaleness
            self.modelError = maeNew
            self.selected_at_rows = currentRows
            self.retired_peers[weakest] = currentRows
            self.peer_added_at.pop(weakest, None)
            self.peer_added_at[candidate] = currentRows
            self._appendLedger(
                currentRows, weakest, candidate, maeBase, maeNew, True, 'margin')
            self.cooldown = _peer_search.pruneCooldown(
                self.cooldown, currentRows, self.cooldown_rows)
            return self._result(maeNew)

        # reject: restore the baseline, cool the candidate down, log the attempt.
        self._commitBaseline(baseHead, maeBase, baseGains, baseFeatCols,
                             baseStaleness, currentRows)
        self.cooldown[candidate] = currentRows
        self._appendLedger(
            currentRows, weakest, candidate, maeBase, maeNew, False, 'no_improvement')
        self.cooldown = _peer_search.pruneCooldown(
            self.cooldown, currentRows, self.cooldown_rows)
        return self._result(maeBase)

    def _commitBaseline(self, head, mae, gains, featCols, staleness, currentRows):
        self.head = head
        self.peer_gains = gains
        self.feature_columns = featCols
        self.staleness_seconds = staleness
        self.modelError = mae
        self.selected_at_rows = currentRows

    # ------------------------------------------------------------------ #
    # predict (Jordan-1 §5 / MULTIVARIATE.md §3.4)
    # ------------------------------------------------------------------ #

    def predict(self, data: pd.DataFrame, **kwargs) -> Union[None, pd.DataFrame]:
        """Ship ``lastObservedValue + head_delta``; None on any failure so the
        engine's Starter fallback (MULTIVARIATE.md §5.5) takes over — never raise.

        The 2-step autoregression (``_runForecast`` calls predict twice, the
        second time with a synthetic newer row appended) is handled implicitly:
        the peer streams have no observation newer than the store's last real
        epoch, so ``merge_asof(direction='backward')`` holds every peer at its
        last observed value for the synthetic row and the peer deltas collapse
        toward 0 — exactly the "uses last observed peer values" rule (Jordan-1
        §5). The target's own lags DO advance (the synthetic value is in the
        input frame), which is the intended autoregression.
        """
        try:
            if self.head is None or not self.peer_uuids or not self.feature_columns:
                return None
            if data is None or 'value' not in getattr(data, 'columns', []):
                return None
            store = _getStore()
            peerFrames = [(u, store.history(u)) for u in self.peer_uuids]
            aligned, _ = _features.alignPeers(
                data, peerFrames, stalenessSeconds=self.staleness_seconds)
            frame = _features.buildFrame(aligned)
            row = _features.inferenceRow(frame)
            if self.use_tfm_on_target:
                row = row.assign(tfm_delta=[float(self._predictTfmDelta(data, store))])
            if list(row.columns) != list(self.feature_columns):
                return None
            delta = float(self.head.predict(row)[0])
            if not math.isfinite(delta):
                return None
            values = pd.to_numeric(data['value'], errors='coerce').dropna()
            if values.empty:
                return None
            level = float(values.iloc[-1]) + delta
            times = pd.to_datetime(data['date_time'])
            lastTs = times.iloc[-1]
            cadence = times.diff().median() if len(times) >= 2 else pd.Timedelta(hours=1)
            if pd.isna(cadence):
                cadence = pd.Timedelta(hours=1)
            return pd.DataFrame({'date_time': [lastTs + cadence], 'pred': [level]})
        except Exception:
            return None

    def _predictTfmDelta(self, data, store) -> float:
        """tfm_delta for the inference row (Jordan-1 §2 "At inference").

        ONE horizon-2 TimesFM call is made from the target's REAL history (the
        store, up to ``last_real_epoch``) and cached per target uuid, so both
        predict calls of ``_runForecast``'s 2-step autoregression share it. The
        step is detected exactly as the adapter detects the augmented row: if
        ``data``'s last epoch is newer than the store's last real epoch we are
        on the augmented (second) step and use the horizon-2 forecast; otherwise
        the first step and the horizon-1 forecast. The delta denominator is the
        current row's level (``data``'s last value -- the real last observation
        on step 1, the synthetic value on the augmented step). Any failure /
        non-finite -> 0.0 and the prediction proceeds.
        """
        try:
            targetHist = store.history(self.uid) if self.uid is not None else None
            if targetHist is not None and not targetHist.empty:
                ctxVals = pd.to_numeric(
                    targetHist['value'], errors='coerce').dropna().to_numpy(dtype=float)
                lastRealEpoch = float(
                    pd.to_datetime(targetHist['date_time']).iloc[-1].timestamp())
            else:
                ctxVals = pd.to_numeric(
                    data['value'], errors='coerce').dropna().to_numpy(dtype=float)
                lastRealEpoch = float(
                    pd.to_datetime(data['date_time']).iloc[-1].timestamp())
            if len(ctxVals) == 0:
                return 0.0
            dataLastEpoch = float(pd.to_datetime(data['date_time']).iloc[-1].timestamp())
            isAugmented = dataLastEpoch > lastRealEpoch + 1e-6
            forecast = _predictForecastCached(self.uid, lastRealEpoch, ctxVals)
            if forecast is None or len(forecast) < 2:
                return 0.0
            f = forecast[1] if isAugmented else forecast[0]
            level = float(pd.to_numeric(data['value'], errors='coerce').dropna().iloc[-1])
            return _features.tfmDelta(f, level)
        except Exception:
            return 0.0

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _seed(self, currentRows: int) -> int:
        """Deterministic per (uid, row count): reproducible fits, but each
        retrain (different row count) explores a different draw. Uses a stable
        hash so determinism holds across processes, not just within one."""
        raw = f'{self.uid}:{currentRows}'.encode()
        return int(hashlib.sha256(raw).hexdigest(), 16) % (2 ** 32)

    def _history(self, store, uuid, loaded):
        hist = loaded.get(uuid)
        if hist is None:
            hist = store.history(uuid)
        return hist

    def _trainOn(self, target, peerUuids, loaded, forecaster=None):
        """Align -> build -> (attach tfm_delta) -> chronological 80/20 -> fit
        head -> test MAE.

        Returns ``(head, testMae, featureColumns, stalenessUsed)`` or None when
        there are too few usable training rows. The head serves from its 80%
        train fit (mirrors XgbAdapter); the 20% test split yields the MAE that
        drives score()/compare() and the swap accept/reject decision.

        When ``use_tfm_on_target`` is on, a ``tfm_delta`` column is attached
        after buildFrame and before the split (Jordan-1 §2). The cache is keyed
        by ROW POSITION in the aligned/built frame (== index into the cleaned,
        time-sorted target column, a stable prefix as history grows). Because
        ``aligned['target']`` is independent of the peer set, the first call in
        a retrain fills the cache and the second (candidate) call finds every
        row cached -- so the TimesFM values are identical across baseline and
        candidate, and no second TimesFM call fires (Jordan-1 §3).
        """
        store = _getStore()
        peerFrames = [(u, self._history(store, u, loaded)) for u in peerUuids]
        aligned, staleness = _features.alignPeers(
            target, peerFrames, stalenessSeconds=self.staleness_seconds)
        frame = _features.buildFrame(aligned)
        featCols = _features.featureColumns(len(peerUuids))
        if self.use_tfm_on_target:
            targetVals = pd.to_numeric(aligned['target'], errors='coerce').to_numpy(dtype=float)
            self.tfm_delta_cache = _features.tfmDeltaForRows(
                targetVals, self.tfm_delta_cache, self.tfm_min_context, forecaster)
            frame = frame.assign(tfm_delta=[
                float(self.tfm_delta_cache.get(i, 0.0)) for i in range(len(frame))])
            featCols = list(featCols) + ['tfm_delta']
        # Drop the final inference row (its label y is NaN by construction).
        train = frame.iloc[:-1]
        train = train[train['y'].notna()]
        if len(train) < _MIN_TRAIN_ROWS:
            return None
        X = train[featCols]
        y = train['y']
        split = int(len(train) * 0.8)
        if split < 1 or split >= len(train):
            return None
        Xtr, Xte = X.iloc[:split], X.iloc[split:]
        ytr, yte = y.iloc[:split], y.iloc[split:]
        if len(Xte) < 1:
            return None
        headCls = _heads.HEAD_REGISTRY.get(self.head_name, _heads.XgbHead)
        head = headCls().fit(Xtr, ytr)
        try:
            mae = float(mean_absolute_error(yte, head.predict(Xte)))
        except Exception:
            return None
        return head, mae, list(featCols), staleness

    def _warmStartPeers(self, target, pool, loaded):
        """Opt-in initial K by |Pearson corr| of aligned DELTAS (MULTIVARIATE.md
        §3.2): min overlap 30, drop NaN corr and near-duplicates (|corr|>0.995)."""
        store = _getStore()
        scored = []
        for u in pool:
            try:
                hist = self._history(store, u, loaded)
                aligned, _ = _features.alignPeers(
                    target, [(u, hist)], stalenessSeconds=self.staleness_seconds)
                tgtDelta = aligned['target'].pct_change()
                peerDelta = aligned['p0'].pct_change()
                pair = pd.concat([tgtDelta, peerDelta], axis=1).replace(
                    [np.inf, -np.inf], np.nan).dropna()
                if len(pair) < _WARM_MIN_OVERLAP:
                    continue
                corr = pair.iloc[:, 0].corr(pair.iloc[:, 1])
                if corr is None or not np.isfinite(corr):
                    continue
                if abs(corr) > _WARM_MAX_ABS_CORR:
                    continue
                scored.append((abs(float(corr)), u))
            except Exception:
                continue
        scored.sort(key=lambda t: t[0], reverse=True)
        return [u for _, u in scored[:self.top_k]]

    def _appendLedger(self, atRows, swappedOut, swappedIn, prevMae, newMae, kept, reason):
        entry = {
            'at_rows': int(atRows),
            'swapped_out': swappedOut,
            'swapped_in': swappedIn,
            'prev_test_mae': float(prevMae),
            'new_test_mae': float(newMae),
            'kept': bool(kept),
            'reason': reason,
        }
        self.swap_ledger = _peer_search.appendLedger(
            self.swap_ledger, entry, max_entries=self.max_ledger_entries)

    def _result(self, mae) -> TrainingResult:
        result = TrainingResult(1, self)
        result.modelError = float(mae) if mae is not None else None
        return result


def _loadMultivariateConfig() -> dict:
    """Resolve config from neuron ``engine.multivariate`` when available, else
    defaults. This is the only config path — the engine passes no config kwargs
    to adapter constructors (verified in engine.py: ``adapter(uid=...)``)."""
    cfg = dict(_DEFAULTS)
    try:
        from satorineuron import config as _neuronConfig
        neuron = _neuronConfig.get() or {}
        mv = (neuron.get('engine') or {}).get('multivariate') or {}
        for key in cfg:
            if key in mv:
                cfg[key] = mv[key]
    except Exception:
        pass
    return cfg
