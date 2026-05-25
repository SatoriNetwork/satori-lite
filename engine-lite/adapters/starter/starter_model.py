import pandas as pd
import numpy as np
from typing import Union
from collections import namedtuple
from sklearn.linear_model import LinearRegression
from satoriengine.veda.adapters._rng import make_rng
from satoriengine.veda.adapters.interface import ModelAdapter, TrainingResult


class StarterAdapter(ModelAdapter):

    @staticmethod
    def condition(*args, **kwargs) -> float:
        if (
            isinstance(kwargs.get('availableRamGigs'), float)
            and kwargs.get('availableRamGigs') < .025
        ):
            return 1.0
        if len(kwargs.get('data', [])) <= 10:
            return 1.0
        return 0.0

    def __init__(self, **kwargs):
        super().__init__()
        self.model = None

    def load(self, modelPath: str, **kwargs) -> Union[None, "ModelAdapter"]:
        """loads the model model from disk if present"""

    def save(self, modelpath: str, **kwargs) -> bool:
        """saves the stable model to disk"""
        return True

    def fit(self, data: pd.DataFrame, **kwargs) -> TrainingResult:
        # we don't need to fit anything
        #forecast = StarterAdapter.starterEnginePipeline(data)
        #if self.model is None:
        #    self.model = forecast
        return TrainingResult(-1, self)

    def compare(self, other: ModelAdapter, **kwargs) -> bool:
        return True

    def score(self, **kwargs) -> float:
        return 0.0

    def predict(self, data, **kwargs) -> Union[None, pd.DataFrame]:
        """prediction without training"""
        return StarterAdapter.starterEnginePipeline(data)

    @staticmethod
    def starterEnginePipeline(starterDataset: pd.DataFrame) -> pd.DataFrame:
        """
        Starter pipeline. Per-call RNG draws principled hyperparameters
        (lookback window, intercept choice, recency-weight half-life) so
        different nodes produce diverse but valid forecasts on identical
        data. The pilot/stable loop filters bad draws over time.
        """
        if starterDataset is None or len(starterDataset) == 0:
            return pd.DataFrame({
                "ds": [pd.Timestamp.now() + pd.Timedelta(days=1)],
                "pred": [0]})
        if len(starterDataset) == 1:
            value = starterDataset.iloc[0, 1]
            return pd.DataFrame({
                "ds": [pd.Timestamp.now() + pd.Timedelta(days=1)],
                "pred": [value]})
        rng = make_rng()
        n = len(starterDataset)
        if n <= 4:
            # Always at least the last 2 rows; small jitter into 3-4 when
            # available. Keeps results near the original mean-of-last-2
            # behavior while still differing across nodes.
            tail_k = int(rng.integers(2, n + 1)) if n >= 3 else 2
            value = starterDataset.iloc[-tail_k:, 1].mean()
            return pd.DataFrame({
                "ds": [pd.Timestamp.now() + pd.Timedelta(days=1)],
                "pred": [value]})
        # Narrow ranges since Starter has no scoring loop (compare() always
        # returns True), so every draw is visible. Diversity comes from the
        # lookback window and optional gentle recency weighting only — the
        # intercept is always fit (no-intercept forces lines through origin
        # and produces wildly biased extrapolations for non-zero series).
        # Use at least 80% of available rows (and never fewer than 5 since
        # this branch is gated on n > 4). Caps at n so rng.integers always
        # has a valid range.
        min_lookback = max(5, int(n * 0.8))
        lookback = int(rng.integers(min_lookback, n + 1))
        window = starterDataset.iloc[-lookback:]
        x = window.index.values.reshape(-1, 1)
        y = window.iloc[:, 1].values
        model = LinearRegression()
        # 60% pure OLS, 40% gentle recency weighting with a long half-life
        # so weights stay near uniform.
        if rng.random() < 0.6:
            model.fit(x, y)
        else:
            half_life = float(rng.choice([float(lookback), 2.0 * lookback]))
            ages = (x[-1, 0] - x[:, 0]).astype(float)
            weights = np.power(0.5, ages / half_life)
            model.fit(x, y, sample_weight=weights)
        next_time = starterDataset.index[-1] + 1
        predicted_value = model.predict([[next_time]])[0]
        return pd.DataFrame({
            "ds": [pd.Timestamp.now() + pd.Timedelta(days=1)],
            "pred": [predicted_value]})
