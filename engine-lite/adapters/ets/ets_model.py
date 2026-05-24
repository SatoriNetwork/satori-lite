"""
ETS (Exponential Smoothing) adapter.

Wraps statsmodels.tsa.holtwinters.ExponentialSmoothing. Safe default across
seasonal, trending, and flat series. Zero new deps (statsmodels already
required by the engine).

Guards:
- Zero-variance series → return last value (ExponentialSmoothing is unstable).
- Fit failure → fall back to last-value naive.
"""
from typing import Union
import os
import warnings
import joblib
import numpy as np
import pandas as pd
from satoriengine.veda.adapters.interface import ModelAdapter, TrainingResult
from satorilib.logging import info, debug


class ETSAdapter(ModelAdapter):

    @staticmethod
    def condition(*args, **kwargs) -> float:
        # Data-length floor is enforced centrally — see
        # MIN_OBSERVATIONS_FOR_TRAINED_MODEL in engine.py.
        return 1.0

    def __init__(self, uid: str = None, modelPath: str = None, **kwargs):
        super().__init__()
        self.uid = uid
        self.modelPath = modelPath
        self._lastSeries: Union[np.ndarray, None] = None
        self._lastFingerprint: Union[bytes, None] = None
        self.modelError: Union[float, None] = None

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
        ETS refits on every predict (cheap). fit() caches the series and
        computes a one-step rolling-origin MAE on the tail so the engine's
        score-based selection has a real number to compare.

        Fast-path: if the input series hashes identical to the previous
        fit() call, skip the rolling MAE entirely and reuse the cached
        modelError. This avoids burning CPU when the engine re-trains on
        an unchanged dataset (e.g. trainingDelay=0 hot loops).
        """
        series = self._extractSeries(data)
        fingerprint = series.tobytes() if series is not None else None
        if (fingerprint is not None
                and fingerprint == self._lastFingerprint
                and self.modelError is not None):
            return TrainingResult(1, self)
        self._lastSeries = series
        self._lastFingerprint = fingerprint
        self.modelError = self._rollingMae(series)
        return TrainingResult(1, self)

    def score(self, **kwargs) -> float:
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
        try:
            otherScore = other.score()
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
        pred = self._forecastOne(series)
        return self._wrapPrediction(data, pred)

    @staticmethod
    def _forecastOne(series: np.ndarray) -> float:
        if len(series) < 5 or np.nanstd(series) < 1e-12:
            return float(series[-1])
        try:
            from statsmodels.tsa.holtwinters import ExponentialSmoothing
            # Suppress statsmodels' ConvergenceWarning and related noise —
            # we fall back to last-value if the fit is unusable anyway.
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                # use_brute=False skips the grid pre-search; maxiter caps the
                # L-BFGS-B run so pathological series (very-large magnitudes,
                # near-constant tails) can't pin the training worker.
                model = ExponentialSmoothing(
                    series,
                    trend='add',
                    seasonal=None,
                    initialization_method='estimated',
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
    def _rollingMae(cls, series: Union[np.ndarray, None], horizon: int = 3) -> float:
        """
        One-step rolling-origin MAE on the last `horizon` points. Default
        horizon=3 is intentionally tight — each iteration runs a full
        ExponentialSmoothing.fit() (L-BFGS-B), so this is the dominant CPU
        cost per training cycle. Three samples is enough to score a model
        without burning minutes per stream.
        """
        if series is None or len(series) < 10:
            return float('inf')
        n = len(series)
        start = max(5, n - horizon)
        errs = []
        for i in range(start, n):
            pred = cls._forecastOne(series[:i])
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
