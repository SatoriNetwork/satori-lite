# Heavy Engine

## Overview

Continuously training prediction system for the main datastreams transported via the central server. Uses an adapter-based model selection system that automatically picks the best model based on available resources (RAM, CPU) and dataset size. A pilot/stable comparison loop ensures the production model only improves over time.

**Source**: `engine-lite/satoriengine/veda/engine.py`

---

## Model Adapters

All adapters implement the `ModelAdapter` interface (`engine-lite/adapters/interface.py`):

```
condition(data, cpu, availableRamGigs) → float (0.0–1.0)
fit(data) → TrainingResult
predict(data) → DataFrame with 'pred' column
score() → float (MAE, lower is better)
compare(other) → bool (True if this model beats other)
load(modelPath) → model or None
save(modelPath) → bool
```

### Adapter Selection Table

| Adapter | File | Data Size | RAM Min | Other Conditions |
|---|---|---|---|---|
| **StarterAdapter** | `adapters/starter/starter_model.py` | ≤ 10 rows | 25 MB | Also selected when RAM < 25MB regardless of data size |
| **XgbAdapter** | `adapters/xgboost/xgb.py` | > 10 rows | 25 MB | Also selected when CPU = 1 |
| **XgbChronosAdapter** | `adapters/xgbchronos/xgb_chronos.py` | 20–1000 rows | 100 MB | Adds Chronos predictions as features |
| **SKAdapter** | `adapters/sktime/sk.py` | 1000–10000 rows | 100 MB | Requires CPU > 4 |
| **SimpleTTMAdapter** | `adapters/tinytimemixer/simplettm.py` | < 10 rows | 100 MB | TinyTimeMixer-based |

### Selection Logic (`chooseAdapter()` — engine.py:1133-1182)

```
preferredAdapters = [XgbAdapter, StarterAdapter]
defaultAdapters   = [XgbAdapter, XgbAdapter, StarterAdapter]

1. For each preferred adapter (skip any in failedAdapters):
   - If adapter.condition() == 1.0 → select it, stop
2. If none matched, try defaultAdapters in order
3. Ultimate fallback: StarterAdapter
```

The adapter is re-evaluated at the start of each training iteration, so as data grows the engine automatically switches (e.g., StarterAdapter → XgbAdapter when data exceeds 10 rows).

---

## StarterAdapter

**Purpose**: Fallback adapter for minimal data or low-resource environments. No training.

- `fit()` returns status -1 (no training performed)
- `compare()` always returns True (any upgrade replaces it)
- `score()` always returns 0.0
- `predict()` uses `starterEnginePipeline()`:
  - 0 rows → returns 0
  - 1 row → returns that value
  - 2–4 rows → mean of last 2
  - 5+ rows → sklearn LinearRegression on indices

---

## XgbAdapter

**Purpose**: Primary prediction adapter using XGBoost gradient-boosted trees.

### Data Pipeline (`_manageData()`)

1. **Preprocessing** (`xgbDataPreprocess()` in `adapters/xgboost/preprocess.py`):
   - Parses timestamps (Unix or ISO format)
   - Detects sampling frequency from median time diff
   - Rounds noisy timestamps if >5% variance in intervals
   - Aggregates duplicate timestamps (mean of values)
   - Resamples to regular frequency grid

2. **Feature Engineering**:
   - **Percentage changes** at Fibonacci-inspired lags: `[1, 2, 3, 5, 8, 13, 21, 34, 55]`
   - **Time features**: hour, day, month, year, day_of_week
   - **Infinity cleanup**: replaces ±inf with column max/min finite values
   - **Target column**: `tomorrow = value.shift(-1)` (next value as prediction target)

3. **Train/Test Split**: 80/20, `shuffle=False` (preserves time order)

### Training Loop

Each call to `fit()`:
1. Preprocesses data incrementally (only new rows)
2. Splits 80/20
3. Mutates hyperparameters (Gaussian noise, 10% of range std dev)
4. Trains `XGBRegressor` with eval_set on both train and test

### Hyperparameter Search

Parameters are mutated each iteration within bounds:

| Parameter | Range |
|---|---|
| n_estimators | 100–2000 |
| max_depth | 3–10 |
| learning_rate | 0.005–0.3 |
| subsample | 0.6–1.0 |
| colsample_bytree | 0.6–1.0 |
| min_child_weight | 1–10 |
| gamma | 0–1 |
| scale_pos_weight | 0.5–10 |

- `random_state` is kept static across mutations
- Integer params (n_estimators, max_depth, min_child_weight) are rounded
- Uses all CPU cores (`n_jobs=-1`) and `tree_method='hist'`

### Scoring

- Uses **Mean Absolute Error (MAE)** on the test set
- Untrained model returns `inf`

### Model Persistence

- Saves/loads via `joblib`: `{'stableModel': XGBRegressor, 'modelError': float}`
- Handles corrupted files (deletes if pickle/corrupt/truncated error)
- Auto-creates directory structure on save

---

## Pilot/Stable Training Loop

The core training loop in `_single_training_iteration()` (engine.py:1184-1221):

```
1. chooseAdapter(inplace=True)     # may switch adapter class
2. if adapter is StarterAdapter:   # skip training, just predict
       return
3. pilot.fit(data)                 # train with mutated hyperparameters
4. if pilot.compare(stable):       # pilot MAE < stable MAE?
       save(pilot)                 #   yes → save to disk
       stable = deepcopy(pilot)    #   yes → promote to stable
```

**Key properties**:
- The stable model's score **never worsens** — only improvements are kept
- Hyperparameters mutate each iteration, exploring the search space
- `compare()` against a different adapter class always returns True (enables switching from StarterAdapter to XgbAdapter)
- Failed adapters are tracked in `failedAdapters` and skipped in future selection

---

## Testing

**Test file**: `tests/test_heavy_engine.py` (91 tests)

Run inside the container:
```bash
docker exec satori python -m pytest /Satori/tests/test_heavy_engine.py -v
```

### Test Coverage

| Area | Tests | What's Verified |
|---|---|---|
| StarterAdapter condition | 6 | Data size thresholds, RAM fallback |
| StarterAdapter prediction | 10 | All tiers (empty, 1, 2-4, 5+), edge cases |
| StarterAdapter interface | 5 | fit/compare/score/save/load contracts |
| XgbAdapter condition | 6 | RAM, data size, CPU thresholds |
| XgbAdapter hyperparameters | 6 | Bounds, mutation, type safety |
| XgbAdapter fit & predict | 10 | Linear, constant, sinusoidal, noisy, negative, spikes, large/small values |
| XgbAdapter scoring | 4 | Untrained, after fit, constant data, custom test set |
| XgbAdapter compare | 4 | Cross-type, same-type, self-comparison |
| XgbAdapter save/load | 4 | Roundtrip, missing file, directory creation, prediction consistency |
| Adapter switching | 7 | Condition-based selection, growing dataset switchover |
| Starter→Xgb transition | 5 | End-to-end data growth scenarios |
| Data edge cases | 10 | Min viable, large, frequencies, zeros, steps, alternating, exponential, negative |
| Preprocessing | 6 | Feature columns, no infinities, split ratio, time ordering |
| Training improvement | 8 | Score monotonicity, convergence, hyperparameter mutation, growing data |
