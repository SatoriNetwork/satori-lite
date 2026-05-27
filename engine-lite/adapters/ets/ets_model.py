"""
ETS (Exponential Smoothing) adapter.

Wraps statsmodels.tsa.holtwinters.ExponentialSmoothing. Safe default across
seasonal, trending, and flat series. Zero new deps (statsmodels already
required by the engine).

Guards:
- Zero-variance series -> return last value (ExponentialSmoothing is unstable).
- Fit failure -> fall back to last-value naive.

Caching: after a successful fit we extract (α, β, φ, last level, last trend)
from the result. A subsequent predict() on the same (or longer-by-a-few-rows)
series walks the new observations through the Holt-Winters update equations
to extend the state and forecasts one step ahead - this is the equivalent of
statsmodels' `.append(refit=False)` on the state-space ETS model (which
`HoltWintersResults` does not expose). When a cold refit IS needed and the
structural params still match, we warm-start L-BFGS-B from the cached
smoothing params via `start_params`.
"""
from typing import Union
import os
import warnings
import joblib
import numpy as np
import pandas as pd
from satoriengine.veda.adapters._rng import make_rng
from satoriengine.veda.adapters.interface import ModelAdapter, TrainingResult
from satorilib.logging import info, debug


# Maximum extra observations to fold in via `.append(refit=False)` before we
# force a cold refit. Small enough that the no-refit shortcut doesn't drift
# arbitrarily far from a freshly-optimised model.
_MAX_APPEND_BEFORE_REFIT = 16


class ETSAdapter(ModelAdapter):

    @staticmethod
    def condition(*args, **kwargs) -> float:
        # Data-length floor is enforced centrally - see
        # MIN_OBSERVATIONS_FOR_TRAINED_MODEL in engine.py.
        return 1.0

    def __init__(self, uid: str = None, modelPath: str = None, **kwargs):
        super().__init__()
        self.uid = uid
        self.modelPath = modelPath
        self._lastSeries: Union[np.ndarray, None] = None
        self.modelError: Union[float, None] = None
        # Initial hyperparameters so predict() works before any fit(). Each
        # subsequent fit() redraws via _drawFitParams() for ongoing
        # exploration (the pilot/stable loop filters bad draws).
        self._fitParams: dict = self._drawFitParams()
        # Cached fit state. We don't keep the full statsmodels result object
        # for forecasting (it has no usable refit=False API); instead we
        # extract the smoothing params and final (level, trend) tuple and
        # walk forward via the Holt-Winters update equations. We DO keep the
        # raw params dict for warm-starting cold refits.
        self._cache: Union[dict, None] = None
        self._fittedStructural: Union[tuple, None] = None

    @staticmethod
    def _drawFitParams() -> dict:
        rng = make_rng()
        trend = rng.choice(['add', None])
        damped = bool(rng.integers(0, 2)) if trend == 'add' else False
        init = rng.choice(['estimated', 'heuristic'])
        return {
            'trend': trend,
            'damped_trend': damped,
            'initialization_method': init,
        }

    def _structuralKey(self, params: dict = None) -> tuple:
        p = params or self._fitParams
        return (p.get('trend'), p.get('damped_trend'), p.get('initialization_method'))

    def _invalidateCache(self) -> None:
        self._cache = None
        self._fittedStructural = None

    def _cacheFromResult(self, result, series_len: int) -> Union[dict, None]:
        """Pull (alpha, beta, phi, level_n, trend_n) off the fit so we can
        forecast / extend without re-fitting. Returns None if extraction
        fails - caller should treat the cache as cold."""
        try:
            p = result.params
            trend_kind = self._fitParams.get('trend')
            damped = bool(self._fitParams.get('damped_trend'))
            alpha = float(p.get('smoothing_level') or 0.0)
            if trend_kind == 'add':
                beta = float(p.get('smoothing_trend') or 0.0)
                phi_raw = p.get('damping_trend')
                phi = (
                    float(phi_raw)
                    if damped and phi_raw is not None and np.isfinite(phi_raw)
                    else 1.0
                )
                level = float(result.level[-1])
                trend = float(result.trend[-1])
            else:
                beta = 0.0
                phi = 1.0
                level = float(result.level[-1])
                trend = 0.0
            if not (np.isfinite(level) and np.isfinite(trend)
                    and np.isfinite(alpha) and np.isfinite(beta) and np.isfinite(phi)):
                return None
            return {
                'alpha': alpha,
                'beta': beta,
                'phi': phi,
                'level': level,
                'trend': trend,
                'trend_kind': trend_kind,
                'series_len': series_len,
                'raw_params': p,  # for warm-start
            }
        except Exception:
            return None

    @staticmethod
    def _extendState(cache: dict, new_obs: np.ndarray) -> dict:
        """Walk the Holt-Winters update equations forward through `new_obs`,
        returning a new cache dict with updated level/trend/series_len. This
        is the manual equivalent of `.append(new_obs, refit=False)`."""
        alpha = cache['alpha']
        beta = cache['beta']
        phi = cache['phi']
        l = cache['level']
        b = cache['trend']
        trend_kind = cache['trend_kind']
        if trend_kind == 'add':
            for x in new_obs:
                l_new = alpha * float(x) + (1.0 - alpha) * (l + phi * b)
                b = beta * (l_new - l) + (1.0 - beta) * phi * b
                l = l_new
        else:
            for x in new_obs:
                l = alpha * float(x) + (1.0 - alpha) * l
        return {
            **cache,
            'level': l,
            'trend': b,
            'series_len': cache['series_len'] + len(new_obs),
        }

    @staticmethod
    def _forecastFromCache(cache: dict) -> float:
        """One-step forecast from cached (level, trend, phi)."""
        if cache['trend_kind'] == 'add':
            return cache['level'] + cache['phi'] * cache['trend']
        return cache['level']

    def load(self, modelPath: str = None, **kwargs) -> Union[None, "ModelAdapter"]:
        modelPath = modelPath or self.modelPath
        if modelPath and os.path.isfile(modelPath):
            try:
                saved = joblib.load(modelPath)
                self.modelError = saved.get('modelError')
                return self
            except Exception:
                return None
        return None

    def save(self, modelPath: str = None, **kwargs) -> bool:
        modelPath = modelPath or self.modelPath
        if not modelPath:
            return True
        try:
            os.makedirs(os.path.dirname(modelPath), exist_ok=True)
            joblib.dump({'modelError': self.modelError}, modelPath)
            return True
        except Exception:
            return False

    def fit(self, data: pd.DataFrame, **kwargs) -> TrainingResult:
        """
        Each fit draws fresh hyperparameters and scores them via a one-step
        rolling-origin MAE backtest on the tail. The pilot/stable loop in
        StreamModel keeps the good draws and discards the rest, so this is
        ongoing exploration (mirrors XGB's per-fit behavior).

        New params invalidate the cache. We then pre-fit on the full series
        so the next predict() can skip straight to forecast.
        """
        self._fitParams = self._drawFitParams()
        self._invalidateCache()
        series = self._extractSeries(data)
        self._lastSeries = series
        self.modelError = self._rollingMaeCached(series, params=self._fitParams)
        # Pre-fit on the full series so producePrediction's first predict()
        # is essentially free, and its autoregression second call only does
        # a refit=False append.
        if series is not None and len(series) >= 5 and np.nanstd(series) >= 1e-12:
            _, result = self._fitFresh(series, self._fitParams)
            if result is not None:
                cache = self._cacheFromResult(result, len(series))
                if cache is not None:
                    self._cache = cache
                    self._fittedStructural = self._structuralKey()
        return TrainingResult(1, self)

    def score(self, series: Union[np.ndarray, None] = None, **kwargs) -> float:
        """
        If `series` is given, recompute MAE on it using this instance's
        locked-in hyperparameters. Used by compare() so both pilot and
        stable are evaluated on the SAME current dataset (otherwise stable
        carries a stale score from older/smaller data).
        """
        if series is not None:
            return self._rollingMaeCached(series, params=self._fitParams)
        return self.modelError if self.modelError is not None else float('inf')

    def compare(self, other: ModelAdapter = None, **kwargs) -> bool:
        """Lower MAE wins. Across classes, defer to the engine (return True)."""
        thisScore = self.score()
        if other is None or not isinstance(other, self.__class__):
            info(
                'model improved! (cross-class swap)'
                f'\n  pilot  score: {thisScore}'
                f'\n  stable model: {type(other).__name__ if other is not None else "None"}',
                color='green')
            return True
        # Re-score stable on current series so both are compared on the
        # same data (mirrors XGB.compare which passes self.testX/testY).
        try:
            otherScore = other.score(series=self._lastSeries)
        except Exception:
            otherScore = float('inf')
        isImproved = thisScore < otherScore
        if isImproved:
            info(
                'model improved!'
                f'\n  stable score: {otherScore}'
                f'\n  pilot  score: {thisScore}',
                color='green')
        else:
            debug(
                f'\nstable score: {otherScore}'
                f'\npilot  score: {thisScore}')
        return isImproved

    def predict(self, data: pd.DataFrame, **kwargs) -> Union[pd.DataFrame, None]:
        series = self._extractSeries(data)
        if series is None or len(series) == 0:
            return None
        pred = self._forecastCached(series)
        return self._wrapPrediction(data, pred)

    # ------------------------------------------------------------------
    # Cached forecasting
    # ------------------------------------------------------------------

    def _forecastCached(self, series: np.ndarray) -> float:
        """One-step forecast, extending the cached state when possible.

        Fast path: if structural params match and the new series extends the
        cached series by at most _MAX_APPEND_BEFORE_REFIT rows, walk the new
        observations through the HW update equations and forecast - no
        optimiser, no statsmodels call.

        Slow path: cold refit, warm-started from cached smoothing params if
        the structural triple still matches.
        """
        if len(series) < 5 or np.nanstd(series) < 1e-12:
            return float(series[-1])

        if (
            self._cache is not None
            and self._fittedStructural == self._structuralKey()
            and 0 < self._cache['series_len'] <= len(series)
            and len(series) - self._cache['series_len'] <= _MAX_APPEND_BEFORE_REFIT
        ):
            cached_len = self._cache['series_len']
            if len(series) == cached_len:
                pred = self._forecastFromCache(self._cache)
            else:
                new_obs = np.asarray(series[cached_len:], dtype=float)
                self._cache = self._extendState(self._cache, new_obs)
                pred = self._forecastFromCache(self._cache)
            if np.isfinite(pred):
                return float(pred)

        # Cold fit (warm-started if structural params still match).
        start_params = self._warmStartParams()
        pred, result = self._fitFresh(series, self._fitParams, start_params=start_params)
        if result is not None:
            cache = self._cacheFromResult(result, len(series))
            if cache is not None:
                self._cache = cache
                self._fittedStructural = self._structuralKey()
        return pred

    def _warmStartParams(self) -> Union[np.ndarray, None]:
        """Return cached smoothing params as a 1d array for `start_params`,
        in the order statsmodels expects. None on any mismatch -> cold start.

        Order is: smoothing_level, [smoothing_trend], [damping_trend],
        initial_level, [initial_trend]. statsmodels infers the count from
        the (trend, damped_trend) configuration of the new ExponentialSmoothing
        instance, which by construction matches the cached structural key
        when this method returns something.
        """
        if (
            self._cache is None
            or self._fittedStructural != self._structuralKey()
        ):
            return None
        try:
            p = self._cache['raw_params']
            trend_kind = self._fitParams.get('trend')
            damped = bool(self._fitParams.get('damped_trend'))
            arr = [p['smoothing_level']]
            if trend_kind == 'add':
                arr.append(p['smoothing_trend'])
                if damped:
                    arr.append(p['damping_trend'])
            arr.append(p['initial_level'])
            if trend_kind == 'add':
                arr.append(p['initial_trend'])
            out = np.asarray([float(x) for x in arr], dtype=float)
            if not np.all(np.isfinite(out)):
                return None
            return out
        except Exception:
            return None

    @staticmethod
    def _fitFresh(
        series: np.ndarray,
        params: dict,
        start_params: Union[np.ndarray, None] = None,
    ):
        """Cold fit on the full series; returns (forecast_value, Results) or
        (fallback_value, None) on failure. `start_params` is best-effort -
        statsmodels will raise on shape mismatch, in which case we retry cold."""
        try:
            from statsmodels.tsa.holtwinters import ExponentialSmoothing
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                model = ExponentialSmoothing(
                    series,
                    trend=params['trend'],
                    damped_trend=params['damped_trend'],
                    seasonal=None,
                    initialization_method=params['initialization_method'],
                )
                fit_kwargs = dict(
                    optimized=True,
                    use_brute=False,
                    minimize_kwargs={'options': {'maxiter': 50}},
                )
                try:
                    result = model.fit(start_params=start_params, **fit_kwargs)
                except (TypeError, ValueError):
                    result = model.fit(**fit_kwargs)
                pred = float(result.forecast(1)[0])
            if not np.isfinite(pred):
                return float(series[-1]), None
            return pred, result
        except Exception:
            return float(series[-1]), None

    def _rollingMaeCached(
        self,
        series: Union[np.ndarray, None],
        horizon: int = 3,
        params: dict = None,
    ) -> float:
        """
        One-step rolling-origin MAE on the last `horizon` points. Within this
        call structural params are constant, so we fit once on the smallest
        prefix and walk forward via the HW update equations - turning N full
        L-BFGS-B fits into 1 fit + (N-1) O(1) state updates.

        Note: the manual walk holds the smoothing params (alpha/beta/phi)
        FROZEN at the first fit's values, whereas the legacy stateless
        rollingMae re-optimised them at every step. For horizon=3 the
        difference is tiny and bounded, but it IS a semantic change - the
        rolling MAE now scores "how well does this fit extrapolate", not
        "how well does refitting at each step recover the next point".
        """
        if series is None or len(series) < 10:
            return float('inf')
        params = params or self._fitParams
        n = len(series)
        start = max(5, n - horizon)
        if start >= n:
            return float('inf')

        # Build the initial local cache by fitting on series[:start].
        _, result = self._fitFresh(series[:start], params)
        if result is None:
            return float('inf')
        local_cache = self._localCacheFromResult(result, start, params)
        if local_cache is None:
            # Fall back to per-step cold fits if extraction failed.
            return self._rollingMae(series, horizon=horizon, params=params)

        errs = []
        for i in range(start, n):
            pred = self._forecastFromCache(local_cache)
            if not np.isfinite(pred):
                pred = float(local_cache['level'])
            errs.append(abs(pred - float(series[i])))
            # Fold the observed value into the cache for the next step.
            local_cache = self._extendState(
                local_cache, np.asarray([series[i]], dtype=float))

        if not errs:
            return float('inf')
        return float(np.mean(errs))

    @staticmethod
    def _localCacheFromResult(result, series_len: int, params: dict) -> Union[dict, None]:
        """Variant of _cacheFromResult that takes explicit `params` (so it
        works in static-style rolling MAE without leaning on `self._fitParams`)."""
        try:
            p = result.params
            trend_kind = params.get('trend')
            damped = bool(params.get('damped_trend'))
            alpha = float(p.get('smoothing_level') or 0.0)
            if trend_kind == 'add':
                beta = float(p.get('smoothing_trend') or 0.0)
                phi_raw = p.get('damping_trend')
                phi = (
                    float(phi_raw)
                    if damped and phi_raw is not None and np.isfinite(phi_raw)
                    else 1.0
                )
                level = float(result.level[-1])
                trend = float(result.trend[-1])
            else:
                beta, phi = 0.0, 1.0
                level = float(result.level[-1])
                trend = 0.0
            if not (np.isfinite(level) and np.isfinite(trend)
                    and np.isfinite(alpha) and np.isfinite(beta) and np.isfinite(phi)):
                return None
            return {
                'alpha': alpha,
                'beta': beta,
                'phi': phi,
                'level': level,
                'trend': trend,
                'trend_kind': trend_kind,
                'series_len': series_len,
            }
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Legacy static helpers - retained for any external callers; the in-class
    # paths use the cached methods above.
    # ------------------------------------------------------------------

    @staticmethod
    def _forecastOne(series: np.ndarray, params: dict = None) -> float:
        """Stateless single-step forecast. Retained for compatibility; the
        adapter itself goes through `_forecastCached`."""
        if len(series) < 5 or np.nanstd(series) < 1e-12:
            return float(series[-1])
        params = params or {'trend': 'add', 'damped_trend': False, 'initialization_method': 'estimated'}
        try:
            from statsmodels.tsa.holtwinters import ExponentialSmoothing
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                model = ExponentialSmoothing(
                    series,
                    trend=params['trend'],
                    damped_trend=params['damped_trend'],
                    seasonal=None,
                    initialization_method=params['initialization_method'],
                ).fit(
                    optimized=True,
                    use_brute=False,
                    minimize_kwargs={'options': {'maxiter': 50}},
                )
                pred = float(model.forecast(1)[0])
            if not np.isfinite(pred):
                return float(series[-1])
            return pred
        except Exception:
            return float(series[-1])

    @classmethod
    def _rollingMae(cls, series: Union[np.ndarray, None], horizon: int = 3, params: dict = None) -> float:
        """Stateless rolling MAE - retained for compatibility (e.g. tests that
        want pre-cache behavior). The adapter itself uses _rollingMaeCached."""
        if series is None or len(series) < 10:
            return float('inf')
        n = len(series)
        start = max(5, n - horizon)
        errs = []
        for i in range(start, n):
            pred = cls._forecastOne(series[:i], params=params)
            errs.append(abs(pred - float(series[i])))
        if not errs:
            return float('inf')
        return float(np.mean(errs))

    @staticmethod
    def _extractSeries(data: pd.DataFrame) -> Union[np.ndarray, None]:
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
    def _wrapPrediction(data: pd.DataFrame, pred: float) -> pd.DataFrame:
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
