"""Engine testing ground — efficient central-path reference pipeline.

Models the WHOLE central prediction flow, each stage done the efficient way:

    ingest -> normalize -> persist -> load history -> engine -> batch output

It runs the adapters directly — no neuron, no relay, no web UI, no server
connection, no training threads. The modules it wires together
(``satoriengine.data_helper``, ``satoriengine.stream_store``) are written
port-ready: the proven design moves into the real neuron/engine/storage later.

Run (one-off container, same dev image, neuron never starts):

    ./playground                       # uses the bundled sample
    ./playground <path-to-batch.json>  # a different captured batch
"""

from __future__ import annotations

import os
import sys
import json

import numpy as np
import pandas as pd

from satoriengine.data_helper import (
    normalize_central_observation,
    batch_to_stream_frames,
)
from satoriengine.stream_store import StreamStore

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE = os.path.join(HERE, 'central_batch_sample.json')
# Dedicated playground database — NEVER the real engine-lite/db/engine.db.
PLAYGROUND_DB = os.path.join(HERE, 'playground.db')

SYNTHETIC_UUID = 'synthetic-0000-0000-0000-000000000000'


def rule(title: str) -> None:
    print('\n' + '=' * 70)
    print(title)
    print('=' * 70)


def fresh_store() -> StreamStore:
    """Recreate the playground db from scratch so every run is deterministic."""
    for suffix in ('', '-wal', '-shm', '-journal'):
        path = PLAYGROUND_DB + suffix
        if os.path.exists(path):
            os.remove(path)
    return StreamStore(PLAYGROUND_DB)


def load_batch(path: str) -> list[dict]:
    with open(path) as f:
        return json.load(f)


def synthetic_batches(stream_uuid: str, n: int = 40) -> list[list[dict]]:
    """A sequence of single-observation batches for one stream.

    Models the real world: each 11h poll delivers one new observation per
    stream. Accumulating them is the only way a stream crosses the >10-row
    threshold and reaches the XGB path (a single real batch is 1 row/stream).
    """
    rng = np.random.default_rng(42)
    start = pd.Timestamp('2026-05-01', tz='UTC')
    batches = []
    for i in range(n):
        epoch = (start + pd.Timedelta(hours=i)).timestamp()
        value = 100 + 0.5 * i + 5 * np.sin(i / 6) + rng.normal(0, 0.5)
        batches.append([{
            'value': str(value),
            'observed_at': str(epoch),
            'hash': '',
            'id': 900000 + i,
            'stream_uuid': stream_uuid,
            'stream': {'uuid': stream_uuid, 'name': 'synthetic_stream'},
        }])
    return batches


def ingest(store: StreamStore, batches: list[list[dict]]) -> set[str]:
    """ingest -> normalize -> persist, for a sequence of raw Central batches."""
    total_obs = sum(len(b) for b in batches)
    inserted = 0
    streams: set[str] = set()
    for batch in batches:
        frames = batch_to_stream_frames(batch)  # normalize + group per stream
        for stream_uuid, frame in frames.items():
            inserted += store.append(stream_uuid, frame)  # persist
            streams.add(stream_uuid)
    print(f'  {len(batches)} batch(es), {total_obs} observation(s) '
          f'-> {inserted} new row(s) across {len(streams)} stream(s)')
    return streams


def pick_adapter(history: pd.DataFrame, adapters: list):
    """Mimic StreamModel.chooseAdapter: first adapter whose condition fires
    (the >10-row rule — XgbAdapter for >10 rows, StarterAdapter otherwise)."""
    for adapter_cls in adapters:
        if adapter_cls.condition(data=history, cpu=0, availableRamGigs=8.0) == 1:
            return adapter_cls
    return adapters[-1]


def run_engine(store: StreamStore, stream_uuid: str, adapters: list) -> dict | None:
    """load history -> adapter pick -> fit/predict, for one stream."""
    history = store.history(stream_uuid)          # single epoch->datetime conv
    chosen = pick_adapter(history, adapters)
    print(f'  stream {stream_uuid[:8]}  history={len(history)} row(s)  '
          f'->  {chosen.__name__}')
    try:
        adapter = chosen()
        if chosen.__name__ == 'XgbAdapter':
            adapter.fit(history)                  # XGB must train before predict
        result = adapter.predict(history)
        if result is not None and 'pred' in result.columns:
            value = float(result['pred'].iloc[0])
            print(f'  predicted next value: {value}')
            return {'stream_uuid': stream_uuid,
                    'adapter': chosen.__name__,
                    'value': value}
        print('  prediction: None')
    except Exception as e:
        import traceback
        print(f'  {chosen.__name__} failed: {e}')
        traceback.print_exc()
    return None


def main() -> None:
    batch_path = sys.argv[1] if len(sys.argv) > 1 else SAMPLE

    rule('Engine testing ground — efficient central-path pipeline')
    print(f'batch file   : {batch_path}')
    print(f'playground db: {PLAYGROUND_DB}  (fresh each run)')

    store = fresh_store()
    batch = load_batch(batch_path)

    rule('1. Ingest — raw observation[0] from Central')
    print(json.dumps(batch[0], indent=2, sort_keys=True))

    rule('2. Normalize — data_helper.normalize_central_observation')
    print(json.dumps(
        {k: str(v) for k, v in normalize_central_observation(batch[0]).items()},
        indent=2, sort_keys=True))

    rule('3. Persist — normalize + group + StreamStore.append (real batch)')
    real_streams = ingest(store, [batch])

    # Adapters imported lazily so steps 1-3 still run if xgboost is missing.
    from satoriengine.veda.adapters import XgbAdapter, StarterAdapter
    adapters = [XgbAdapter, StarterAdapter]

    predictions: list[dict] = []

    rule('4. Engine — real stream (1 observation -> StarterAdapter)')
    p = run_engine(store, sorted(real_streams)[0], adapters)
    if p:
        predictions.append(p)

    rule('5. Persist — 40 synthetic single-obs batches (one stream)')
    print('  (models 40 successive polls accumulating into one stream)')
    ingest(store, synthetic_batches(SYNTHETIC_UUID))
    print(f'  StreamStore.row_count = {store.row_count(SYNTHETIC_UUID)}')

    rule('6. Engine — synthetic stream (accumulated history -> XgbAdapter)')
    p = run_engine(store, SYNTHETIC_UUID, adapters)
    if p:
        predictions.append(p)

    rule('7. Output — prediction batch ready for submission')
    print(f'{len(predictions)} prediction(s) collected (no network call):')
    print(json.dumps(predictions, indent=2))

    store.close()
    rule('done')


if __name__ == '__main__':
    main()
