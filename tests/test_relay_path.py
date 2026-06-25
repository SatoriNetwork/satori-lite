"""In-container functional test for the Commit 4 relay path.

Exercises _backfillRelayHistory (the JSON-envelope parsing blocker fix),
_relayPredict (get-or-create + predict via the real StreamModel), the sink tag,
and the collectAndSubmitPredictions sink guard -- by calling the real StartupDag
methods against a fake self (no full StartupDag construction).
"""

import sys
import types
import threading

sys.path.insert(0, '/Satori/Engine')
sys.path.insert(0, '/Satori/Lib')
sys.path.insert(0, '/Satori/Neuron')

import pandas as pd

from start import StartupDag
from satorilib.satori_nostr.models import DatastreamObservation


class FakeStorage:
    def __init__(self):
        self.rows = {}  # uuid -> list[(epoch, value, id)]

    def getStreamRowCount(self, uuid):
        return len(self.rows.get(uuid, []))

    def storeStreamData(self, uuid, df, provider='central'):
        existing = self.rows.setdefault(uuid, [])
        before = len(existing)
        for _, r in df.iterrows():
            existing.append((float(r['epoch']), float(r['value']), str(r['id'])))
        return len(existing) - before

    def getStreamDataForEngine(self, uuid):
        rows = sorted(self.rows.get(uuid, []), key=lambda x: x[0])
        if not rows:
            return pd.DataFrame(columns=['date_time', 'value', 'id'])
        return pd.DataFrame({
            'date_time': pd.to_datetime([r[0] for r in rows], unit='s', utc=True),
            'value': [r[1] for r in rows],
            'id': [r[2] for r in rows],
        })

    def storePrediction(self, **kw):
        return True


class FakeNetworkDB:
    def __init__(self, rows):
        self._rows = rows  # newest-first list of dict rows

    def get_observations(self, stream_name, provider_pubkey, limit=50):
        return list(self._rows[:limit])


def envelope_row(seq, ts, val):
    obs = DatastreamObservation(
        stream_name='s', timestamp=ts, value=val, seq_num=seq)
    return {'value': obs.to_json(), 'observed_at': ts,
            'received_at': ts, 'seq_num': seq}


def make_fake(rows):
    engine = types.SimpleNamespace(
        streamModels={},
        storage=FakeStorage(),
        pause=lambda *a, **k: None,
        resume=lambda *a, **k: None)
    fake = types.SimpleNamespace()
    fake._engineLock = threading.RLock()
    fake.aiengine = engine
    fake.server = None
    fake.wallet = None
    fake.networkDB = FakeNetworkDB(rows)
    fake.ensureEngine = lambda: engine
    # Bind the real (unbound) methods to the fake self.
    fake._safeEpoch = StartupDag._safeEpoch
    fake._backfillRelayHistory = lambda *a, **k: StartupDag._backfillRelayHistory(fake, *a, **k)
    fake._createRelayModel = lambda *a, **k: StartupDag._createRelayModel(fake, *a, **k)
    return fake, engine


# Base epoch ~ 2024-01-01
BASE = 1704067200


def test_backfill_parses_json_envelope_and_sorts():
    # networkDB returns newest-first; values are full observation JSON envelopes.
    rows = [envelope_row(seq=i, ts=BASE + i * 3600, val=10.0 + i)
            for i in range(6)][::-1]
    fake, engine = make_fake(rows)
    fake._backfillRelayHistory('btc', 'provider', 'uuid-1')
    stored = engine.storage.rows['uuid-1']
    # Parsed to numeric values, sorted ascending by epoch.
    assert [v for _, v, _ in stored] == [10.0, 11.0, 12.0, 13.0, 14.0, 15.0]
    assert [e for e, _, _ in stored] == sorted(e for e, _, _ in stored)


def test_backfill_skips_non_numeric_rows():
    rows = [
        envelope_row(0, BASE, 10.0),
        envelope_row(1, BASE + 3600, 'not-a-number'),
        envelope_row(2, BASE + 7200, 12.0),
    ][::-1]
    fake, engine = make_fake(rows)
    fake._backfillRelayHistory('btc', 'provider', 'uuid-2')
    stored = engine.storage.rows['uuid-2']
    assert [v for _, v, _ in stored] == [10.0, 12.0]


def test_relay_predict_creates_model_with_relay_sink_and_predicts():
    rows = [envelope_row(seq=i, ts=BASE + i * 3600, val=100.0 + i)
            for i in range(8)][::-1]
    fake, engine = make_fake(rows)
    obs = types.SimpleNamespace(timestamp=BASE + 8 * 3600, value=108.0, seq_num=8)

    out = StartupDag._relayPredict(fake, 'btc', 'provider', obs, 108.0)
    assert out is not None, "expected a forecast string"
    float(out)
    # Exactly one relay model created, tagged sink='relay'.
    assert len(engine.streamModels) == 1
    model = next(iter(engine.streamModels.values()))
    assert model.sink == 'relay'


def test_relay_predict_reuses_existing_model():
    rows = [envelope_row(seq=i, ts=BASE + i * 3600, val=100.0 + i)
            for i in range(8)][::-1]
    fake, engine = make_fake(rows)
    obs1 = types.SimpleNamespace(timestamp=BASE + 8 * 3600, value=108.0, seq_num=8)
    obs2 = types.SimpleNamespace(timestamp=BASE + 9 * 3600, value=109.0, seq_num=9)
    StartupDag._relayPredict(fake, 'btc', 'provider', obs1, 108.0)
    StartupDag._relayPredict(fake, 'btc', 'provider', obs2, 109.0)
    assert len(engine.streamModels) == 1  # reused, not recreated


def test_safe_epoch_bounds():
    assert StartupDag._safeEpoch(BASE) == float(BASE)
    assert StartupDag._safeEpoch(BASE * 1000) is None  # milliseconds -> rejected
    assert StartupDag._safeEpoch('not-a-time') is None
    assert StartupDag._safeEpoch(None) is None


def test_collect_skips_relay_models():
    # collectAndSubmitPredictions must NOT submit relay-sink predictions.
    queued = []
    central = types.SimpleNamespace(sink='central', _pending_prediction={
        'stream_uuid': 'c', 'stream_name': 'c', 'value': '1',
        't1_value': None, 'observed_at': '0', 'hash': 'h'})
    relay = types.SimpleNamespace(sink='relay', _pending_prediction={
        'stream_uuid': 'r', 'stream_name': 'r', 'value': '2',
        't1_value': None, 'observed_at': '0', 'hash': 'h'})
    engine = types.SimpleNamespace(
        streamModels={'c': central, 'r': relay},
        queuePrediction=lambda **kw: queued.append(kw['stream_uuid']),
        flushPredictionQueue=lambda: {'successful': 1, 'total_submitted': 1})
    fake = types.SimpleNamespace(aiengine=engine)
    StartupDag.collectAndSubmitPredictions(fake)
    assert queued == ['c'], f"only central should be queued, got {queued}"
    assert relay._pending_prediction is not None  # relay left untouched


if __name__ == '__main__':
    import traceback
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception:
                failures += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    print(f"\n{'ALL PASSED' if failures == 0 else f'{failures} FAILED'}")
    sys.exit(1 if failures else 0)
