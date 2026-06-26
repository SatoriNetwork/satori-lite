from typing import Union
import os
import numpy as np
import pandas as pd
from threading import Lock
import torch
import timesfm
from satoriengine.veda.adapters.interface import ModelAdapter, TrainingResult


# Minimum history (per stream) before TimesFM is eligible. Below this the engine
# falls back to the next preferred adapter (XGBoost, then Starter). TimesFM is a
# long-context foundation model; on short series it does not beat naive, so we
# gate it well above the universal MIN_OBSERVATIONS_FOR_TRAINED_MODEL floor.
TIMESFM_MIN_POINTS = 350


class TimesFmAdapter(ModelAdapter):
    """
    Google TimesFM 2.5 (200M, PyTorch) as a zero-shot forecasting adapter.

    Zero-shot: there is no per-stream training. The model weights are loaded
    once per process and shared across every stream (unlike Chronos, which loads
    per instance) so a node with many streams pays the ~1.5 GB resident cost only
    once. Forecasts are serialized through a class-level lock because the single
    shared model is called from multiple stream threads.
    """

    # Shared, load-once model + the locks that guard load and inference.
    _shared_model = None
    _model_init_lock = Lock()
    _inference_lock = Lock()

    @staticmethod
    def condition(*args, **kwargs) -> float:
        """
        Eligible only with enough RAM for the resident model and enough history
        for the long-context model to earn its keep. Returns exactly 1.0 (the
        engine selects on `== 1`) or 0.0.
        """
        availableRamGigs = kwargs.get('availableRamGigs')
        if isinstance(availableRamGigs, (int, float)) and availableRamGigs < 2.0:
            return 0.0
        if len(kwargs.get('data', [])) < TIMESFM_MIN_POINTS:
            return 0.0
        return 1.0

    def __init__(self, **kwargs):
        super().__init__()
        self.contextLen = 512  # max history fed to the model
        self.model = self._ensureModel()

    @classmethod
    def _ensureModel(cls):
        """Load + compile the shared model once; reuse it forever after."""
        if cls._shared_model is not None:
            return cls._shared_model
        with cls._model_init_lock:
            if cls._shared_model is not None:
                return cls._shared_model
            try:
                # Persist HF weights to the mounted models volume so the ~800 MB
                # download survives container restarts. Export it (not just a
                # local var) so huggingface_hub actually honors it.
                hfhome = os.environ.get(
                    'HF_HOME', '/Satori/Neuron/models/huggingface')
                os.environ['HF_HOME'] = hfhome
                os.makedirs(hfhome, exist_ok=True)
                torch.set_num_threads(os.cpu_count() or 2)
                torch.set_float32_matmul_precision('high')
                model = timesfm.TimesFM_2p5_200M_torch.from_pretrained(
                    'google/timesfm-2.5-200m-pytorch')
                model.compile(timesfm.ForecastConfig(
                    max_context=512,
                    max_horizon=128,
                    per_core_batch_size=1,   # low single-stream latency
                    normalize_inputs=True,   # internal RevIN, no manual scaling
                    use_continuous_quantile_head=True,
                    fix_quantile_crossing=True))
                cls._shared_model = model
            except Exception as e:
                print(f"TimesFM model initialization error: {e}")
                cls._shared_model = None
        return cls._shared_model

    def load(self, modelPath: str, *args, **kwargs) -> Union[None, "ModelAdapter"]:
        """Zero-shot: nothing to load from disk."""
        return self

    def save(self, modelpath: str, *args, **kwargs) -> bool:
        """Zero-shot: nothing to persist."""
        return True

    def fit(self, *args, **kwargs) -> TrainingResult:
        """Zero-shot: no training. Report success so the model stays usable."""
        return TrainingResult(1, self)

    def compare(self, *args, **kwargs) -> bool:
        return kwargs.get('override', True)

    def score(self, *args, **kwargs) -> float:
        return np.inf

    @staticmethod
    def _extractSeries(data: pd.DataFrame) -> np.ndarray:
        """Numeric univariate series from the engine's [date_time, value, id]
        frame, NaNs dropped, as float32."""
        if isinstance(data, pd.DataFrame):
            if 'value' in data.columns:
                series = pd.to_numeric(data['value'], errors='coerce')
            elif data.shape[1] >= 2:
                series = pd.to_numeric(data.iloc[:, 1], errors='coerce')
            else:
                series = pd.to_numeric(data.iloc[:, 0], errors='coerce')
            return series.dropna().to_numpy(dtype=np.float32)
        return np.asarray(data, dtype=np.float32).reshape(-1)

    @staticmethod
    def _wrapPrediction(data: pd.DataFrame, pred: float) -> pd.DataFrame:
        """Build the engine's expected {date_time, pred} frame, inferring the
        next timestamp from the observed cadence (mirrors ETSAdapter)."""
        try:
            if isinstance(data, pd.DataFrame) and 'date_time' in data.columns:
                times = pd.to_datetime(data['date_time'])
                last = times.iloc[-1]
                diff = times.diff().median() if len(times) >= 2 else pd.Timedelta(hours=1)
                next_ts = last + diff
            else:
                next_ts = pd.Timestamp.now() + pd.Timedelta(hours=1)
        except Exception:
            next_ts = pd.Timestamp.now() + pd.Timedelta(hours=1)
        return pd.DataFrame({'date_time': [next_ts], 'pred': [float(pred)]})

    def predict(self, data: pd.DataFrame, **kwargs) -> Union[None, pd.DataFrame]:
        series = self._extractSeries(data)
        if self.model is None or len(series) == 0:
            # graceful fallback: last value (or 0 if empty)
            last = float(series[-1]) if len(series) else 0.0
            return self._wrapPrediction(data, last)
        context = series[-self.contextLen:]
        with TimesFmAdapter._inference_lock:
            point, _ = self.model.forecast(horizon=1, inputs=[context])
        return self._wrapPrediction(data, float(point[0, 0]))
