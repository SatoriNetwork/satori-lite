"""XGB adapter variant with four independently-toggleable improvements.

The four toggles, isolated so the bench can measure each contribution:

  use_delta_target   Train on `value.shift(-1) - value` instead of `value.shift(-1)`.
                     At predict time, add the predicted delta to the last observed
                     value. Makes the target stationary; was the highest-impact
                     change in the diagnostic (regime C: MAE 1.00 -> 0.71 at n=200).

  adaptive_lags      Drop percent-change lags larger than n // 4. The original
                     code adds lags up to 55, leaving five lag columns entirely
                     NaN at n=10 and the first ~55 rows mostly NaN at n=100. The
                     adaptive variant uses only lags that have at least 4 data
                     points to work with.

  t_feature          Add a monotonic row-index feature `t = np.arange(n)`. The
                     original features are all cyclical (hour/day/dow) or
                     differential (percent_n) - there is no monotonic position
                     for the model to learn "this row is later" directly.

  tight_hyperparams  Use small/conservative XGBoost hyperparams when n < 100
                     (n_estimators 50-200, max_depth 3-5, learning_rate 0.05-0.15)
                     instead of the original wide ranges (100-2000 / 3-10 /
                     0.005-0.3). With 8 training rows and 2000 trees of depth
                     10, the original ranges guarantee overfitting on small data.

The class is a fresh implementation rather than a subclass of XgbAdapter
because the relevant logic in `XgbAdapter._manageData` is interleaved with
unrelated preprocessing; subclassing would mean overriding it wholesale anyway.
"""
from typing import Union
import os
import joblib
import numpy as np
import pandas as pd
import psutil
from xgboost import XGBRegressor
from sklearn.metrics import mean_absolute_error
from sklearn.model_selection import train_test_split

from satoriengine.veda.adapters._rng import make_rng
from satoriengine.veda.adapters.xgboost.preprocess import (
    xgbDataPreprocess,
    _prepareTimeFeatures,
)
from satoriengine.veda.adapters.interface import ModelAdapter, TrainingResult


class XgbImprovedAdapter(ModelAdapter):

    @staticmethod
    def condition(*args, **kwargs) -> float:
        if (
            isinstance(kwargs.get('availableRamGigs'), float)
            and kwargs.get('availableRamGigs') < .025
        ):
            return 0
        return 1.0

    def __init__(
        self,
        use_delta_target: bool = False,
        adaptive_lags: bool = False,
        t_feature: bool = False,
        tight_hyperparams: bool = False,
        **kwargs,
    ):
        super().__init__()
        self.use_delta_target = use_delta_target
        self.adaptive_lags = adaptive_lags
        self.t_feature = t_feature
        self.tight_hyperparams = tight_hyperparams

        self.model: XGBRegressor = None
        self.modelError: float = None
        self.hyperparameters: Union[dict, None] = None
        self.dataset: pd.DataFrame = None
        self.trainX: pd.DataFrame = None
        self.testX: pd.DataFrame = None
        self.trainY: np.ndarray = None
        self.testY: np.ndarray = None
        self.split: float = None
        self.rng = make_rng()
        # Last observed level — needed at predict time to invert the delta
        # target. Set every time _manageData runs.
        self._lastObservedValue: Union[float, None] = None

    # ------------------------------------------------------------------
    # Standard ModelAdapter API
    # ------------------------------------------------------------------

    def load(self, modelPath: str, **kwargs):
        try:
            saved = joblib.load(modelPath)
            self.model = saved['stableModel']
            self.modelError = saved['modelError']
            return self.model
        except Exception:
            return None

    def save(self, modelpath: str, **kwargs) -> bool:
        try:
            os.makedirs(os.path.dirname(modelpath), exist_ok=True)
            self.modelError = self.score()
            joblib.dump(
                {'stableModel': self.model, 'modelError': self.modelError},
                modelpath,
            )
            return True
        except Exception:
            return False

    def compare(self, other=None, **kwargs) -> bool:
        if not isinstance(other, self.__class__):
            return True
        thisScore = self.score()
        try:
            otherScore = other.score(test_x=self.testX, test_y=self.testY)
        except Exception:
            otherScore = 0.0
        return thisScore < otherScore

    def score(self, test_x=None, test_y=None, **kwargs) -> float:
        if self.model is None:
            return np.inf
        self.modelError = mean_absolute_error(
            test_y if test_y is not None else self.testY,
            self.model.predict(test_x if test_x is not None else self.testX),
        )
        return self.modelError

    def fit(self, data: pd.DataFrame, **kwargs) -> TrainingResult:
        self._manageData(data)
        x = self.dataset.iloc[:-1, :-1]
        y = self.dataset.iloc[:-1, -1]
        # Drop rows where the target is NaN (happens when delta target is
        # used and `value.diff()` produces NaN at the head, or when long-lag
        # percent features are still NaN even after adaptive truncation).
        valid = y.notna()
        x = x.loc[valid]
        y = y.loc[valid]
        if len(x) < 4:
            # Not enough training rows after dropping NaN targets. Bail
            # out with a model that predicts the last observed value.
            self.model = None
            return TrainingResult(0, self)

        n_total = len(self.dataset)
        self.trainX, self.testX, self.trainY, self.testY = train_test_split(
            x, y,
            test_size=self.split or 0.2,
            shuffle=False,
            random_state=37,
        )
        self.trainX = self.trainX.reset_index(drop=True)
        self.testX = self.testX.reset_index(drop=True)

        self.hyperparameters = self._mutateParams_instance(
            prevParams=self.hyperparameters,
            rng=self.rng,
            n_rows=n_total,
        )
        if self.model is None:
            self.model = XGBRegressor(**self.hyperparameters)
        else:
            self.model.set_params(**self.hyperparameters)
        self.model.fit(
            self.trainX,
            self.trainY,
            eval_set=[(self.trainX, self.trainY), (self.testX, self.testY)],
            verbose=False,
        )
        return TrainingResult(1, self)

    def predict(self, data: pd.DataFrame, **kwargs):
        if self.model is None:
            # No trained model — fall back to last-observed level.
            self._manageData(data)
            if self.dataset is None or self._lastObservedValue is None:
                return None
            futureDates = self._futureDates(1)
            return pd.DataFrame({
                'date_time': futureDates,
                'pred': [self._lastObservedValue],
            })

        self._manageData(data)
        if self.dataset is None:
            return None
        featureSet = self.dataset.iloc[[-1], :-1]
        raw_prediction = float(self.model.predict(featureSet)[0])

        # Invert the delta target back to a level prediction.
        if self.use_delta_target and self._lastObservedValue is not None:
            prediction = self._lastObservedValue + raw_prediction
        else:
            prediction = raw_prediction

        futureDates = self._futureDates(1)
        return pd.DataFrame({'date_time': futureDates, 'pred': [prediction]})

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _futureDates(self, periods: int) -> pd.DatetimeIndex:
        sf = self._samplingFrequency or '1h'
        return pd.date_range(
            start=pd.Timestamp(self.dataset.index[-1]) + pd.Timedelta(sf),
            periods=periods,
            freq=sf,
        )

    def _manageData(self, data: pd.DataFrame) -> pd.DataFrame:
        """Build the feature matrix. Same shape as XgbAdapter._manageData but
        with our four toggles layered in."""

        procData = xgbDataPreprocess(data)
        df = procData.dataset.copy()
        if 'id' in df.columns:
            df = df.drop(columns=['id'])

        self._samplingFrequency = procData.sampling_frequency
        # Remember the last *observed* level before we transform `value`.
        # This is what we add the predicted delta to at predict time.
        if 'value' in df.columns and len(df) > 0:
            try:
                self._lastObservedValue = float(df['value'].iloc[-1])
            except Exception:
                self._lastObservedValue = None

        # Calendar features (always on — they're cheap and harmless).
        df = _prepareTimeFeatures(df)

        # Toggle: t feature (monotonic position).
        if self.t_feature:
            df['t'] = np.arange(len(df), dtype=float)

        # Toggle: percent-change lags, adaptive or full.
        df = self._addPercentageChange(df, adaptive=self.adaptive_lags)
        df = self._clearoutInfinities(df)

        # Target: either next level or next delta.
        if self.use_delta_target:
            df['tomorrow'] = df['value'].shift(-1) - df['value']
        else:
            df['tomorrow'] = df['value'].shift(-1)

        self.dataset = df
        return df

    @staticmethod
    def _addPercentageChange(df: pd.DataFrame, adaptive: bool) -> pd.DataFrame:
        all_lags = [1, 2, 3, 5, 8, 13, 21, 34, 55]
        if adaptive:
            n = len(df)
            # Each lag-k feature needs at least k+1 rows to be defined; we
            # also want at least ~4 non-NaN values for the feature to carry
            # any signal. So cap at n//4 (i.e. keep lag k only if k <= n/4).
            lags = [k for k in all_lags if k <= max(1, n // 4)]
            if not lags:
                lags = [1]
        else:
            lags = all_lags
        for past in lags:
            df[f'percent{past}'] = (
                (df['value'] - df['value'].shift(past))
                / df['value'].shift(past)
            ) * 100
        return df

    @staticmethod
    def _clearoutInfinities(df: pd.DataFrame) -> pd.DataFrame:
        for col in df.columns:
            if df[col].dtype.kind in 'bifc':
                mask = ~df[col].isin([np.inf, -np.inf])
                if mask.any():
                    mx = df[col][mask].max()
                    mn = df[col][mask].min()
                    df[col] = df[col].replace(np.inf, mx)
                    df[col] = df[col].replace(-np.inf, mn)
        return df

    # ------------------------------------------------------------------
    # Hyperparameter handling
    # ------------------------------------------------------------------

    @staticmethod
    def paramBounds(tight: bool = False) -> dict:
        if tight:
            return {
                'n_estimators': (50, 200),
                'max_depth': (3, 5),
                'learning_rate': (0.05, 0.15),
                'subsample': (0.7, 1.0),
                'colsample_bytree': (0.7, 1.0),
                'min_child_weight': (1, 5),
                'gamma': (0, 0.5),
                'scale_pos_weight': (0.8, 1.5),
            }
        # Original ranges
        return {
            'n_estimators': (100, 2000),
            'max_depth': (3, 10),
            'learning_rate': (0.005, 0.3),
            'subsample': (0.6, 1.0),
            'colsample_bytree': (0.6, 1.0),
            'min_child_weight': (1, 10),
            'gamma': (0, 1),
            'scale_pos_weight': (0.5, 10),
        }

    @classmethod
    def _prepParams(cls, rng=None, tight: bool = False) -> dict:
        rng = rng or np.random.default_rng(37)
        bounds = cls.paramBounds(tight=tight)
        cpu = psutil.cpu_count(logical=True) or -1
        return {
            'random_state': int(rng.integers(0, 10000)),
            'eval_metric': 'mae',
            'n_jobs': cpu,
            'tree_method': 'hist',
            'learning_rate': float(rng.uniform(*bounds['learning_rate'])),
            'subsample': float(rng.uniform(*bounds['subsample'])),
            'colsample_bytree': float(rng.uniform(*bounds['colsample_bytree'])),
            'gamma': float(rng.uniform(*bounds['gamma'])),
            'n_estimators': int(rng.integers(*bounds['n_estimators'])),
            'max_depth': int(rng.integers(*bounds['max_depth'])),
            'min_child_weight': int(rng.integers(*bounds['min_child_weight'])),
            'scale_pos_weight': float(rng.uniform(*bounds['scale_pos_weight'])),
        }

    def _mutateParams_instance(self, prevParams, rng, n_rows: int):
        """Instance variant that consults `self.tight_hyperparams`. Wraps the
        classmethod with the n_rows-vs-tight logic applied."""
        tight = self.tight_hyperparams and (n_rows is not None and n_rows < 100)
        rng = rng or np.random.default_rng(37)
        prevParams = prevParams or self._prepParams(rng, tight=tight)
        bounds = self.paramBounds(tight=tight)
        out = {}
        for param, (lo, hi) in bounds.items():
            cur = prevParams.get(param, lo)
            span = hi - lo
            tweak = rng.normal(0, span * 0.1)
            val = max(lo, min(hi, cur + tweak))
            if param in ('n_estimators', 'max_depth', 'min_child_weight'):
                val = int(round(val))
            out[param] = val
        out['random_state'] = prevParams.get('random_state', int(rng.integers(0, 10000)))
        out['eval_metric'] = 'mae'
        if 'n_jobs' in prevParams:
            out['n_jobs'] = prevParams['n_jobs']
        if 'tree_method' in prevParams:
            out['tree_method'] = prevParams['tree_method']
        return out
