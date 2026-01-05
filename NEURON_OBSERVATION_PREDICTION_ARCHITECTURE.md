# Satori Neuron Architecture

**Last Updated:** 2026-01-05
**Version:** 1.0

## Overview

The Satori Neuron is a client-side application that receives multi-cryptocurrency observations from the Central Server, generates predictions using AI models, and submits those predictions back to the server in batches.

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                         CENTRAL SERVER                               │
│                                                                       │
│  Daily Run (every 24h):                                             │
│  1. Fetch 25+ cryptos from 4 APIs                                   │
│  2. Fetch N pairs from SafeTrade                                    │
│  3. Save each as separate stream with UUID                          │
│                                                                       │
│  Endpoints:                                                          │
│  - GET  /api/v1/observations/batch  → Returns all 40+ observations │
│  - POST /api/v1/predictions/batch   → Receives batch predictions   │
└─────────────────────────────────────────────────────────────────────┘
                              ↓ GET observations
                              ↑ POST predictions
┌─────────────────────────────────────────────────────────────────────┐
│                        SATORI NEURON (Client)                        │
│                                                                       │
│  ┌────────────────────────────────────────────────────────────────┐ │
│  │  NEURON (start.py)                                             │ │
│  │  - Polls observations every 11 hours                           │ │
│  │  - Coordinates AI Engine                                       │ │
│  │  - Collects and batches predictions                            │ │
│  └────────────────────────────────────────────────────────────────┘ │
│                              ↓                                        │
│  ┌────────────────────────────────────────────────────────────────┐ │
│  │  AI ENGINE (engine.py)                                         │ │
│  │  - Manages 40+ StreamModels (one per crypto)                  │ │
│  │  - Queues predictions for batch submission                     │ │
│  │  - Flushes queue after all models predict                      │ │
│  └────────────────────────────────────────────────────────────────┘ │
│                              ↓                                        │
│  ┌────────────────────────────────────────────────────────────────┐ │
│  │  STREAM MODELS (40+ instances)                                │ │
│  │  - Each model: XgbAdapter or StarterAdapter                   │ │
│  │  - Trains on historical data                                   │ │
│  │  - Generates predictions when new data arrives                 │ │
│  └────────────────────────────────────────────────────────────────┘ │
│                              ↓                                        │
│  ┌────────────────────────────────────────────────────────────────┐ │
│  │  SQLite DATABASE                                               │ │
│  │  - streams table: Metadata for each crypto                     │ │
│  │  - <uuid> tables: Historical data per crypto (40+ tables)     │ │
│  └────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Data Flow

### 1. Observation Reception (Every 11 Hours)

**File:** `neuron-lite/start.py:271-364`

```python
def pollObservationsForever(self):
    while True:
        # 1. Fetch batch of observations from server
        observations = self.server.getObservationsBatch(storage=storage)

        # 2. Process each observation
        for observation in observations:
            stream_uuid = observation.get('stream_uuid')  # Server-provided UUID
            stream_name = observation.get('stream', {}).get('name')  # e.g., "btc", "eth"
            value = observation.get('value')

            # 3. Store stream metadata
            storage.db.upsertStream(
                uuid=stream_uuid,
                name=stream_name,
                author=stream_info.get('author'),
                ...
            )

            # 4. Store observation in stream-specific table
            storage.storeStreamObservation(
                streamUuid=stream_uuid,  # Used as table name
                timestamp=observation.get('observed_at'),
                value=str(value),
                hash_val=str(hash_val),
                provider='central'
            )

            # 5. Create model if doesn't exist
            if stream_uuid not in self.aiengine.streamModels:
                streamId = StreamId(
                    source='central-lite',
                    author='satori',
                    stream=stream_name,
                    target=''
                )
                self.aiengine.streamModels[stream_uuid] = StreamModel(
                    streamId=streamId,
                    predictionStreamId=None,
                    predictionProduced=None
                )
                self.aiengine.streamModels[stream_uuid].chooseAdapter(inplace=True)

            # 6. Pass data to model for prediction
            self.aiengine.streamModels[stream_uuid].onDataReceived(df)

        # 7. Collect predictions and submit batch
        self.collectAndSubmitPredictions()

        # Wait 11 hours
        time.sleep(60 * 60 * 11)
```

**Key Points:**
- Server provides `stream_uuid` - client does NOT generate UUIDs
- Each crypto gets its own SQLite table named by `stream_uuid`
- Each crypto gets its own AI model indexed by `stream_uuid`
- All observations processed before predictions collected

---

### 2. Prediction Generation

**File:** `engine-lite/engine.py:515-562, 909-991`

When `onDataReceived(df)` is called on a StreamModel:

```python
def onDataReceived(self, data: pd.DataFrame):
    # 1. Store data in SQLite
    insertedRows = self.storage.storeStreamData(
        self.streamUuid,
        storageDf,
        provider='central'
    )

    # 2. Update in-memory data
    self.data = pd.concat([self.data, engineDf], ignore_index=True)

    # 3. Trigger prediction
    if insertedRows > 0:
        self.producePrediction()

def producePrediction(self, updatedModel=None):
    # 4. Generate prediction using stable model
    forecast = model.predict(data=self.data)

    # 5. Pass to server (batched)
    self.passPredictionData(forecast, passToCentralServer=True)

def publishPredictionToServer(self, forecast, useBatch=True):
    # 6. Store prediction locally
    self.storage.storePrediction(...)

    # 7. Queue for batch submission
    self._pending_prediction = {
        'stream_uuid': self.streamUuid,
        'stream_name': stream_name,
        'value': predictionValue,
        'observed_at': observationTime,
        'hash': observationHash
    }
```

**Result:** Each model generates a prediction and stores it in `_pending_prediction` for the neuron to collect.

---

### 3. Batch Prediction Submission

**File:** `neuron-lite/start.py:233-269`

After all observations are processed:

```python
def collectAndSubmitPredictions(self):
    # 1. Collect predictions from all models
    for stream_uuid, model in self.aiengine.streamModels.items():
        if model._pending_prediction:
            pred = model._pending_prediction

            # 2. Queue in engine
            self.aiengine.queuePrediction(
                stream_uuid=pred['stream_uuid'],
                stream_name=pred['stream_name'],
                value=pred['value'],
                observed_at=pred['observed_at'],
                hash_val=pred['hash']
            )

            # 3. Clear pending prediction
            model._pending_prediction = None

    # 4. Submit all queued predictions in one API call
    result = self.aiengine.flushPredictionQueue()
```

**File:** `engine-lite/engine.py:346-383`

```python
def queuePrediction(self, stream_uuid, stream_name, value, observed_at, hash_val):
    with self.predictionQueueLock:
        self.predictionQueue.append({
            'stream_uuid': stream_uuid,
            'stream_name': stream_name,
            'value': value,
            'observed_at': observed_at,
            'hash': hash_val
        })

def flushPredictionQueue(self):
    with self.predictionQueueLock:
        predictions_to_submit = self.predictionQueue.copy()

    # Single API call for all predictions
    result = self.server.publishPredictionsBatch(predictions_to_submit)

    if result and result.get('successful', 0) > 0:
        self.predictionQueue = []  # Clear on success
```

**File:** `lib-lite/satorilib/server/server.py:1193-1247`

```python
def publishPredictionsBatch(self, predictions: list[dict]):
    response = self._makeAuthenticatedCall(
        function=requests.post,
        endpoint='/api/v1/predictions/batch',
        payload=json.dumps({'predictions': predictions})
    )

    return response.json()  # {total_submitted, successful, failed, ...}
```

**Result:** All 40+ predictions submitted in **1 API call** instead of 40+ individual calls.

---

## Database Schema

### Client SQLite Database

**Location:** `/Satori/Engine/db/engine.db`

#### `streams` Table (Metadata)

```sql
CREATE TABLE IF NOT EXISTS streams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_stream_id INTEGER,
    uuid TEXT UNIQUE NOT NULL,              -- Server-provided UUID
    name TEXT,                               -- e.g., "btc", "eth", "safetrade_btc"
    author TEXT,                             -- Wallet pubkey of publisher
    secondary TEXT,
    target TEXT,
    meta TEXT,
    description TEXT,
    last_synced TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
);
```

**File:** `engine-lite/storage/sqlite_manager.py:298-323`

#### Stream Data Tables (One per Crypto)

Each stream gets its own table named by UUID:

```sql
CREATE TABLE IF NOT EXISTS "<uuid>" (
    ts TIMESTAMP PRIMARY KEY NOT NULL,      -- Unix timestamp
    value NUMERIC(20, 10) NOT NULL,         -- Price/value
    hash TEXT NOT NULL,                     -- Data integrity hash
    provider TEXT NOT NULL                  -- "central"
);
```

**Example:**
- Table `"a1b2c3d4-..."` contains all Bitcoin price history
- Table `"e5f6g7h8-..."` contains all Ethereum price history
- 40+ tables total (one per cryptocurrency)

**File:** `engine-lite/storage/sqlite_manager.py:48-63`

---

## Model Management

### Dynamic Model Creation

**File:** `neuron-lite/start.py:315-341`

```python
if stream_uuid not in self.aiengine.streamModels:
    # Create StreamId
    streamId = StreamId(
        source='central-lite',
        author='satori',
        stream=stream_name,  # "btc", "eth", etc.
        target=''
    )

    # Create StreamModel
    self.aiengine.streamModels[stream_uuid] = StreamModel(
        streamId=streamId,
        predictionStreamId=None,
        predictionProduced=None
    )

    # Choose adapter (XgbAdapter or StarterAdapter)
    self.aiengine.streamModels[stream_uuid].chooseAdapter(inplace=True)
```

**Result:** First time a crypto is seen, a model is automatically created for it.

### Model Selection

**File:** `engine-lite/engine.py:1019-1068`

```python
def chooseAdapter(self):
    availableRamGigs = psutil.virtual_memory().available / 1e9

    for adapter in self.preferredAdapters:  # [XgbAdapter, StarterAdapter]
        if adapter.condition(data=self.data, cpu=self.cpu, availableRamGigs=availableRamGigs):
            return adapter

    return self.defaultAdapters[-1]  # StarterAdapter fallback
```

**Adapters:**
- **XgbAdapter:** XGBoost model (preferred if enough resources)
- **StarterAdapter:** Simple moving average (fallback)

### Model Training Loop

**File:** `engine-lite/engine.py:1070-1124`

Each model runs in its own thread:

```python
def run(self):
    while len(self.data) > 0:
        # 1. Choose best adapter
        self.chooseAdapter(inplace=True)

        # 2. Train pilot model
        trainingResult = self.pilot.fit(data=self.data, stable=self.stable)

        # 3. Compare to stable model
        if self.pilot.compare(self.stable):
            # 4. Save if better
            if self.pilot.save(self.modelPath()):
                self.stable = copy.deepcopy(self.pilot)

        # 5. Sleep before next iteration
        time.sleep(self.trainingDelay)  # Default: 600 seconds (10 min)
```

**Model Path:** `/Satori/Neuron/models/veda/<predictionStreamUuid>/<AdapterName>.joblib`

---

## API Endpoints

### Client → Server

#### GET `/api/v1/observations/batch`

**Purpose:** Fetch all observations from latest daily run

**Request:**
```http
GET /api/v1/observations/batch?limit=100
Authorization: Bearer <jwt_token>
```

**Response:**
```json
[
  {
    "id": 123,
    "value": "45000.50",
    "observed_at": "1704470400",
    "hash": "abc123",
    "ts": "2026-01-05T12:00:00",
    "stream": {
      "id": 1,
      "uuid": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
      "name": "btc",
      "author": "server_pubkey_here",
      "secondary": null,
      "target": null,
      "meta": null,
      "description": null
    }
  },
  {
    "id": 124,
    "value": "3000.25",
    ...
    "stream": {
      "uuid": "e5f6g7h8-...",
      "name": "eth",
      ...
    }
  }
  // ... 38 more observations
]
```

**File:** `lib-lite/satorilib/server/server.py:1249-1323`

#### POST `/api/v1/predictions/batch`

**Purpose:** Submit multiple predictions in one request

**Request:**
```http
POST /api/v1/predictions/batch
Authorization: Bearer <jwt_token>
Content-Type: application/json

{
  "predictions": [
    {
      "stream_uuid": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
      "stream_name": "btc",
      "value": "45100.00",
      "observed_at": "1704474000",
      "hash": "def456"
    },
    {
      "stream_uuid": "e5f6g7h8-...",
      "stream_name": "eth",
      "value": "3010.50",
      "observed_at": "1704474000",
      "hash": "ghi789"
    }
    // ... 38 more predictions
  ]
}
```

**Response:**
```json
{
  "total_submitted": 40,
  "successful": 40,
  "failed": 0,
  "prediction_ids": [201, 202, 203, ...],
  "errors": null
}
```

**File:** `lib-lite/satorilib/server/server.py:1193-1247`

---

## Configuration

### Neuron Settings

**File:** `satorineuron/config/config.yaml` (or similar)

```yaml
# Polling interval (hours)
observation_poll_interval: 11

# Model training delay (seconds)
training_delay: 600  # 10 minutes between training iterations

# Database path
data_dir: /Satori/Engine/db
dbname: engine.db

# Server URL
central_url: https://central.satorinet.io  # or localhost for dev
```

### Environment Variables

```bash
SATORI_ENV=prod          # prod, dev, local, testprod
SATORI_UI_PORT=24601     # Web UI port
```

---

## Key Files Reference

### Neuron

| File | Purpose |
|------|---------|
| `neuron-lite/start.py` | Main neuron orchestration, polling, batch submission |
| `neuron-lite/config.py` | Configuration management |

### Engine

| File | Purpose |
|------|---------|
| `engine-lite/engine.py` | Engine class, StreamModel class, prediction queue |
| `engine-lite/storage/manager.py` | Storage manager interface |
| `engine-lite/storage/sqlite_manager.py` | SQLite operations, stream tables |

### Adapters

| File | Purpose |
|------|---------|
| `engine-lite/adapters/xgboost/xgb.py` | XGBoost model adapter |
| `engine-lite/adapters/starter/starter_model.py` | Moving average fallback |

### Client Library

| File | Purpose |
|------|---------|
| `lib-lite/satorilib/server/server.py` | API client, batch methods |
| `lib-lite/satorilib/wallet/` | Authentication, signing |

---

## Development & Testing

### Running the Neuron

```bash
cd /app/central-lite/satori-lite
python neuron-lite/start.py
```

### Testing Observation Processing

```python
from satorilib.server import SatoriServerClient
from engine-lite.storage.manager import EngineStorageManager

# Initialize
server = SatoriServerClient(wallet)
storage = EngineStorageManager.getInstance()

# Fetch observations
observations = server.getObservationsBatch(storage=storage)
print(f"Received {len(observations)} observations")

# Check streams table
streams = storage.db.getAllStreams()
print(f"Tracking {len(streams)} streams")
```

### Testing Batch Predictions

```python
# Prepare predictions
predictions = [
    {
        'stream_uuid': 'a1b2c3d4-...',
        'stream_name': 'btc',
        'value': '45000',
        'observed_at': str(time.time()),
        'hash': 'abc123'
    }
]

# Submit batch
result = server.publishPredictionsBatch(predictions)
print(f"Success: {result['successful']}/{result['total_submitted']}")
```

### Inspecting SQLite Database

```bash
sqlite3 /Satori/Engine/db/engine.db

# List all tables (one per crypto + metadata)
.tables

# View streams metadata
SELECT uuid, name, author FROM streams;

# View Bitcoin data (replace UUID with actual)
SELECT * FROM "a1b2c3d4-e5f6-7890-abcd-ef1234567890" LIMIT 10;
```

---

## Troubleshooting

### No Observations Received

**Check:**
1. Server is running and accessible
2. JWT authentication is working: `server._ensure_authenticated()`
3. Endpoint is correct: `/api/v1/observations/batch`
4. Server has run daily data collection (Step 0 in daily_run.py)

**Logs:**
```
✓ Batch observation: 40 observations from server
✓ Stored btc: $45000.00 (UUID: a1b2c3d4...)
```

### Models Not Created

**Check:**
1. `stream_uuid` is present in observation response
2. Engine is initialized: `self.aiengine is not None`
3. StreamModel import is working

**Logs:**
```
✓ Created model for new stream: btc (UUID: a1b2c3d4...)
```

### Predictions Not Submitted

**Check:**
1. Models generated predictions: `model._pending_prediction` should be set
2. Batch collection is called: `collectAndSubmitPredictions()`
3. Queue is flushing: `flushPredictionQueue()`
4. Server endpoint is accessible: `/api/v1/predictions/batch`

**Logs:**
```
Collected 40 predictions from models
Submitting batch of 40 predictions to server...
✓ Batch submitted: 40/40 successful
```

### Database Errors

**Common Issues:**
- Table doesn't exist: Check `tableExists()` before operations
- Duplicate timestamp: PRIMARY KEY constraint on `ts` column
- Migration needed: Run `migrateTimestampFormat()` for datetime→Unix conversion

---

## Performance Considerations

### Memory Usage

- **Per Model:** ~50-200 MB (XGBoost) or ~5-10 MB (Starter)
- **40 Models:** ~2-8 GB total
- **Historical Data:** ~10-50 MB per crypto (depends on history length)

### CPU Usage

- **Training:** 1 thread per model (40 threads)
- **Prediction:** Lightweight, triggered on new data
- **Batch Submission:** Single network call (minimal overhead)

### Network Efficiency

**Before Batching:**
- 40+ API calls for predictions
- ~2-5 KB per call
- Total: ~100-200 KB

**After Batching:**
- 1 API call for predictions
- ~10-20 KB total
- **95% reduction in requests**

---

## Future Enhancements

1. **Adaptive Polling:** Adjust interval based on server activity
2. **Model Ensembles:** Combine multiple adapters for better accuracy
3. **Incremental Learning:** Update models without full retraining
4. **Compression:** Gzip batch payloads for large prediction sets
5. **Retry Logic:** Exponential backoff for failed submissions
6. **Metrics Dashboard:** Real-time monitoring of models and predictions

---

## Appendix

### Complete Observation Flow Example

```
1. Server Daily Run (00:00 UTC)
   ├─ Fetch BTC: $45,000
   ├─ Fetch ETH: $3,000
   └─ Save to streams table with UUIDs

2. Neuron Poll (11 hours later)
   ├─ GET /api/v1/observations/batch
   ├─ Receive 40 observations with stream metadata
   ├─ Store in SQLite:
   │  ├─ streams table: {uuid, name, author}
   │  └─ <uuid> tables: {ts, value, hash, provider}
   └─ Create models if needed

3. Model Prediction (immediately after data received)
   ├─ onDataReceived(df) → Store data
   ├─ producePrediction() → Generate forecast
   └─ publishPredictionToServer() → Queue prediction

4. Batch Submission (after all models predict)
   ├─ collectAndSubmitPredictions() → Collect from all models
   ├─ queuePrediction() × 40 → Add to engine queue
   ├─ flushPredictionQueue() → Submit batch
   └─ POST /api/v1/predictions/batch → Server saves all

5. Server Processing
   ├─ Validate predictions
   ├─ Save to predictions table (peer_id, value, ts)
   └─ Return {successful: 40, total_submitted: 40}
```

### UUID Consistency Verification

| Component | UUID Usage | Example |
|-----------|------------|---------|
| Server | Generates on stream creation | `uuid.uuid4()` → `a1b2c3d4-...` |
| Response | Included in stream object | `stream.uuid` |
| Client Storage | Table name for stream data | `CREATE TABLE "a1b2c3d4-..."` |
| Client Metadata | Primary key in streams table | `uuid TEXT UNIQUE NOT NULL` |
| Model Index | Key in streamModels dict | `streamModels['a1b2c3d4-...']` |
| Prediction | Identifies target stream | `stream_uuid: 'a1b2c3d4-...'` |

**Critical:** The same UUID flows through the entire system from server generation to prediction submission.

---

**End of Document**
