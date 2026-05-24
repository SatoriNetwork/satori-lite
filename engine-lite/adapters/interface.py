import pandas as pd
from typing import Union, Optional, Any
import joblib
import os
from satorilib.logging import error, debug, info


class TrainingResult:

    def __init__(self, status, model: "ModelAdapter"):
        self.status = status
        self.model = model
        self.modelError = None

class ModelAdapter:

    def __init__(self, *args, **kwargs):
        self.model = None
        self.modelError: float = None

    @staticmethod
    def condition(*args, **kwargs) -> float:
        """
        Defines the condition for the adapter to be executed.

        Args:
            accepts information about the environment (hardware specs, etc.)
            and data (length, entropy, etc.)

        Returns:
            float in [0, 1]. 0 = do not use this model under these conditions;
            1 = these conditions are ideal for this model.

        Guidelines for new adapters:
            - DO check adapter-specific requirements: RAM/CPU/GPU, optional
              dependencies (e.g. torch), data shape (univariate, sampling
              regularity), etc.
            - DO NOT gate on minimum data length. The engine enforces a
              universal floor via MIN_OBSERVATIONS_FOR_TRAINED_MODEL in
              engine.py (chooseAdapter forces StarterAdapter below it).
              Adapters can assume `len(data) >= MIN_OBSERVATIONS_FOR_TRAINED_MODEL`
              when their condition() is invoked.
            - Returning 1 unconditionally is fine if no adapter-specific
              concern applies — the central guard plus user preference order
              will handle the rest.
        """
        if (
            isinstance(kwargs.get('availableRamGigs'), float)
            and kwargs.get('availableRamGigs') < .1
        ):
            return 0
        return 1

    def load(self, modelPath: str, *args, **kwargs) -> Union[None, "ModelAdapter"]:
        """
        loads the model model from disk if present

        Args:
            modelpath: Path where the model should be loaded from

        Returns:
            ModelAdapter: Model if load successful, None otherwise
        """
        pass

    def save(self, modelpath: str, *args, **kwargs) -> bool:
        """
        Save the model to disk.

        Args:
            model: The model to save
            modelpath: Path where the model should be saved

        Returns:
            bool: True if save successful, False otherwise
        """
        pass

    def fit(self, *args, **kwargs) -> TrainingResult:
        """
        Train a new model.

        Args:
            **kwargs: Keyword arguments including datapath and stable model

        Returns:
            TrainingResult: Object containing training status and model
        """
        pass

    def compare(self, *args, **kwargs) -> bool:
        """
        Compare other (model) and pilot models based on their backtest error.
        Args:
            other: The model to compare against, typically the "stable" model
        Returns:
            bool: True if pilot should replace other, False otherwise
            this should return a comparison object which has a bool expression
        """
        pass

    def score(self, *args, **kwargs) -> float:
        """
        will score the model.
        """
        pass

    def predict(self, *args, **kwargs) -> Union[None, pd.DataFrame]:
        """
        Make predictions using the stable model

        Args:
            **kwargs: Keyword arguments including datapath and stable model

        Returns:
            Optional[pd.DataFrame]: Predictions if successful, None otherwise
        """
        pass
