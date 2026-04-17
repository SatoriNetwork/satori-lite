from satoriengine.veda.adapters.interface import ModelAdapter, TrainingResult
# from satoriengine.veda.adapters.sktime import SKAdapter
from satoriengine.veda.adapters.starter import StarterAdapter
from satoriengine.veda.adapters.xgboost import XgbAdapter

# ETSAdapter wraps statsmodels ExponentialSmoothing - no extra deps
try:
    from satoriengine.veda.adapters.ets import ETSAdapter
except ImportError:
    ETSAdapter = None

# XgbChronosAdapter requires torch - make it optional
try:
    from satoriengine.veda.adapters.xgbchronos import XgbChronosAdapter
except ImportError:
    XgbChronosAdapter = None

# from satoriengine.veda.adapters.tinytimemixer import SimpleTTMAdapter
