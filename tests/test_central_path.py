"""The central path through the engine, on real SQLite: the neuron's Engine
from its streams and alias, a StreamModel that starts from the history stored,
each observation stored once, and a new observation leaving a prediction both
in local storage and in _pending_prediction, which the neuron's
collectAndSubmitPredictions batches to Central. Frames are what start.py passes
the engine: date_time (datetime64 UTC), value, id. Few rows, so the starter
adapter predicts and no training thread starts.
"""

import pandas as pd
import pytest

from satorilib.concepts.structs import Stream, StreamId
from satoriengine.veda.engine import Engine, StreamModel
from satoriengine.veda.storage import EngineStorageManager

BASE = 1704067200  # 2024-01-01 UTC
HOUR = 3600


def frame(epochs, values):
    return pd.DataFrame({
        'date_time': pd.to_datetime(epochs, unit='s', utc=True),
        'value': values,
        'id': [f'h{e}' for e in epochs],
    })


@pytest.fixture
def streams():
    sub = StreamId(source='central-lite', author='satori', stream='btc', target='')
    pub = StreamId(source='central-lite', author='satori', stream='btc_pred', target='')
    return Stream(streamId=sub), Stream(streamId=pub, predicting=sub)


@pytest.fixture
def storage(tmp_path, monkeypatch):
    s = EngineStorageManager(data_dir=str(tmp_path), dbname='engine.db')
    monkeypatch.setattr(EngineStorageManager, '_instance', s)  # what Engine() takes
    yield s
    s.close()
    s.stream_store.close()


def model(streams, storage):
    sub, pub = streams
    return StreamModel.createFromServer(
        streamUuid=sub.streamId.uuid,
        predictionStreamUuid=pub.streamId.uuid,
        server=object(),
        alias=object(),
        subscriptionStream=sub,
        publicationStream=pub,
        pauseAll=lambda: None,
        resumeAll=lambda: None,
        storage=storage)


def test_engine_from_neuron_holds_its_streams_and_alias(streams, storage):
    sub, pub = streams
    server, alias = object(), object()
    engine = Engine.createFromNeuron(
        subscriptions=[sub], publications=[pub], server=server, alias=alias)
    assert engine.server is server and engine.alias is alias
    assert engine.subscriptionStreams == [sub] and engine.publicationStreams == [pub]
    assert engine.storage is storage


def test_model_starts_from_the_stored_history(streams, storage):
    storage.storeStreamData(streams[0].streamId.uuid, pd.DataFrame(
        {'epoch': [BASE, BASE + HOUR], 'value': [1.0, 2.0], 'id': ['a', 'b']}))
    m = model(streams, storage)
    assert m.sink == 'central' and m.storage is storage
    assert list(m.data['value']) == [1.0, 2.0]


def test_each_observation_is_stored_once(streams, storage):
    m = model(streams, storage)
    first = frame([BASE, BASE + HOUR, BASE + 2 * HOUR], [100.0, 101.5, 102.3])
    m.onDataReceived(first)
    m.onDataReceived(first)
    m.onDataReceived(frame([BASE + 3 * HOUR], [103.0]))
    stored = storage.getStreamDataForEngine(streams[0].streamId.uuid)
    assert list(stored.columns) == ['date_time', 'value', 'id']
    assert list(stored['date_time']) == list(
        pd.to_datetime([BASE + i * HOUR for i in range(4)], unit='s', utc=True))
    assert list(stored['value']) == [100.0, 101.5, 102.3, 103.0]
    assert list(stored['id']) == [f'h{BASE + i * HOUR}' for i in range(4)]
    assert list(m.data['value']) == [100.0, 101.5, 102.3, 103.0]


def test_a_new_observation_leaves_a_prediction_for_the_batch(streams, storage):
    sub, pub = streams
    m = model(streams, storage)
    observed = frame([BASE, BASE + HOUR, BASE + 2 * HOUR], [100.0, 101.0, 102.0])
    m.onDataReceived(observed)
    pending = m._pending_prediction
    assert pending['stream_uuid'] == sub.streamId.uuid
    assert pending['stream_name'] == 'btc'
    assert pending['t1_value'] is not None
    value = float(pending['value'])
    assert 100.0 <= value <= 102.0  # the starter predicts from the stream's values
    assert list(storage.getPredictions(pub.streamId.uuid)['value']) == [value]

    del m._pending_prediction  # collected, as collectAndSubmitPredictions does
    m.onDataReceived(observed)  # nothing new: no prediction
    assert not hasattr(m, '_pending_prediction')
    assert len(storage.getPredictions(pub.streamId.uuid)) == 1
