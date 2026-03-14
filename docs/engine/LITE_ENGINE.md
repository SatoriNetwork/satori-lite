# Lite Engine

## Overview

Stateless, lightweight prediction engine for datastreams transported via Nostr. Creates a fresh model on every `predict()` call — no persistent state, no training loop.

**Source**: `neuron-lite/satorineuron/lite_engine.py`

---

## Prediction Tiers

| Observations | Method |
|---|---|
| 0 | Returns `None` |
| 1 | Returns that value |
| 2–4 | Mean of last 2 values |
| 5+ | sklearn `LinearRegression` (predict at next index) |

---

## How It Works

1. Observations arrive **newest-first**, reversed to chronological order
2. Non-numeric values are filtered out (supports direct floats, string floats, JSON-encoded numbers)
3. Based on the count of valid numeric values, one of the tiers above is applied
4. For 5+ observations: fits `LinearRegression` with indices as X, values as Y, predicts at index `n`

### Dependencies
- `numpy` — array operations for sklearn input
- `sklearn.linear_model.LinearRegression` — same approach as the heavy engine's `StarterAdapter`

### Relationship to Heavy Engine
The 5+ tier matches the `StarterAdapter.starterEnginePipeline()` logic exactly (engine-lite/adapters/starter/starter_model.py:71-80). Both use sklearn LinearRegression on observation indices.

---

## Testing

**Test file**: `tests/test_lite_engine.py` (33 tests)

Run inside the container:
```bash
docker exec satori python -m pytest /Satori/tests/test_lite_engine.py -v
```

### Test Coverage

| Area | Tests | What's Verified |
|---|---|---|
| Empty/None input | 2 | Returns None for empty list |
| Single observation | 2 | Returns the value as string |
| Mean tier (2–4 obs) | 3 | Mean of last 2 chronological values |
| Linear regression (5+) | 5 | Upward/downward trends, constant, 10-point, noisy data |
| Non-numeric handling | 3 | Non-numeric strings, missing value key |
| JSON encoding | 3 | Numeric strings, quoted numbers, integers |
| Extract numeric | 5 | Float/int strings, non-numeric, empty, passthrough |
| Edge cases | 10 | Mixed valid/invalid, large/small values, negatives, zeros, zero-crossing, large dataset, tier boundary |
