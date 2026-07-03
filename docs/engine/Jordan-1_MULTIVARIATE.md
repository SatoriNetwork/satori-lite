# Jordan-1: Multivariate Prediction with Random-Swap Peer Search

Alternative to [`MULTIVARIATE.md`](./MULTIVARIATE.md). Same goal — use peer
data streams as features to predict a target — with two changes:

1. **XGBoost head, TimesFM as opt-in target-only feature.** No TimesFM on
   peers. When `use_tfm_on_target` is on, TimesFM produces a rolling
   one-step forecast of the target itself, and that becomes one feature
   (`tfm_delta`) the head weighs against target lags and peer deltas. One
   TimesFM call per predict per target, not K.
2. **Random-swap peer search with a persisted ledger.** Each retrain,
   identify the weakest peer by XGBoost feature gain, swap it for a random
   candidate from the store, retrain, keep or revert on test MAE. Every
   attempt is logged so rejected peers cool down and don't get retried
   immediately.

File layout, adapter interface, engine wiring, deepcopy rules, persistence
location, alignment rules, staleness handling, and data-quality guards are
unchanged from MULTIVARIATE.md §3.4.

---

## 1. Feature schema

| Feature | Definition | Source (train and inference) |
|---|---|---|
| target lags | pct-change at lags [1, 2, 3, 5, 8] | observed |
| `p{k}_delta_0` | peer k pct-change, t-1 to t | observed (aligned) |
| `p{k}_delta_1` | peer k pct-change, t-2 to t-1 | observed (aligned) |
| `tfm_delta` (optional) | TimesFM one-step forecast of the target, `(forecast_next - target_now) / target_now` | rolling one-step-ahead at train, single call at inference |
| label `y` | target level diff, t to t+1 | observed |

No `p{k}_next` and no `shift(-1)` on peers — peer features are symmetric
between train and serve. Peers use `merge_asof(direction='backward')` with
staleness tolerance and NaN → 0.0 fill. Delta target as MULTIVARIATE.md §3.3.

## 2. Constructing `tfm_delta` (opt-in)

**At training.** For each row t (t ≥ `tfm_min_context`, default 32),
context = target history up to and including t. Build N inputs (one per
training row) and issue ONE batched TimesFM call with horizon=1. Convert
each forecast to delta form. Rows with t < min_context or non-finite
forecasts: 0.0.

**Incremental cache.** TimesFM zero-shot is deterministic given a fixed
context, so `tfm_delta[t]` never changes across retrains. Cache
`{row_index: value}` per target uuid in the persisted state; each retrain
forecasts only newly-accumulated rows.

**At inference.** One TimesFM call with horizon=2 (covers the 2-step
autoregression) using the target's history up to the current epoch. Cache
per `(target_uuid, last_epoch)`; both `predict` calls in `_runForecast`
share it.

**On failure.** TimesFM missing / OOM / non-finite → `tfm_delta = 0.0`. The
head trains and serves as though TimesFM were disabled; no engine-level
fallback fires.

## 3. Random-swap peer search

Working set of K peers (default 5), evolved one swap per retrain.

**Initial peer set.** K uniformly random uuids from the candidate pool
(StreamStore, ≥ `peer_min_rows` rows, minus target, minus `_pred` streams,
minus zero-variance, minus cooldown). Opt-in warm-start: initial K =
correlation top-K per MULTIVARIATE.md §3.2.

**Retrain step:**

1. Train baseline head on current peer set (fixed seed); `mae_base` from
   chronological 80/20 test split.
2. Score peers by XGBoost feature gain
   (`booster.get_score(importance_type='gain')`) summed across each peer's
   `p{k}_delta_*` columns only (not `tfm_delta`). Lowest = weakest. Ties:
   oldest peer in set, then uuid order.
3. Pick a random candidate from the eligible pool.
4. Swap weakest for candidate, rebuild matrix, retrain (same seed);
   `mae_new`.
5. Accept iff `mae_new < mae_base * (1 - keep_margin)`, default
   `keep_margin = 0.01`.
6. On accept: peer set updated, ledger append `kept=True`.
7. On reject: restore prior head and peer set, new peer to cooldown, ledger
   append `kept=False`.

When TimesFM is on, `tfm_delta` values are held constant across both
trainings (cached), so the swap is attributable to the peer change alone.

**First fit.** No prior gains, so the swap step is skipped: train once on
the initial K peers, ledger entry with `swapped_in = swapped_out = None`,
`reason = 'initial'`.

## 4. Persisted state (schema_version = 2)

Joblib at `modelPath()`:

```python
{
    'schema_version': 2,
    'head_name': 'xgboost',
    'head_state': dict,
    'peer_uuids': list[str],
    'peer_gains': dict[str, float],
    'feature_columns': list[str],
    'staleness_seconds': float,
    'modelError': float,
    'selected_at_rows': int,
    'swap_ledger': list[dict],
    'cooldown': dict[str, int],
    'retired_peers': dict[str, int],
    'use_tfm_on_target': bool,
    'tfm_delta_cache': dict[int, float],
    'tfm_min_context': int,
}
```

Ledger entry:

```python
{
    'at_rows': int,
    'swapped_out': str | None,
    'swapped_in': str | None,
    'prev_test_mae': float,
    'new_test_mae': float,
    'kept': bool,
    'reason': str,  # 'initial' | 'margin' | 'no_improvement'
}
```

Ledger LRU-capped at `max_ledger_entries` (default 200). Toggling
`use_tfm_on_target` or changing `tfm_min_context` clears the cache.
`load()` refuses schema_version mismatches → None → clean retrain.

## 5. Files and wiring

| File | Change |
|---|---|
| `adapters/multivariate/heads.py` | NEW — head interface + `XgbHead` |
| `adapters/multivariate/features.py` | NEW — `alignPeers`, `buildFrame`, `tfmDeltaForRows` |
| `adapters/multivariate/peer_search.py` | NEW — initial pick, worst pick, candidate pick, ledger append, cooldown |
| `adapters/multivariate/multivariate.py` | REWRITE the stub (currently has inverted `condition()`) |
| `adapters/multivariate/__init__.py` | FIX (currently imports `StarterAdapter`) |
| `adapters/__init__.py` | Guarded optional import, same pattern as TimesFM |
| `engine.py` | Two lines: import + `ADAPTER_REGISTRY['multivariate']` |
| `satoriengine/stream_store.py` | One method: `count_streams_with_min_rows(min_rows)` |

**`condition()` gates:**

- target rows < 60 → 0.0
- fewer than 2 streams with ≥ 30 rows → 0.0 (single SQL count, ~60s TTL
  cache)
- any store exception → 0.0
- if `use_tfm_on_target: true`: available RAM < 2 GB → 0.0

**`fit()`.** Load candidate histories from StreamStore → align → attach
`tfm_delta` if enabled → run the retrain step (§3). Deepcopy-safe: no
sqlite connections, no torch model on the adapter instance; TimesFM
reached through `TimesFmAdapter._ensureModel()` and
`TimesFmAdapter._inference_lock`.

**`predict()`.** Build inference row (target lags + peer deltas +
`tfm_delta` if enabled) → `head.predict` → ship
`lastObservedValue + head_delta`. Augmented step of `_runForecast`'s
2-step autoregression uses last observed peer values and horizon-2
`tfm_delta` from the cached forecast.

**Config:**

```yaml
engine:
  preferred_adapter: multivariate
  multivariate:
    head: xgboost
    top_k: 5
    peer_min_rows: 30
    keep_margin: 0.01
    cooldown_rows: 100
    max_candidates: 50
    max_ledger_entries: 200
    warm_start: false
    use_tfm_on_target: false
    tfm_min_context: 32
```
