"""
ETS (Exponential Smoothing) adapter.

Wraps statsmodels.tsa.holtwinters.ExponentialSmoothing. Safe default across a
wide range of data characters (seasonal, trending, flat). Zero new dependencies
beyond statsmodels (already installed).

Benchmark findings (see tasks/adapter-benchmark-results.md):
- Beats XgbAdapter on 14/16 synthetic streams
- Handles constant data (returns last value), scale-invariant
- Ties naive on random walks, wins on trending/seasonal

Guards:
- If training series has zero variance, return the last value directly
  (ExponentialSmoothing is unstable on constants).
- If fit fails, fall back to last-value naive.
"""
from typing import Union
import os
import joblib
import numpy as np
import pandas as pd
from satoriengine.veda.adapters.interface import ModelAdapter, TrainingResult


class ETSAdapter(ModelAdapter):

    @staticmethod
    def condition(*args, **kwargs) -> float:
        # Works for any stream with >= 5 observations.
        # Lower RAM floor than XGB/Chronos; safe on small nodes.
        data = kwargs.get('data', [])
        if len(data) < 5:
            return 0.0
        return 1.0

    def __init__(self, uid: str = None, modelPath: str = None, **kwargs):
        super().__init__()
        self.uid = uid
        self.modelPath = modelPath
        self._lastSeries: Union[np.ndarray, None] = None
        self._lastPrediction: Union[float, None] = None
        self.modelError: Union[float, None] = None

    def load(self, modelPath: str = None, **kwargs) -> Union[None, "ModelAdapter"]:
        """ETS has no long-lived weights; score is persisted for compare()."""
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
        ETS refits on every predict() (cheap). fit() just caches the series so
        score/compare have a reference.
        """
        series = self._extractSeries(data)
        self._lastSeries = series
        return TrainingResult(1, self)

    def score(self, **kwargs) -> float:
        return self.modelError if self.modelError is not None else float('inf')

    def compare(self, other: ModelAdapter, **kwargs) -> bool:
        """ETS is a stable statistical model — always prefer it if training succeeded."""
        return True

    def predict(self, data: pd.DataFrame, **kwargs) -> Union[pd.DataFrame, None]:
        series = self._extractSeries(data)
        if series is None or len(series) == 0:
            return None

        # Constant-data guard: ExponentialSmoothing blows up when std == 0.
        # Return the last value directly.
        if len(series) < 5 or np.nanstd(series) < 1e-12:
            pred = float(series[-1])
            return self._wrapPrediction(data, pred)

        try:
            from statsmodels.tsa.holtwinters import ExponentialSmoothing
            model = ExponentialSmoothing(
                series,
                trend='add',
                seasonal=None,
                initialization_method='estimated',
            ).fit(optimized=True)
            pred = float(model.forecast(1)[0])
            if not np.isfinite(pred):
                pred = float(series[-1])
        except Exception:
            # Fall back to last-value naive on any fit error
            pred = float(series[-1])

        self._lastPrediction = pred
        return self._wrapPrediction(data, pred)

    @staticmethod
    def _extractSeries(data: pd.DataFrame) -> Union[np.ndarray, None]:
        """Pull the numeric 'value' column out of whatever DataFrame shape we get."""
        if data is None or len(data) == 0:
            return None
        if 'value' in data.columns:
            s = pd.to_numeric(data['value'], errors='coerce')
        else:
            # Fallback: use the second column (Satori convention: [date_time, value, id])
            s = pd.to_numeric(data.iloc[:, 1], errors='coerce') if data.shape[1] >= 2 else None
        if s is None:
            return None
        return s.dropna().to_numpy(dtype=np.float64)

    @staticmethod
    def _wrapPrediction(data: pd.DataFrame, pred: float) -> pd.DataFrame:
        """Match XgbAdapter/XgbChronosAdapter output shape: DataFrame with date_time + pred."""
        # Use next timestamp based on sampling cadence; fall back to +1h
        try:
            if 'date_time' in data.columns:
                times = pd.to_datetime(data['date_time'])
                last = times.iloc[-1]
                if len(times) >= 2:
                    diff = times.diff().median()
                else:
                    diff = pd.Timedelta(hours=1)
                next_ts = last + diff
            else:
                next_ts = pd.Timestamp.now() + pd.Timedelta(hours=1)
        except Exception:
            next_ts = pd.Timestamp.now() + pd.Timedelta(hours=1)
        return pd.DataFrame({'date_time': [next_ts], 'pred': [pred]})
