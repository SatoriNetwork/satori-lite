from typing import Union
import os
import time
import numpy as np
import pandas as pd
import random
import torch
from threading import Lock
from chronos import ChronosPipeline
from satoriengine.veda.adapters.interface import ModelAdapter, TrainingResult


class PretrainedChronosAdapter(ModelAdapter):

    # Class-level lock
    _model_init_lock = Lock()

    @staticmethod
    def condition(*args, **kwargs) -> float:
        ''' don't use this, it doesn't learn '''
        return 0.0


    def __init__(self, useGPU: bool = False, **kwargs):
        super().__init__()
        #PretrainedChronosAdapter.set_seed(37) # does not make it deterministic
        hfhome = os.environ.get(
            'HF_HOME', default='/Satori/Neuron/models/huggingface')
        os.makedirs(hfhome, exist_ok=True)
        deviceMap = 'cuda' if useGPU else 'cpu'
        try:
            with PretrainedChronosAdapter._model_init_lock:
                self.model = ChronosPipeline.from_pretrained(
                    "amazon/chronos-t5-large" if useGPU else "amazon/chronos-t5-small",
                    # "amazon/chronos-t5-tiny", # 8M
                    # "amazon/chronos-t5-mini", # 20M
                    # "amazon/chronos-t5-small", # 46M
                    # "amazon/chronos-t5-base", # 200M
                    # "amazon/chronos-t5-large", # 710M
                    # 'cpu' for any CPU, 'cuda' for Nvidia GPU, 'mps' for Apple Silicon
                    device_map=deviceMap,
                    torch_dtype=torch.bfloat16,
                    # force_download=True,
                )
            self.contextLen = 512  # historical context
        except Exception as e:
            print(f"Chronos model initialization error: {e}")
            self.contextLen = 512
            self.model = None
        #self.model.model.eval() # does not make it deterministic

    @staticmethod
    def set_seed(seed: int):
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)

    def fit(self, **kwargs) -> TrainingResult:
        ''' online learning '''
        return TrainingResult(1, self)

    def predict(self, data: pd.DataFrame, **kwargs) -> np.ndarray:
        data = data.values  # Convert DataFrame to numpy array
        if self.model is None:
            return np.asarray(data[-1], dtype=np.float32)
        # Chronos expects a 1D tensor for a single series. Flatten to guarantee
        # shape (T,) regardless of whether caller passed a Series, (T,), or (T,1).
        data = np.asarray(data, dtype=np.float32).reshape(-1)
        data = data[-self.contextLen:]
        context = torch.tensor(data)
        #t1_start = time.perf_counter_ns()
        forecast = self.model.predict(
            context,
            1,  # prediction_length
            num_samples=4,  # 20
            temperature=1.0,  # 1.0
            top_k=64,  # 50
            top_p=1.0,  # 1.0
        )  # forecast shape: [num_series, num_samples, prediction_length]
        # low, median, high = np.quantile(forecast[0].numpy(), [0.1, 0.5, 0.9], axis=0)
        median = forecast.median(dim=1).values
        predictions = median[0]
        #total_time = (time.perf_counter_ns() - t1_start) / 1e9  # seconds
        #print(
        #    f"Chronos prediction time seconds: {total_time}    "
        #    f"Historical context size: {data.shape}    "
        #    f"Predictions: {predictions}")
        return np.asarray(predictions, dtype=np.float32)

    def compare(self, other: Union[ModelAdapter, None] = None, **kwargs) -> bool:
        """
        Compare other (model) and this models based on their backtest error.
        Returns True if this model performs better than other model.
        """
        return kwargs.get('override', True)

    def score(self, **kwargs) -> float:
        """will score the model"""
        return np.inf
