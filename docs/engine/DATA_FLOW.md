# AI Prediction Engine — Data Flow

How observation data moves through the neuron: where it comes from, how a
timestamp/value pair becomes a row in a dataframe, how predictions are made,
and how they get back out for scoring.

> Scope: this documents the code on the `seer` branch after the efficient
> central-path port (2026-05-23). It is the reference for ongoing engine work.

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
_networkListen(relay_url)
  └─ async for obs in client.observations()     # Nostr subscription stream
       └─ _networkProcessObservation(obs)
```

`_networkProcessObservation`:

1. Serializes the observation to JSON (`obs.observation.to_json()`).
2. `networkDB.save_observation(...)` → SQLite `observations` table.
   Returns `False` if duplicate (dedup by `event_id` or `(stream_name, provider_pubkey, seq_num)`).
3. If the row is **new** and the stream `is_predicting`, calls `_networkRunEngine()`.

### A.2 `observations` table (SQLite)

`neuron-lite/satorineuron/network_db.py`

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK | autoincrement |
| `stream_name` | TEXT | stream identifier |
| `provider_pubkey` | TEXT | Nostr pubkey of the data provider |
| `seq_num` | INTEGER | provider's sequence number |
| `observed_at` | INTEGER | Unix epoch (seconds) — provider's timestamp |
| `received_at` | INTEGER | Unix epoch (seconds) — when we received it |
| `value` | TEXT | the observation, as a JSON string |
| `event_id` | TEXT | Nostr event id, used for dedup |

### A.3 Prediction — `_networkRunEngine`

```python
observations = networkDB.get_observations(stream_name, provider_pubkey, limit=30)
prediction   = LiteEngine().predict(observations)
```

`get_observations` returns the **last 30** rows ordered `received_at DESC`
(newest-first), as a `list[dict]`.

### A.4 `LiteEngine.predict()`

`engine-lite/satoriengine/lite/lite_engine.py`

Stateless — no dataframe, no model, timestamps not used.

1. Reverse the list → chronological order.
2. Extract numeric values: `float()`, then `json.loads()` fallback.
3. Predict: 1 row → echo; 2–4 rows → mean of last 2; ≥5 rows → `LinearRegression`
   over `X = [0, 1, ..., n-1]`, predict at `X = n`.

### A.5 Persisting and publishing

- `networkDB.save_prediction(...)` → SQLite `predictions` table.
- Publish to `{stream}_pred` channel on every connected relay.
- For each bounty host: send prediction as encrypted DM via `submitPrediction()`.

---

## Path B — Central path (`veda.Engine` + XGB/Starter adapters)

### B.1 Engine startup

```
delayedEngine()        # sleeps 6h, then buildEngine()
  └─ buildEngine() → spawnEngine()
       └─ Engine.createFromNeuron(...)   # → self.aiengine
```

### B.2 Polling observations from central

`pollObservationsForever` runs on a daemon thread:

- **First poll**: random 5–30 minute delay after startup (to distribute load across nodes).
- **Subsequent polls**: every 11 hours.
- `observations = server.getObservationsBatch(storage=...)` — one observation per
  stream, the latest snapshot. ~74 streams in a typical batch.

### B.3 Ingest — normalize + group (`data_helper.py`)

`engine-lite/satoriengine/data_helper.py`

The raw batch is passed to `batch_to_stream_frames(observations)` which:

1. Calls `normalize_central_observation(obs)` for each raw observation:
   - Parses `observed_at` to a **float epoch once** (falls back to `ts`).
   - Parses `value` to `float`.
   - Uses `hash` as id, falling back to the row `id` (hash is always `""` in practice).
   - Returns `{stream_uuid, stream_name, epoch, value, id}`.
2. Groups normalized rows by `stream_uuid`.
3. Returns `{stream_uuid: DataFrame[epoch, value, id]}` — one frame per stream,
   sorted oldest-first.

The timestamp is parsed to a float epoch **exactly once** here. It is never
re-parsed.

### B.4 Raw observation format from central

`getObservationsBatch` returns the JSON body of `/api/v1/observations/batch`
plus a client-added `stream_uuid`. Each observation:

```json
{
  "id": 201570,
  "value": "0.00083",
  "observed_at": "1779426055.9544094",
  "ts": "2026-05-22T05:00:55.955559",
  "hash": "",
  "stream_id": 66,
  "stream_uuid": "b4bf6ce9-64be-49b2-a17c-037cb4a40f9f",
  "stream": { "id": 66, "uuid": "b4bf6ce9-...", "name": "safetrade_xtm", ... }
}
```

| Field | Type | Notes |
|---|---|---|
| `id` | int | Central's row id; used as `id` since `hash` is always empty |
| `value` | **string** | Numeric value as a string — parsed to `float` at ingest |
| `observed_at` | **string** | Unix epoch with fractional seconds — the timestamp used |
| `ts` | string | ISO-8601, no timezone — ignored (observed_at takes precedence) |
| `hash` | string | Always `""` in practice |
| `stream_uuid` | string | Added client-side; copy of `stream.uuid` |

### B.5 Persist — `StreamStore`

`engine-lite/satoriengine/stream_store.py`

One `StreamStore` per db file. Single table:

```sql
CREATE TABLE IF NOT EXISTS observations (
    stream_uuid TEXT NOT NULL,
    epoch       REAL NOT NULL,
    value       REAL NOT NULL,
    id          TEXT,
    PRIMARY KEY (stream_uuid, epoch)
)
```

`store.append(stream_uuid, frame)` — `INSERT OR IGNORE` via `executemany`.
Dedup is free via the composite PK — no SHA hash chain needed.

`store.history(stream_uuid)` — reads all rows for the stream, converts
`epoch → datetime64 UTC` **once** (vectorized), returns `[date_time, value, id]`
sorted oldest-first. This is the single epoch→datetime64 conversion point.

### B.6 Per-stream model and `onDataReceived`

For each stream in the batch (`pollObservationsForever` in `start.py`):

1. **Create `StreamModel`** if it doesn't exist for this `stream_uuid`.
2. **Convert epoch → datetime64 once** per stream:
   ```python
   engine_frame = pd.DataFrame({
       'date_time': pd.to_datetime(frame['epoch'], unit='s', utc=True),
       'value': frame['value'].values,
       'id': frame['id'].values,
   })
   ```
3. Call `model.onDataReceived(engine_frame)` — once per stream per poll.

`onDataReceived` (`engine.py`):

1. **Storage**: extracts epoch from `date_time` (`astype('int64') // 10**9`),
   builds `[epoch, value, id]`, calls `storage.storeStreamData` → `StreamStore.append`.
2. **In-memory**: appends only rows not already in `self.data` (checked on `date_time`).
3. **Adapter check**: `chooseAdapter(inplace=True)` — once per stream per poll.
4. **Predict**: `producePrediction()` if new rows were stored.

### B.7 Adapter selection — the >10-row rule

`chooseAdapter` iterates `preferredAdapters = [XgbAdapter, StarterAdapter]`,
picks first whose `condition()` returns `1.0`.

| Rows in `self.data` | Adapter |
|---|---|
| `<= 10` | `StarterAdapter` (simple stats, no training) |
| `> 10` | `XgbAdapter` (XGBoost, trained) |

`StarterAdapter`: 1 row → echo; 2–4 rows → mean of last 2; >4 rows → linear
regression on index. (Mirrors `LiteEngine`.)

`XgbAdapter`: trains via a shared single-worker queue
(`satoriengine/veda/training/queue_manager.py`).

### B.8 XGB preprocessing — `xgbDataPreprocess`

`engine-lite/adapters/xgboost/preprocess.py`

Called by `XgbAdapter._manageData` on every `fit` / `predict`. Takes the
`[date_time (datetime64), value, id]` frame:

1. Detects datetime64 dtype — skips re-parsing if already converted (fast path).
2. Sets `date_time` as index.
3. Detects sampling frequency (median gap). If noisy (>5% distinct gaps),
   rounds timestamps onto a regular grid.
4. Collapses duplicates: `groupby(level=0).agg({"value":"mean","id":"first"})`.
5. `asfreq(freq, method="nearest")` — fills gaps with NaN.

`_manageData` then adds:
- `_prepareTimeFeatures`: `hour, day, month, year, day_of_week`.
- `addPercentageChange`: `percent{n}` for Fibonacci lags `1,2,3,5,8,13,21,34,55`.
- `clearoutInfinities`: clamps `±inf` to column min/max.
- `tomorrow = value.shift(-1)` — the training target.

### B.9 Autoregression — the value we ship is t+2, not t+1

`Engine.producePrediction` (`engine.py:1086`) runs **two-step autoregression on
every adapter, uniformly**. The adapter is asked for a one-step forecast, that
forecast is appended to the history as if it were a real observation, and the
adapter is asked again — the **second** forecast is what gets stored and
submitted.

```
firstForecast  = model.predict(self.data)
augmentedData  = self.data + synthetic_row(value=firstValue, date_time=firstForecast.date_time)
secondForecast = model.predict(augmentedData)
forecast       = secondForecast if isinstance(secondForecast, DataFrame) else firstForecast
```

`_createAugmentedData` (`engine.py:1061`) builds the synthetic row:

| Column | Source |
|---|---|
| `date_time` | `firstForecast['date_time'].iloc[0]` (or `ds`, or `now()`) |
| `value` | `StreamForecast.firstPredictionOf(firstForecast)` — the first-step value |
| `id` | `sha256(f"{firstValue}{timestamp}").hexdigest()[:16]` — synthetic, not a real hash-chain link |

Then `pd.concat([self.data, tempRow])`. The synthetic row only lives for the
duration of the second predict call; it is not persisted.

**Implications:**

- The value queued for central is a **2-step-ahead** forecast.
- Cost doubles: every adapter runs `predict` twice per stream per poll.
- For `XgbAdapter`, the second call re-runs `xgbDataPreprocess` + `_manageData`
  on a frame that is one row longer.
- For `ETSAdapter`, the first call is a pure cache hit (`level + φ·trend`,
  no statsmodels call). The second call walks the synthetic row through the
  Holt-Winters update equations (manual `refit=False` equivalent, since
  `HoltWintersResults` has no `.append()`). Both calls are O(1) — no L-BFGS-B.
- If the second call returns anything other than a DataFrame, the engine logs a
  warning and falls back to the first forecast.

### B.10 Predictions — storage and submission

After `producePrediction`:

- **Local SQLite**: `storage.storePrediction(predictionStreamUuid, ...)` → written
  to `EngineSqliteDatabase` (per-stream table, `provider='engine'`). Audit log only;
  not read back by the core prediction flow.
- **In-memory**: stored as `model._pending_prediction = {stream_uuid, stream_name, value, observed_at, hash}`
  (`engine.py:1032`).

`collectAndSubmitPredictions` (`start.py:4188`) runs once per poll cycle after
all streams are processed:

1. Walks `aiengine.streamModels`, drains each `_pending_prediction` into the
   engine queue via `aiengine.queuePrediction(...)` (`engine.py:411`).
2. `aiengine.flushPredictionQueue()` (`engine.py:423`) → one batch submit.
3. `server.publishPredictionsBatch(predictions)` (`satorilib/src/satorilib/server/server.py:1248`)
   POSTs to `/api/v1/predictions/batch`.

**Submission payload — `POST /api/v1/predictions/batch`:**

```json
{
  "predictions": [
    {
      "stream_uuid": "b4bf6ce9-64be-49b2-a17c-037cb4a40f9f",
      "stream_name": "safetrade_xtm",
      "value": "0.00084",
      "observed_at": "1779426055.9544094",
      "hash": "<observation hash>"
    }
  ]
}
```

| Field | Source |
|---|---|
| `stream_uuid` | `StreamModel.streamUuid` |
| `stream_name` | Pulled from `subscriptionStream.streamId.stream` |
| `value` | **Second** (autoregressed) forecast as a string |
| `observed_at` | The triggering **observation's** timestamp — not the prediction's t+2 timestamp |
| `hash` | The triggering observation's hash |

Response:

```json
{ "total_submitted": N, "successful": K, "failed": N-K, "prediction_ids": [...], "errors": [...] }
```

The queue is cleared only when `successful > 0`. On failure the queue is
retained and retried on the next flush — predictions can therefore stack up
across poll cycles if central is unreachable.

---

## Storage layout

### Observation store — `StreamStore` (new, efficient)

`engine-lite/satoriengine/stream_store.py` → `engine-lite/db/engine.db`

Single `observations` table: `(stream_uuid, epoch, value, id)` with composite PK.
Used for all Path B observation history.

**Auto-migration**: on first startup, `StreamStore.__init__` detects legacy
per-stream tables (written by old code) and migrates them into `observations`.
Completion is recorded in `_satori_migrations` so subsequent startups skip.
Prediction tables (identified by `provider='engine'`) are skipped — they stay
in `EngineSqliteDatabase`.

### Prediction store — `EngineSqliteDatabase` (legacy, retained)

`engine-lite/storage/sqlite_manager.py` → `engine-lite/db/engine.db`

Per-stream tables (`provider='engine'`). Used for prediction audit log only.
Not performance-critical (one write per stream per 11h poll).

### Relay-path stores

`neuron-lite/satorineuron/network_db.py` — separate SQLite file.
`observations` and `predictions` tables for Path A only. Unrelated to
`EngineSqliteDatabase`.

---

## Quick reference — column names per stage

| Stage | Object | Columns |
|---|---|---|
| Raw central batch | JSON array | `id, value, observed_at, ts, hash, stream_id, stream_uuid, stream{}` |
| After `normalize_central_observation` | dict | `stream_uuid, stream_name, epoch (float), value (float), id` |
| After `batch_to_stream_frames` | `DataFrame` | `epoch, value, id` |
| `pollObservationsForever` → engine | `DataFrame` | `date_time (datetime64 UTC), value, id` |
| `StreamStore` on disk | SQLite row | `stream_uuid, epoch (REAL), value, id` |
| `StreamStore.history()` → engine | `DataFrame` | `date_time (datetime64 UTC), value, id` |
| `StreamModel.data` in memory | `DataFrame` | `date_time (datetime64 UTC), value, id` |
| XGB after `xgbDataPreprocess` | `XgbProcessedData.dataset` | `value, id` (datetime index) |
| XGB after `_manageData` | training frame | `value, hour, day, month, year, day_of_week, percent{1..55}, tomorrow` |

---

## What was optimised (2026-05-23, `seer` branch)

The old central path called `onDataReceived` 74× per poll (one per observation),
each time doing 2 DataFrame copies, a per-row SHA hash chain DB query, a
`pd.concat + drop_duplicates` over the full history, and a `psutil` RAM check.

The port replaced this with:

| Before | After |
|---|---|
| 74 single-row DataFrames | `batch_to_stream_frames` → 1 frame per stream |
| Per-row `_getLastHashLocked` + SHA | `INSERT OR IGNORE` on composite PK, no hashing |
| Timestamp parsed 3–4× | Parsed to float epoch once in `data_helper`; epoch→datetime64 once in poll loop |
| Per-stream UUID tables | Single `observations` table |
| `pd.concat + drop_duplicates` 74× | Append-only diff on `date_time` once per stream |
| `chooseAdapter` + psutil 74× | Once per stream per poll |
| `producePrediction` 74× before batch | Once per stream, still before batch collect |

Playground (`./playground`) proves the design end-to-end. Auto-migration runs
once on first startup per node; subsequent restarts hit the sentinel and skip.

---

## Open issues

1. **Two parallel engines.** `LiteEngine` (relay) and `veda.Engine` (central)
   both publish to `{stream}_pred`. It is unclear which is the intended
   production path.
2. **`LiteEngine` ignores time.** Regression uses observation index, not
   timestamps — irregular cadence is invisible to it.
3. **`asfreq` introduces NaN rows.** For sparse streams, XGB resampling fills
   gaps with NaN; the last feature row can be NaN-heavy.
4. **30-observation window** in Path A. Hard cap on `LiteEngine` history.
5. **XGB underperforms naive last-value.** Benchmarked ~4× worse on real Satori
   streams. Documented in `tasks/prediction-engine-upgrade.md`.
6. **`_manageData` reprocesses full history** on every `fit`/`predict` call.
   Fine at low row counts; will slow as history grows past ~400 rows.
7. ~~**ETS refits from scratch on every predict.**~~ Fixed: `ETSAdapter` now
   caches `(α, β, φ, level_n, trend_n)` after `fit()` and propagates new
   observations through the Holt-Winters update equations instead of refitting.
   Cold refits (when structural params change or the cache is too stale) are
   warm-started via `start_params`. Bench `./playground-ets` shows ~2x speedup
   with numerically equal predictions (first call bit-identical; autoregressive
   second call diverges by < 1e-4 due to frozen smoothing params, which is the
   intended semantic of `refit=False`).

---

## Key files

| File | Role |
|---|---|
| `neuron-lite/start.py` | Orchestration — both paths (`pollObservationsForever`, `collectAndSubmitPredictions`, `_networkRunEngine`) |
| `engine-lite/satoriengine/data_helper.py` | `normalize_central_observation`, `batch_to_stream_frames` — single ingest point |
| `engine-lite/satoriengine/stream_store.py` | `StreamStore` — efficient observation storage + auto-migration |
| `engine-lite/storage/manager.py` | `EngineStorageManager` — wires `StreamStore` for observations, `EngineSqliteDatabase` for predictions |
| `engine-lite/storage/sqlite_manager.py` | `EngineSqliteDatabase` — legacy per-stream tables (predictions only) |
| `engine-lite/engine.py` | `veda.Engine` / `StreamModel` — `onDataReceived`, `producePrediction`, `chooseAdapter` |
| `engine-lite/adapters/xgboost/xgb.py` | `XgbAdapter` — XGBoost model |
| `engine-lite/adapters/xgboost/preprocess.py` | `xgbDataPreprocess` — XGB DataFrame preprocessing |
| `engine-lite/adapters/starter/starter_model.py` | `StarterAdapter` — simple-stats fallback (≤10 rows) |
| `engine-lite/adapters/ets/ets_model.py` | `ETSAdapter` — Holt-Winters (statsmodels), refits on every predict |
| `engine-lite/satoriengine/veda/training/queue_manager.py` | Shared single-worker training queue |
| `engine-lite/testground/engine_testground.py` | End-to-end playground: ingest → normalize → persist → engine → predict |
| `engine-lite/testground/central_batch_sample.json` | Captured 74-observation batch for offline testing |
