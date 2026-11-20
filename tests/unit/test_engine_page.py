"""The Local Predictions page reads what the engine stores: observations go
through storeStreamData into the StreamStore (engine.py, start.py), predictions
through storePrediction into the prediction stream's table. /api/engine/performance
charts both, /api/engine/streams lists the prediction tables no model holds now and
the last observation's time. Real SQLite under tmp_path, the real routes."""
import time
from types import SimpleNamespace
from unittest import mock

import pandas as pd
import pytest

from satoriengine.veda.storage import EngineStorageManager

OBS = '0b5e1c6a-0000-5000-8000-000000000001'
PRED = '0b5e1c6a-0000-5000-8000-000000000002'
OLD_PRED = '0b5e1c6a-0000-5000-8000-000000000003'  # a stream no model predicts now
HOUR = 3600
NOW = float(int(time.time()) // HOUR * HOUR)
OBSERVED = [(NOW - 3 * HOUR, 100.0), (NOW - 2 * HOUR, 110.0), (NOW - HOUR, 120.0)]


@pytest.fixture
def storage(tmp_path):
    s = EngineStorageManager(data_dir=str(tmp_path), dbname='engine.db')
    # as the engine stores them (engine.py: storeStreamData, passPredictionData)
    s.storeStreamData(OBS, pd.DataFrame({
        'epoch': [e for e, _ in OBSERVED],
        'value': [v for _, v in OBSERVED],
        'id': [f'h{int(e)}' for e, _ in OBSERVED]}))
    s.storePrediction(PRED, str(NOW - 2.5 * HOUR), 108.0, 'p1', 'engine')
    s.storePrediction(OLD_PRED, str(NOW - 5 * HOUR), 90.0, 'p2', 'engine')
    yield s
    s.close()
    s.stream_store.close()


@pytest.fixture
def client(storage):
    from web.app import create_app
    import web.routes as routes
    model = SimpleNamespace(
        predictionStreamUuid=PRED, storage=storage, adapter=None,
        subscriptionStream=SimpleNamespace(streamId=SimpleNamespace(
            stream='btc', source='central-lite', author='satori', target='')))
    startup = SimpleNamespace(aiengine=SimpleNamespace(streamModels={OBS: model}, storage=storage))
    app = create_app(testing=True)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['wallet_open'] = True
        sess['session_id'] = 'sid'
    with mock.patch.dict(routes._session_wallets, {'sid': mock.MagicMock()}), \
            mock.patch.object(routes, 'get_startup', return_value=startup):
        yield client


def test_the_chart_has_the_observations_the_engine_stored(client):
    r = client.get(f'/api/engine/performance?stream_uuid={PRED}')
    assert r.status_code == 200
    data = r.get_json()
    assert [(float(o['ts']), o['value']) for o in data['observations']] == OBSERVED
    assert [p['value'] for p in data['predictions']] == [108.0]
    # the prediction made at -2.5h is scored against the observation after it, -2h's
    assert [(a['predicted'], a['actual']) for a in data['accuracy']] == [(108.0, 110.0)]


def test_the_streams_give_the_last_observation_time(client):
    data = client.get('/api/engine/streams').get_json()
    assert data['last_observation_ts'] == pd.Timestamp(NOW - HOUR, unit='s', tz='UTC').isoformat()


def test_the_streams_list_a_prediction_table_no_model_holds(client):
    data = client.get('/api/engine/streams').get_json()
    assert [(s['stream_uuid'], s['stream_type']) for s in data['streams']] == [
        (PRED, 'active_model'), (OLD_PRED, 'stored_prediction')]
    assert data['predictions_24h_total'] == 2
