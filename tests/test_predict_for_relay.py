"""Tests for the StreamModel relay entry point (`predictForRelay`).

Runs inside the dev container (mirrors the /Satori path style used by the other
engine tests). Builds a minimal StreamModel backed by a StarterAdapter and a fake
storage, and verifies the relay entry point plus that the central
producePrediction path is unchanged by the _ingestData/_runForecast extraction.
"""

import sys
import threading

sys.path.insert(0, '/Satori/Engine')
sys.path.insert(0, '/Satori/Lib')

import pandas as pd

from satoriengine.veda.engine import StreamModel
from satoriengine.veda.adapters import StarterAdapter


class FakeStorage:
    def __init__(self):
        self.calls = []

    def storeStreamData(self, uuid, df):
        self.calls.append((uuid, len(df)))
        return len(df)


def make_model(data, stable=True):
    m = StreamModel.__new__(StreamModel)
    m._modelLock = threading.RLock()
    m.sink = 'relay'
    m.streamUuid = 'test-relay-uuid'
    m.predictionStreamUuid = 'test-relay-pred-uuid'
    m.storage = FakeStorage()
    m.cpu = 1
    m.preferredAdapters = [StarterAdapter]
    m.defaultAdapters = [StarterAdapter]
    m.failedAdapters = []
    m.data = data
    m.adapter = StarterAdapter
    m.pilot = StarterAdapter(uid=m.streamUuid)
    m.stable = StarterAdapter(uid=m.streamUuid) if stable else None
    return m


def _frame(start_val, n, start='2024-01-01'):
    return pd.DataFrame({
        'date_time': pd.date_range(start=start, periods=n, freq='h', tz='UTC'),
        'value': [float(start_val + i) for i in range(n)],
        'id': [str(start_val + i) for i in range(n)],
    })


def test_predict_for_relay_returns_numeric_string():
    m = make_model(_frame(10, 8))
    new = pd.DataFrame({
        'date_time': [pd.Timestamp('2024-01-01 08:00', tz='UTC')],
        'value': [18.0],
        'id': ['18'],
    })
    out = m.predictForRelay(new)
    assert out is not None
    float(out)  # must parse as a number
    assert m.storage.calls  # ingest wrote to storage


def test_predict_for_relay_predicts_even_with_no_new_rows():
    # An empty incoming frame stores nothing, but the relay must still emit a
    # prediction from existing history (predict-on-every-observation).
    m = make_model(_frame(10, 8))
    empty = pd.DataFrame(columns=['date_time', 'value', 'id'])
    out = m.predictForRelay(empty)
    assert out is not None
    float(out)


def test_predict_for_relay_returns_none_when_no_model():
    m = make_model(pd.DataFrame(columns=['date_time', 'value', 'id']), stable=False)
    out = m.predictForRelay(pd.DataFrame(columns=['date_time', 'value', 'id']))
    assert out is None


def test_central_producePrediction_still_routes_to_sink():
    # The refactor must keep the central path calling passPredictionData with the
    # autoregressed forecast. Capture the call instead of hitting a server.
    m = make_model(_frame(10, 8))
    captured = {}

    def fake_pass(df, passToCentral=False):
        captured['value'] = float(df['value'].iloc[0])
        captured['passToCentral'] = passToCentral

    m.passPredictionData = fake_pass
    m.producePrediction()  # updatedModel=None -> central live path
    assert 'value' in captured
    assert captured['passToCentral'] is True
