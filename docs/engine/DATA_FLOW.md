# AI Prediction Engine — Data Flow

How observation data moves through the neuron: where it comes from, how a
timestamp/value pair becomes a row in a dataframe, how predictions are made,
and how they get back out for scoring.

> Scope: this documents the *current* code on the `seer` branch. It is meant as
> the reference for the engine-fixing work, so it also calls out the points
> that look wrong or surprising (see [Open Issues](#open-issues)).

---

## TL;DR — there are two engines, not one

The neuron runs **two completely separate prediction systems** at the same
time. They share no code, no dataframe, and no model state.

| | Path A — Relay (live) | Path B — Central (batch) |
|---|---|---|
| Engine class | `LiteEngine` | `veda.Engine` / `StreamModel` |
| File | `engine-lite/satoriengine/lite/lite_engine.py` | `engine-lite/engine.py` + `adapters/` |
| Data source | Nostr relay subscriptions | `server.getObservationsBatch()` from central-lite |
| Trigger | Every observation, as it arrives | Poll loop, every 11 hours |
| Method | Simple stats (mean / linear regression) | XGBoost or Starter adapter, with training |
| Uses XGB? | **No** | **Yes**, when a stream has >10 rows |
| Model files / training | None (stateless) | Yes — joblib model files, training queue |
| Prediction out | `{stream}_pred` relay channel + bounty DMs | Batched submit back to central |

The mental model of *"XGB for >10 rows, simple predictions below that, fed by
central, submitted back as batches for scoring"* describes **Path B**. The
`LiteEngine` (Path A) is a newer, lighter path that never touches XGB.

---

## Path A — Relay path (`LiteEngine`)

### A.1 Observation arrives

`neuron-lite/start.py`

```
_networkListen(relay_url)                       # start.py:961-ish listener loop
  └─ async for obs in client.observations()     # Nostr subscription stream
       └─ _networkProcessObservation(obs)       # start.py:485
```

`_networkProcessObservation` (start.py:485):

1. Serializes the observation to JSON (`obs.observation.to_json()`).
2. `networkDB.save_observation(...)` → SQLite `observations` table.
   Returns `False` if it is a duplicate (dedup by `event_id`, or by
   `(stream_name, provider_pubkey, seq_num)`).
3. If the row is **new** and the stream `is_predicting`, calls
   `_networkRunEngine()`.

### A.2 `observations` table (SQLite)

`neuron-lite/satorineuron/network_db.py:97-107`

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK | autoincrement |
| `stream_name` | TEXT | stream identifier |
| `provider_pubkey` | TEXT | Nostr pubkey of the data provider |
| `seq_num` | INTEGER | provider's sequence number |
| `observed_at` | INTEGER | Unix epoch (seconds) — provider's timestamp |
| `received_at` | INTEGER | Unix epoch (seconds) — `int(time.time())` when *we* received it |
| `value` | TEXT | the observation, stored as a JSON string |
| `event_id` | TEXT | Nostr event id, used for dedup |

`save_observation` (network_db.py:585) sets `received_at = int(time.time())`.
There is no dataframe here — observations are individual SQLite rows.

### A.3 Prediction — `_networkRunEngine`

`start.py:961`

```python
observations = networkDB.get_observations(stream_name, provider_pubkey, limit=30)
prediction   = LiteEngine().predict(observations)
```

`get_observations` (network_db.py:613) returns the **last 30** rows ordered
`received_at DESC` — i.e. **newest-first**, as a `list[dict]` (one dict per row,
keys = column names above).

### A.4 `LiteEngine.predict()`

`engine-lite/satoriengine/lite/lite_engine.py:15`

`LiteEngine` is **stateless** — no dataframe, no model, no timestamps used.

1. Reverse the list → chronological order (oldest-first).
2. For each obs, `_extract_numeric(obs['value'])`:
   - tries `float()`,
   - then `json.loads()` — handles quoted numbers (`'"42.5"'`) and nested
     observation objects (`{"value": "0.091", ...}` → pulls `value`).
   - non-numeric → dropped.
3. Predict from the numeric list `values`:
   - `n == 1` → return that value.
   - `1 < n < 5` → mean of the last two values.
   - `n >= 5` → `LinearRegression` over `X = [0, 1, ..., n-1]`, predict at `X = n`.
4. Returns the prediction as a **string**, or `None` if nothing numeric.

> The regression uses the **observation index** as X, *not the timestamp*.
> Irregular spacing between observations is ignored.

### A.5 Persisting and publishing the prediction

Back in `_networkRunEngine` (start.py:977-1004):

- If `predict()` returned `None` (non-numeric stream) → **echo fallback**:
  re-publish the raw observation value, `method = 'echo'`.
- `networkDB.save_prediction(...)` → SQLite `predictions` table.
- Publish to the **`{stream}_pred`** channel on every connected relay
  (`_networkPublishObservation`).
- `mark_prediction_published(pred_id)`.

`predictions` table — `network_db.py:152-162`:

| Column | Meaning |
|---|---|
| `id` | PK |
| `stream_name`, `provider_pubkey` | which stream this predicts |
| `observation_seq` | `seq_num` of the observation predicted *from* |
| `value` | predicted value (TEXT) |
| `observed_at` | timestamp of the source observation |
| `created_at` | when the prediction row was written |
| `published` | 0/1 — pushed to a relay yet |

### A.6 Bounty submissions (scoring)

Also in `_networkRunEngine` (start.py:1006-1039): for every bounty host the
neuron has joined for this stream, it sends the prediction privately as an
encrypted DM via `submitPrediction()` (start.py:3751).

- `seq_num` is offset by the bounty's `horizon` (default 1 = "predict the next
  value"; 2 = "two steps ahead", etc.).
- The predictor's own wallet pubkey is attached so the host can open a payment
  channel back.
- This is *in addition to* the public `{stream}_pred` publication.

This is the relay-path equivalent of "submitting predictions for scoring".

---

## Path B — Central path (`veda.Engine` + XGB/Starter adapters)

This is the heavyweight ML path. It is the one with the >10-row XGB threshold.

### B.1 Engine startup

`start.py`

```
delayedEngine()           # start.py:4407 — sleeps 6h, then buildEngine()
  └─ buildEngine() ─ spawnEngine()
       └─ Engine.createFromNeuron(...)   # start.py:4666 → self.aiengine
```

### B.2 Polling observations from central

`pollObservationsForever` (start.py:4254) runs on a daemon thread:

- First poll after a random 5–30 min delay; then **every 11 hours**.
- `observations = server.getObservationsBatch(storage=...)` (start.py:4285).
  This batch includes Bitcoin, multi-crypto, and SafeTrade streams.

### B.3 Raw observation format from central

`getObservationsBatch` returns the JSON body of the central
`/api/v1/observations/batch` endpoint verbatim, with one addition: it copies
`stream.uuid` onto each observation as a top-level `stream_uuid`
(`satorilib/src/satorilib/server/server.py:1304`).

A batch is a JSON array — **one observation per stream**, the latest snapshot
for each. Each observation looks like:

```json
{
  "id": 201570,
  "value": "0.00083",
  "observed_at": "1779426055.9544094",
  "ts": "2026-05-22T05:00:55.955559",
  "hash": "",
  "stream_id": 66,
  "stream_uuid": "b4bf6ce9-64be-49b2-a17c-037cb4a40f9f",
  "stream": {
    "id": 66,
    "uuid": "b4bf6ce9-64be-49b2-a17c-037cb4a40f9f",
    "name": "safetrade_xtm",
    "author": null,
    "secondary": null,
    "target": null,
    "description": null,
    "meta": null
  }
}
```

| Field | Type | Notes |
|---|---|---|
| `id` | int | Central's observation row id. |
| `value` | **string** | The numeric value, JSON-encoded as a string. `float()` is applied downstream (start.py:4315). |
| `observed_at` | **string** | Unix epoch **with fractional seconds**. This is the timestamp the neuron actually uses. |
| `ts` | string | ISO-8601, no timezone. Currently *ignored* by the neuron. |
| `hash` | string | Empty (`""`) for every observation in the captured sample. |
| `stream_id` | int | Same value as `stream.id`. |
| `stream_uuid` | string | Added client-side by `getObservationsBatch`; a copy of `stream.uuid`. |
| `stream` | object | Stream metadata — `id, uuid, name, author, secondary, target, description, meta`. Only `id`/`uuid`/`name` are populated in the sample; the rest are `null`. |

Verified across the full 74-observation sample: `value` and `observed_at` are
**always strings**, `hash` is **always `""`**, there are **no nulls** in
`value`, and every observation belongs to a distinct stream (btc, eth, doge,
ada, aave, atom, avax, bnb, dot, … plus `safetrade_*` and `bitcoin`).

> Full captured sample (74 observations, fetched 2026-05-22):
> [`central_batch_sample.json`](./central_batch_sample.json)

### B.4 One observation → a one-row DataFrame

For each observation in the batch (start.py:4299-4317):

```python
df = pd.DataFrame([{
    'ts':    observation.get('observed_at') or observation.get('ts'),
    'value': float(value),
    'hash':  str(hash_val),         # hash or id
}])
```

So **each observation becomes a single-row DataFrame** with columns
`ts`, `value`, `hash`. It is then handed to the per-stream model.

A `StreamModel` is created per `stream_uuid` on first sight
(`StreamModel.createFromServer`, start.py:4350), `chooseAdapter(inplace=True)`
is called, and a training thread is started (`run_forever()`).

### B.5 Accumulating into the full DataFrame — `onDataReceived`

`engine-lite/engine.py:565` — this is where the timestamp/value pair joins the
running dataset.

```python
streamModels[stream_uuid].onDataReceived(df)
```

Inside `onDataReceived`:

1. **Persist** — store the row in per-stream SQLite (table name = the
   `streamUuid`) via `storage.storeStreamData(...)`.
2. **Normalize columns** — rename `ts → date_time`, `hash → id`; drop `provider`.
3. **Parse the timestamp** (engine.py:604-611):
   - if numeric and `> 946684800` (year 2000) → treated as a **Unix epoch**:
     `pd.to_datetime(numeric_times, unit='s', utc=True)`,
   - otherwise parsed as an ISO date string, `utc=True`.
   All timestamps end up timezone-aware UTC.
4. **Append to the in-memory frame**:
   ```python
   self.data = pd.concat([self.data, engineDf], ignore_index=True)
   self.data = self.data.drop_duplicates(subset=['date_time'], keep='last')
   ```
   `self.data` is the running dataset for the stream — columns
   **`date_time`, `value`, `id`**. Duplicates on `date_time` keep the latest.
5. **Re-pick the adapter** — `chooseAdapter(inplace=True)`. If the stream just
   crossed the 10-row line, this upgrades `StarterAdapter → XgbAdapter` and
   joins the training queue (engine.py:616-624).

### B.6 Adapter selection — the >10-row rule

`chooseAdapter` (engine.py:1133) iterates `preferredAdapters = [XgbAdapter,
StarterAdapter]` and picks the first whose `condition()` returns `1.0`.

`XgbAdapter.condition` — `adapters/xgboost/xgb.py:18`:
```python
if availableRamGigs is a float and < 0.025:   return 0      # too little RAM
if cpu == 1 or len(data) > 10:                return 1.0    # ← the rule
return 0.0
```

`StarterAdapter.condition` — `adapters/starter/starter_model.py:11`:
```python
if availableRamGigs is a float and < 0.025:   return 1.0    # fallback
if len(data) <= 10:                           return 1.0
return 0.0
```

So, given enough RAM:

| Rows in `self.data` | Adapter |
|---|---|
| `<= 10` | `StarterAdapter` (simple stats, no training) |
| `> 10` | `XgbAdapter` (XGBoost, trained) |

`StarterAdapter.starterEnginePipeline` (starter_model.py:50) mirrors
`LiteEngine`: 1 row → echo, 2–4 rows → mean of last 2, >4 rows → linear
regression on the index. (`LiteEngine`'s docstring even says it "matches
StarterAdapter's approach".)

### B.7 How XGB shapes the DataFrame

When `XgbAdapter` runs (`xgb.py`), the raw `self.data` (`date_time`, `value`,
`id`) is passed through `xgbDataPreprocess` (`adapters/xgboost/preprocess.py:10`):

1. **Timestamp → index** — `date_time` parsed (Unix-epoch detection, valid range
   2000–2100; else ISO), then `set_index("date_time")`.
2. **Sampling frequency** — the median gap between consecutive timestamps is
   measured. If timestamps are noisy (>5% distinct gap sizes) they are *rounded
   onto a regular grid* (`_processNoisyDataset`).
3. **Collapse duplicates** — `groupby(level=0).agg({"value":"mean","id":"first"})`
   averages any rows that share a timestamp.
4. **Resample to a regular grid** —
   `asfreq(sampling_frequency, method="nearest", fill_value=NaN)`. Gaps in the
   series become NaN rows.

Then `XgbAdapter._manageData` (xgb.py:170) enriches it:

- `_prepareTimeFeatures` adds `hour`, `day`, `month`, `year`, `day_of_week`
  (preprocess.py:161).
- `addPercentageChange` adds `percent{n}` columns for Fibonacci lags
  `1,2,3,5,8,13,21,34,55` (xgb.py:191).
- `clearoutInfinities` clamps `±inf` to column min/max.
- `tomorrow = value.shift(-1)` — this is the **training target** (next value).

Training (`fit`, xgb.py:124) does a non-shuffled `train_test_split` (80/20),
mutates hyperparameters, and fits an `XGBRegressor` (MAE eval metric).
`predict` (xgb.py:154) takes the **last feature row** (`dataset.iloc[[-1], :-1]`)
and predicts one step ahead at `last_index + sampling_frequency`.

XGB training runs through a shared single-worker queue
(`satoriengine/veda/training/queue_manager.py`); `StarterAdapter` skips
training entirely.

### B.8 Submitting predictions back as a batch

`collectAndSubmitPredictions` (start.py:4188), called once per poll cycle after
all observations are processed:

1. Walk every `streamModel`; if it has a `_pending_prediction`, call
   `aiengine.queuePrediction(...)` (engine.py:350).
2. `aiengine.flushPredictionQueue()` (engine.py:362) submits **all queued
   predictions in one batch** to the central server.
3. Result is logged as `successful / total_submitted`.

This batch submission to central-lite is what the scoring pipeline on the
central server consumes.

---

## Quick reference — column names per stage

| Stage | Object | Columns |
|---|---|---|
| Relay obs (SQLite) | `observations` row | `stream_name, provider_pubkey, seq_num, observed_at, received_at, value, event_id` |
| `LiteEngine.predict` input | `list[dict]` | (the obs row dicts; only `value` is read) |
| Central batch (raw) | JSON array of objects | `id, value, observed_at, ts, hash, stream_id, stream_uuid, stream{}` |
| Central obs → df | `pd.DataFrame` | `ts, value, hash` |
| Engine running data | `StreamModel.data` | `date_time, value, id` |
| XGB after preprocess | `XgbProcessedData.dataset` | `value, id` (datetime index) |
| XGB after `_manageData` | training frame | `value, hour, day, month, year, day_of_week, percent{1..55}, tomorrow` |

---

## Open Issues

Points to keep in mind while fixing the engine — these are observations from
the current code, not yet decisions:

1. **Two parallel engines.** `LiteEngine` (relay) and `veda.Engine` (central)
   run independently and both publish to `{stream}_pred`. It is unclear which
   is the intended production path, or whether they are meant to coexist.
2. **`LiteEngine` ignores time.** Its regression uses observation *index*
   (`0..n-1`), not the actual `observed_at` timestamp — irregular cadence is
   invisible to it. The XGB path, by contrast, resamples onto a real time grid.
3. **`LiteEngine` does no XGB at all.** Despite the `n >= 5` branch, the relay
   path never reaches the XGBoost adapter regardless of how much history exists.
4. **`asfreq` introduces NaN rows.** For sparse/irregular streams, XGB
   resampling fills gaps with NaN; the last feature row fed to `model.predict`
   can therefore be NaN-heavy.
5. **30-observation window.** `_networkRunEngine` only ever fetches the last 30
   observations — a hard cap on the history `LiteEngine` can see.
6. **Adapter thrash.** `chooseAdapter` re-runs on *every* `onDataReceived`; a
   stream hovering around 10 rows can flip between `StarterAdapter` and
   `XgbAdapter` repeatedly.
7. **`hash` from central is always empty.** Every observation in the captured
   batch has `hash == ""`, so `hash_val = observation.get('hash') or
   observation.get('id')` always falls back to `id`. The dataframe `id` column
   is therefore central's row id, never a content hash — dedup on it is fine,
   but anything expecting a real hash will not get one.
8. **`value` arrives as a string.** `float(value)` (start.py:4315) is applied
   unconditionally; a non-numeric stream value would raise and the observation
   would be skipped.
9. **One observation per stream per batch.** A batch is the latest snapshot
   across all streams (74 in the sample), not a time series for one stream —
   history is built up only across successive 11-hour polls.

---

## Key files

| File | Role |
|---|---|
| `neuron-lite/start.py` | Orchestration — both paths (`_networkRunEngine`, `pollObservationsForever`, `collectAndSubmitPredictions`, `submitPrediction`) |
| `neuron-lite/satorineuron/network_db.py` | SQLite store: `observations`, `predictions` tables |
| `neuron-lite/satorineuron/lite_engine.py` | Re-export shim for `LiteEngine` |
| `engine-lite/satoriengine/lite/lite_engine.py` | `LiteEngine` — relay-path prediction |
| `engine-lite/engine.py` | `veda.Engine` / `StreamModel` — central-path engine |
| `engine-lite/adapters/xgboost/xgb.py` | `XgbAdapter` — XGBoost model |
| `engine-lite/adapters/xgboost/preprocess.py` | XGB dataframe preprocessing / resampling |
| `engine-lite/adapters/starter/starter_model.py` | `StarterAdapter` — simple-stats model (≤10 rows) |
| `engine-lite/satoriengine/veda/training/queue_manager.py` | Shared single-worker training queue |
