# Satori-Lite Engine — Comprehensive Improvement Plan

## Context

The engine serves prediction streams at **wildly different cadences**: 10-minute, hourly, daily, even yearly. Two pressures shape this redesign, and both are **adapter-agnostic** — they apply to ETS (just added), XGB, and any future adapter (Chronos, TTM, sktime, ensembles, foundation models):

1. **Latency budget, relative to cadence.** Each stream must produce a fresh prediction well before its *own* next observation. For a 10-min stream that's minutes; for a yearly stream that's months. A single absolute timeout is wrong for both ends.
2. **History grows without bound — but at different rates.** A 10-min stream gathers 144 rows/day; a yearly stream may have <20 lifetime points. `StreamModel.data` (`engine.py:581–654`) accumulates everything, and `_manageData` / `_rollingMae` / future adapters re-scan the full series each fit. The fix can't be a single row count.

The plan is organized in three layers:
- **Layer A: Framework primitives** — the cadence-aware skeleton every adapter inherits.
- **Layer B: Engine-level improvements** — adapter-agnostic changes on top of the primitives.
- **Layer C: Forecasting quality** — model-side ideas that the framework should support but that live inside adapters or new adapters.

ETS-internal tuning is **not** scope-blocked here; it lives in Layer C and benefits from the Layer A/B primitives once they're in place.

---

## Layer A — Framework primitives

### A.1 `StreamProfile`: the cadence-aware abstraction

A small per-stream object computed once at ingest and refreshed when cadence changes. Single source of truth for every engine decision below.

```
StreamProfile:
    median_interval_s         # detected from data; today lives inside xgbDataPreprocess
    cadence_class             # 'fast' (≤1h), 'medium' (≤1d), 'slow' (>1d)
    train_window_seconds      # cadence_class → seconds of history kept in memory
    fit_budget_seconds        # cadence_class → max wall-time per fit
    predict_budget_seconds    # cadence_class → max wall-time per predict
    min_rows_for_trained      # cadence_class → overrides global MIN_OBSERVATIONS
    seasonal_periods          # cadence_class → e.g. fast:[144,1008]; medium:[7]; slow:[]
    max_lag                   # cadence_class → cap on XGB Fibonacci lags
    preferred_adapters        # cadence_class → ordered ladder (see B.6)
    value_kind                # 'numeric_continuous' / 'numeric_count' / 'binary' / 'categorical'
    sparsity                  # fraction of intervals with no observation
```

Cadence detection already exists inside `xgbDataPreprocess` (`engine-lite/adapters/xgboost/preprocess.py`). **Promote it** from a per-fit local computation to a `StreamModel` attribute so every adapter and the orchestrator can read it.

**Suggested defaults** (tunable):

| Class | Cadence | `train_window` | `fit_budget` | `min_rows` | Default ladder |
|---|---|---|---|---|---|
| fast   | ≤ 1 h | 4 weeks      | 60 s   | 10 | Starter → ETS → XGB |
| medium | ≤ 1 d | 1 year       | 5 min  | 10 | Starter → ETS → XGB → ZeroShot |
| slow   | > 1 d | all history  | 30 min | 3  | Starter → ZeroShot → ETS |

### A.2 Stream classification (value kind)

Today the engine treats every value as numeric continuous. Some Satori streams are counts, binaries, or categoricals; some are intermittent (mostly zero with occasional bursts).

**Change.** Classify streams on first ingest into `numeric_continuous / numeric_count / binary / categorical`. Adapter ladder routes accordingly: e.g. Croston's method for intermittent counts, a majority-class baseline for categoricals, logistic for binary. Numeric continuous gets today's stack.

### A.3 Profile persistence

Store the `StreamProfile` next to the model in `StreamStore` so restarts skip re-classification and re-detection. Invalidate when median cadence drifts >20% from the persisted value.

---

## Layer B — Engine-level improvements (ranked by impact)

### B.1 Cadence-aware in-memory training window

**Where.** `StreamModel.__init__`, `StreamModel.onDataReceived` (`engine.py:581–654`).

**Change.** Truncate `self.data` by **time** (`now - profile.train_window_seconds`), not row count. `StreamStore` keeps full history; only the training frame is bounded. Slow streams get "all history" → no truncation, uniform abstraction.

**Why.** Every adapter consumes `self.data`. Capping it once at the source bounds preprocessing, fit time, and memory for ETS, XGB, and every future adapter without touching their code — and the cadence-aware bound is correct for 10-min, hourly, daily, and yearly streams alike.

### B.2 Cadence-relative latency-budgeted adapter selection

**Where.** `chooseAdapter` (`engine.py:1173–1226`) and `queue_manager.py:54–123`.

**Change.** Track `last_fit_seconds` per `(stream_uuid, adapter_class)`. In `chooseAdapter`, demote any adapter whose recent fit exceeded `profile.fit_budget_seconds` for K cycles. Demote, don't ban — re-try periodically. Pilot/stable protects quality; this protects throughput per-class.

### B.3 Skip fit if data is unchanged — ⚠️ deferred

**Where.** `queue_manager.py:54–123` worker loop.

**Original idea.** Per stream, remember `(len(data), last_row.date_time)`. If unchanged since last successful fit, skip and re-queue. Was framed as "trivially cadence-independent."

**Why deferred (2026-05-26 prototype run).** A naive skip-when-unchanged conflicts with the engine's current exploration model. `ETSAdapter.fit` and `XgbAdapter.fit` both draw **fresh random hyperparameters per call** (see `adapters/ets/ets_model.py:78`, `adapters/xgboost/xgb.py:140`), and `compare(stable)` keeps the winner. Repeated fits on the same data are how the stable model improves — they're not redundant work. Naive B.3 froze that exploration the moment data stopped changing, which is exactly the wrong outcome on slow streams (where exploration time is most plentiful).

**What to do instead.** Either of:
- **Bounded skip** — allow up to N fits per data fingerprint, then skip. Caps worst-case CPU while preserving exploration headroom.
- **Convergence-aware skip** — track "fits since last `stable` improvement"; skip only after that exceeds a threshold. Lets exploration run as long as it's still finding wins.

Either variant should also key on `adapter_class` so an in-flight adapter upgrade (Starter → ETS → XGB) forces a fresh fit on the new pilot. A prototype caught this the hard way: `fromCentralServer` bypasses `__init__`, so any instance attribute added for B.3 must also be set in that factory or every CentralServer-spawned stream crashes its first training iteration with `AttributeError`.

**Telemetry kept.** The fit-duration log line `fit <uuid8> adapter=<class> rows=<n> took=<s>` added during the B.3 prototype was retained — pure observability, no behavior change, useful baseline for B.5/B.10/B.13 work.

### B.4 Adapter contract: `update(new_rows)` instead of full-frame refits

**Where.** `ModelAdapter` in `engine-lite/adapters/interface.py`; call sites in `engine.py`.

**Change.** Add optional `update(new_rows)`. Default falls back to `fit(full_data)` (nothing breaks). Adapters opt in:
- XGB: append-only feature computation (only new rows go through `addPercentageChange` and time-features; concat onto cached frame).
- ETS: warm-start from previously fitted statsmodels state.
- Future Chronos/TTM: zero-shot, `update` is a no-op.

### B.5 Naive last-value floor (quality guardrail)

**Where.** `StreamModel.compare` / `producePrediction`.

**Change.** Always compute naive last-value MAE on the same backtest tail used by the active adapter. If trained MAE > naive MAE, return naive. Bounds worst-case regression across all adapters and cadences. XGB benchmarks ~4× worse than naive on real Satori streams (`DATA_FLOW.md:340`) — this is non-negotiable until that's understood.

### B.6 Per-class adapter ladders

**Where.** `AUTO_ADAPTERS` (`engine.py:38–47`) replaced by `profile.preferred_adapters`.

**Change.** Ladder is read off the profile:
- **fast / medium**: `[Starter, ETS, XGB]` — cheap first, heavier only when budget allows.
- **slow**: `[Starter, ZeroShot (Chronos/TTM), ETS]` — pretrained zero-shot adapters work with very few points and avoid the "min 10 rows" cliff. Wake the existing scaffolds at `adapters/xgbchronos/` and `adapters/tinytimemixer/` whose `condition()` is currently `0.0`.

### B.7 Cadence-derived seasonality / lag features

**Where.** Passed to adapters via `profile.seasonal_periods` and `profile.max_lag`.

**Change.** ETS reads candidate periods from the profile instead of hard-coding non-seasonal. XGB's Fibonacci lags `[1,2,3,5,8,13,21,34,55]` get clipped to lags that fit the stream's available history.

### B.8 Per-stream model persistence

**Where.** Adapter `save`/`load` already exists; ETS has skeletons (`ets_model.py:54–74`).

**Change.** After every successful fit that wins pilot/stable, persist fitted state (not just `modelError`). On startup, load → predictions immediate; `update()` warm-starts instead of re-exploring. Critical for slow streams.

### B.9 Drift / regime-change guard

**Change.** Maintain trailing-N MAE per stream. If current MAE / baseline MAE > 2.0, mark the stream "dirty" and force a re-explore cycle (redraw hyperparams; reset XGB feature cache). Cadence-agnostic.

### B.10 Resource-aware scheduling

**Where.** `queue_manager.py`.

**Change.** Today it's a single FIFO worker with a 500 MB free-RAM check. Add:
- **Priority by freshness deficit**: streams closest to their next-tick deadline get priority. The slow-stream tail can wait; fast-stream tail cannot.
- **Per-adapter concurrency caps**: e.g. only one XGB at a time (heavy), but multiple ETS in parallel (light).
- **Backoff on repeated timeout**.

### B.11 Backtest harness / per-stream leaderboard

**Change.** A standard one-step rolling-origin MAE function in the engine (not duplicated inside each adapter as ETS does today). Every adapter scores against the same backtest, and per-stream telemetry surfaces which adapter is winning over time. Foundation for any future A/B work.

### B.12 Feature cache

**Where.** XGB-specific today, but generalize.

**Change.** A shared per-stream cache (keyed by `(stream_uuid, adapter_class, feature_set_version)`) so feature engineering is incremental and survives restarts. Removes the recurring "compute 9 percent-change lags on the full frame" cost.

### B.13 Observability

**Change.** Per-(stream, adapter) telemetry: `fit_seconds`, `predict_seconds`, `mae_trailing_N`, `picked_count`, `demoted_count`. Emit to logs and optionally Prometheus. Without this, all subsequent tuning is guesswork.

---

## Layer C — Forecasting quality (adapter-side, but framework-supported)

### C.1 Differencing / stationarity handling

Many Satori streams are integrated (prices, cumulative counts). Train models on first differences when an ADF test rejects stationarity; un-difference on predict. Lives inside adapters (or a wrapper) but the profile carries the recommendation.

### C.2 Outlier robustness

Single bad ticks blow up XGB and ETS. Add a winsorization or robust-z guard on incoming rows (configurable per stream). Optional median-filter pre-processing for noisy streams.

### C.3 Missing value handling

`asfreq(method='nearest')` (current XGB behavior) is wrong for sparse streams — it fabricates data. Switch to explicit gap handling: forward-fill ≤ k periods, mask beyond.

### C.4 Probabilistic forecasts / uncertainty bands

Where the model supports it (ETS, Chronos, TTM, quantile XGB), surface a prediction interval alongside the point forecast. Bounty scoring may eventually reward calibrated uncertainty.

### C.5 Multi-horizon prediction

Today every model trains for `shift(-1)` (one step ahead). Train for multiple horizons in one fit when the adapter supports it (Chronos, TTM natively; XGB via direct multi-output). Useful for bounty windows wider than one observation.

### C.6 Ensemble adapter

A meta-adapter that blends `{naive, ETS, XGB, ZeroShot}` with inverse-MAE weights computed from the backtest harness (B.11). Likely to beat any single component, with a small extra predict cost.

### C.7 Seasonal-naive baseline

A `SeasonalNaiveAdapter` that returns `value[t - seasonal_period]`. Trivially cheap; surprisingly competitive on strongly periodic streams. Sits between `Starter` and the trained models in every ladder.

### C.8 Theta / Croston / ARIMA

Library of cheap classical models. Theta is famously strong on M4-style data; Croston is the right tool for intermittent demand (zero-heavy streams identified by A.2). Auto-ARIMA via `pmdarima` for medium streams with strong seasonality.

### C.9 Cross-stream / global models

Most foundation models (Chronos, TTM) are already global by design. For our own learned models, consider one global XGB trained on all "fast crypto price" streams concatenated with stream-id as a feature, instead of N independent per-stream models. Better generalization for streams with little history; one fit job amortized.

### C.10 Streaming / online algorithms

For fast streams, swap batch-refit-every-N-minutes for true online updates (e.g. `river`'s online Holt-Winters, online linear regression, Bayesian online change-point). Per-row cost; no full-frame refit ever.

### C.11 Online change-point detection

Trigger a forced re-explore (B.9) when a change-point detector (e.g. BOCPD) fires, not just when MAE doubles.

---

## Smaller / housekeeping wins

- **Cap backtest tail length per class** — ETS already uses `horizon=3`; every adapter should use a small profile-derived tail.
- **Configurable training-queue parallelism** — single-worker is the right default, but make it a config; multi-worker becomes safe once B.1, B.2, B.4 are in.
- **Coalesce predictions on slow streams** — debounce duplicate fires.
- **Vectorize batch predict** — when many streams share an adapter (e.g. all using ETS), batch the call where possible.
- **Async I/O for `StreamStore`** — current SQLite writes block the worker thread.
- **Config-driven adapter packs** — let an operator switch on/off whole groups (heavy/light/experimental) without code changes.
- **A/B harness** — shadow-run a candidate adapter alongside the live one and report MAE delta over N cycles before promoting.
- **Stream-config override file** — let high-value streams override their detected profile (e.g. force `fit_budget=10 min` for a critical fast stream).

---

## Open issues this directly closes (from `DATA_FLOW.md`)

- (#1 Two parallel engines) — out of scope here; Path A vs Path B is an architectural decision.
- (#2 LiteEngine ignores time) — irrelevant once Path B is the production path.
- (#3 `asfreq` introduces NaN rows) — closed by **C.3**.
- (#4 30-observation window in Path A) — irrelevant once Path B is canonical; cadence-aware window in Path B (**B.1**) is the right design.
- (#5 XGB underperforms naive last-value) — bounded by **B.5** (naive floor) and revisited by **C.6** (ensemble).
- (#6 `_manageData` reprocesses full history) — closed by **B.1** (bounded window) + **B.4** (incremental update) + **B.12** (feature cache).

---

## Prototype findings (2026-05-26)

Baseline from `./playground` (77-stream walk-forward, 80/20):

| adapter | MAE | MAPE | wins |
|---|---|---|---|
| Starter (naive) | 88.79 | 78.40% | 3 |
| XGB | 142.22 | 3699.52% | 2 |
| ETS | 53.28 | 18.73% | 72 |

Two facts this nails down:
1. **XGB is materially worse than the naive last-value baseline** on real Satori streams — confirming the long-suspected pattern from `DATA_FLOW.md:340`. Until B.5 (naive floor) clamps it, XGB actively hurts aggregate quality whenever it's picked.
2. **ETS dominates 72/77 streams** under current hyperparameters. Any future change must not regress that.

Live container telemetry (post-prototype, B.3 reverted): ETS fits cost ~10–60ms each on 80-row windows. Cost is not the bottleneck at current cadences; **quality is** (B.5, then C.6/C.7 ensemble/seasonal-naive).

---

## Files touched (summary)

| Area | File | Items |
|---|---|---|
| Profile / cadence detection | `engine-lite/engine.py`, `engine-lite/adapters/xgboost/preprocess.py` | A.1, A.2, A.3, B.1, B.2, B.7 |
| Adapter selection | `engine-lite/engine.py:1173–1226` | B.2, B.6, B.9 |
| Training queue | `engine-lite/satoriengine/veda/training/queue_manager.py` | B.2, B.3, B.10 |
| Adapter interface | `engine-lite/adapters/interface.py` | B.4, B.8 |
| Per-adapter `update()` opt-in | `engine-lite/adapters/xgboost/xgb.py`, `engine-lite/adapters/ets/ets_model.py` | B.4 |
| Wake zero-shot adapters | `engine-lite/adapters/xgbchronos/chronos_adapter.py`, `engine-lite/adapters/tinytimemixer/simplettm.py` | B.6 |
| Backtest harness | `engine-lite/engine.py` (new module) | B.5, B.11 |
| Feature cache | `engine-lite/adapters/*` + `engine-lite/storage/` | B.12 |
| Telemetry | `engine-lite/engine.py`, `queue_manager.py` | B.13 |
| New adapters (Layer C) | `engine-lite/adapters/seasonal_naive/`, `ensemble/`, `theta/`, `croston/`, `online/` | C.6, C.7, C.8, C.10 |

---

## Suggested implementation order

1. **Foundation** — A.1 `StreamProfile`, A.2 value-kind classification, A.3 persistence. Nothing else makes sense without this.
2. **Bound the explosion** — B.1 (window), B.3 (skip-if-unchanged). Immediate latency + memory relief on every cadence.
3. **Make selection smart** — B.2 (budget-aware), B.6 (per-class ladders). Wakes zero-shot adapters for slow streams.
4. **Guardrails** — B.5 (naive floor), B.9 (drift guard), B.13 (telemetry). Quality safety net + visibility.
5. **Cut per-fit cost** — B.4 (`update`), B.8 (model persistence), B.12 (feature cache). The hard work; reaps the most after #2 is in.
6. **Scheduling polish** — B.10 (resource-aware queue), B.11 (backtest harness).
7. **Quality (Layer C)** — C.7 (seasonal naive), C.6 (ensemble), then C.1, C.2, C.3 as needed; C.10 (online) and C.9 (global models) are larger bets.

---

## Verification

Exercise each item via `engine-lite/testground/engine_testground.py` plus **synthetic streams at multiple cadences**:

1. Synthesize four streams: 10-min (4 weeks of history), 1-hour (6 months), daily (5 years), yearly (15 points). Add these to the testground harness.
2. Baseline run — record wall-time per stream, MAE per stream, peak RSS per cadence class.
3. Apply each item in isolation; re-run; compare deltas across all four cadence classes.
4. Multi-tick simulation: feed two cadence ticks back-to-back per class and assert each stream finishes a prediction within its own next-tick window (B.2, B.3, B.10).
5. Restart test: kill the engine mid-fit and confirm models load (B.8) and skip-if-unchanged (B.3) work on restart.
6. Drift test: inject a regime change in a synthetic stream and confirm B.9 fires.

End-to-end smoke: point a neuron at the playground sample plus a synthetic slow-stream fixture and watch predictions emit on `{stream}_pred` channels within each stream's cadence budget.
