"""
Shared per-fit RNG for adapters.

Seeded from wall-clock microseconds so that different nodes (and successive
training ticks on the same node) draw different hyperparameters. The
pilot/stable scoring loop in StreamModel keeps the good draws and discards
the bad ones, so this gives ensemble exploration without random noise in
predictions.
"""
import datetime
import numpy as np


def make_rng() -> np.random.Generator:
    return np.random.default_rng(datetime.datetime.now().microsecond // 100)
