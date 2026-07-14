# Engine Evolution: GPU Support and the Road After Multivariate

Companion to [`IMPROVEMENTS.md`](./IMPROVEMENTS.md) (the engine-internal
improvement catalog), [`MULTIVARIATE.md`](./MULTIVARIATE.md) /
[`Jordan-1_MULTIVARIATE.md`](./Jordan-1_MULTIVARIATE.md) (the multivariate
adapter), [`FIREHOSE.md`](./FIREHOSE.md) (network-scale peer data), and
[`RELAY_ARCHITECTURE.md`](./RELAY_ARCHITECTURE.md) (the connection layer).
This doc answers two questions those don't: **how a GPU fits into the
engine** (as a second docker image), and **what order all the open
improvement work should happen in**.

> Status: design only, nothing here is implemented. All file/line references
> verified against the current `unify-relay-engine` working tree; all
> benchmark numbers come from measured runs cited inline (none are estimates
> unless explicitly marked "to be measured").

---

## 1. Where the engine is, and the honest premise

The engine today: one unified `veda.Engine` for both the central-poll and
relay-live paths, a per-stream adapter ladder (starter / ets / xgboost /
timesfm / multivariate), one shared `StreamStore`, a single-worker training
queue. The multivariate adapter (implemented, un-pushed) predicts a target
using 5 peer streams found by random-swap search; FIREHOSE.md designs the
network-scale peer pool it needs.

The premise every decision below flows from, established by measurement:

**Quality — not compute — is the current bottleneck.**

| Measured fact | Source |
|---|---|
| ETS fits cost ~10–60 ms on 80-row windows | `IMPROVEMENTS.md` prototype findings (2026-05-26) |
| TimesFM inference ~0.5 s/forecast on 2 vCPU, flat across context 32→512 | `timesfm/README.md` §4 |
| Naive last-value **beats every model** on the ~90-point daily central streams: naive MAE 180.5 < TimesFM 217.2 ≈ ETS 218.0 < XGB 224.8 | `timesfm/README.md` §4 (300-task backtest) |
| The engine **ships XGB predictions ~4× worse than naive** with no guardrail (naive floor B.5 unimplemented) | `IMPROVEMENTS.md` prototype: naive 88.79 vs XGB 142.22; `engine.py:1190` |
| ETS wins 72/77 streams in the walk-forward | `IMPROVEMENTS.md` prototype findings |

So a GPU does not make today's predictions better, and mostly doesn't make
them meaningfully faster either — a 2-vCPU node already fits ETS in
milliseconds and clears a TimesFM forecast in half a second. **The case for
GPU is capacity for where the engine is going**: firehose-scale multivariate
(batched peer forecasts across hundreds of streams), larger foundation
models (chronos-t5-large is already GPU-gated in code), fine-tuning, and
sub-daily cadences. Section 2 designs that. Section 3 ranks everything else,
with the quality guardrails deliberately first.

---

## 2. GPU: a second image, not a fatter one

### 2.1 Why a separate image (and why the code is already shaped for it)

The CPU/GPU split is fundamentally a **packaging** problem:

- A CUDA-bundled torch wheel is ~1.8–2.5 GB vs ~620 MB for the CPU wheel;
  a naive CUDA image lands at 7.6+ GB vs ~2.9 GB optimized. Forcing that on
  a CPU-majority fleet (the example compose caps nodes at `cpus: "1.3",
  memory: "3.5g"`) is a non-starter.
- The Dockerfile already anticipates this. All three Dockerfiles install
  torch in a **dedicated layer after `requirements.txt`**:
  ```dockerfile
  pip install torch --index-url https://download.pytorch.org/whl/cpu
  pip install timesfm==2.0.1
  ```
  with a comment explaining it exists precisely so timesfm does not drag in
  the ~2 GB CUDA torch. That layer is the seam: a GPU image swaps exactly
  this step and nothing else.
- The engine already tolerates torch being absent (guarded optional imports
  in `adapters/__init__.py`) and already contains one fully GPU-aware
  adapter: `chronos_adapter.py` takes `useGPU`, sets
  `device_map='cuda'|'cpu'`, `torch_dtype=torch.bfloat16`, and picks
  `chronos-t5-large` on GPU vs `-small` on CPU. It is unregistered pending
  exactly this work (`engine.py:37`: *"enable it by adding it back to the
  registry once torch is shipped"*).

### 2.2 The image: `satorinet/satorineuron:gpu`

| Decision | Choice | Why |
|---|---|---|
| Dockerfile | new `Dockerfile.gpu`, copy of `Dockerfile` with the torch layer swapped to `--index-url .../whl/cu126` (or current cu12x) | one-layer diff; strfry build, source copy, entrypoint, HF_HOME all unchanged |
| Base image | keep `python:3.10-slim` | CUDA wheels bundle the CUDA runtime — no `nvidia/cuda` base needed; the host supplies only the NVIDIA driver + nvidia-container-toolkit. Keeps parity with the CPU image |
| Platforms | **amd64 only** | CUDA wheels; arm64 GPU (Jetson) out of scope |
| Expected size | ~3–3.5 GB (vs CPU image today) | torch-cu12x wheel dominates |
| build.sh | new `gpu` target: `:gpu` tag + `:buildcache-gpu` registry cache, `--platform linux/amd64` | mirrors existing `latest`/`slim` targets |
| Extra deps (GPU image only) | `chronos-forecasting` | wakes `XgbChronosAdapter`; not added to the CPU image |

Node opt-in is a compose edit, documented in the example file:

```yaml
services:
  neuron:
    image: satorinet/satorineuron:gpu   # was :latest
    gpus: all                           # or deploy.resources.reservations.devices
    deploy:
      resources:
        limits:
          memory: "6g"                  # was 3.5g; model + CUDA context headroom
```

The update story is untouched: `pull_policy: always` + the neuron's periodic
self-restart (`restartEverythingPeriodic`, `start.py:4870`) re-pulls `:gpu`
exactly like `:latest`.

### 2.3 Engine changes (small; checklist for the implementation round)

1. **Device helper** — `satoriengine` util:
   ```python
   def detectDevice() -> str:   # 'cuda' | 'cpu'
       try:
           import torch
           return 'cuda' if torch.cuda.is_available() else 'cpu'
       except Exception:
           return 'cpu'
   ```
   Config override `engine.device: auto|cpu|cuda` (default `auto`) for
   forcing CPU on a GPU box (debugging) — mirrors `preferred_adapter`.
2. **TimesFM device placement** — `timesfm_adapter.py:53-84`
   (`_ensureModel`) is the only CPU-hardcoded site: it sets
   `torch.set_num_threads(...)` and never places the model on a device.
   External reports indicate the TimesFM 2.5 torch implementation
   auto-moves to CUDA when available — verify at implementation time; the
   change may be near-zero code. Keep `_inference_lock` (one GPU context,
   serialized inference is correct and becomes the natural batching point).
   Raise `per_core_batch_size` (currently 1) on GPU.
3. **Adapter contract: pass the device** — `condition()` today receives
   only `(data, cpu, availableRamGigs)` (`engine.py:1286`); there is no
   hardware notion beyond RAM. Add an optional `device: str` kwarg
   (backwards-compatible — every adapter takes `**kwargs`). Adapters that
   want a GPU gate check `device == 'cuda'` instead of each probing torch
   themselves.
4. **Register Chronos on GPU nodes** — add `XgbChronosAdapter` to
   `ADAPTER_REGISTRY` (it's imported already), fix its dormant conditions
   (`PretrainedChronosAdapter.condition` hard-returns 0.0 with the comment
   *"don't use this, it doesn't learn"*; `XgbChronosAdapter` gates
   `20 ≤ rows < 1000`), and construct with `useGPU = (device == 'cuda')` so
   GPU nodes get `chronos-t5-large`. Gate registration on the `chronos`
   package import guard so the CPU image is unaffected.
5. **VRAM in the resource picture** — the training queue's 500 MB RAM check
   (`queue_manager.py:78`) and the 2 GB TimesFM gate stay; add a
   `torch.cuda.mem_get_info()` check only if/when multiple GPU models
   coexist. Not v1.

Deepcopy safety carries over unchanged: adapter instances must never hold
the torch model (the engine does `copy.deepcopy(self.pilot)`); TimesFM is
reached through class-level singletons and the multivariate adapter through
module-level accessors — the GPU model lives in exactly those places.

### 2.4 What the GPU actually buys

| Unlock | Today (measured, 2 vCPU) | With GPU |
|---|---|---|
| Batched TimesFM throughput | batch-32 ≈ 4.3 s ≈ **134 ms/series** | **to be measured** with the existing `timesfm/bench.py` + `latency_sweep.py` inside the GPU container; transformer inference gains scale with batch size, so the batched path (multivariate peer forecasts, firehose-scale fan-out) is where the win concentrates |
| chronos-t5-large | not runnable (code selects `-small` on CPU) | already wired: `chronos_adapter.py:34` picks `-large` when `useGPU` |
| TimesFM 2.5 inference cost | 2.5 is reported **~15× slower than 2.0** at inference ([timesfm#313](https://github.com/google-research/timesfm/issues/313)); the shipped pin is `timesfm==2.0.1` | if a 2.5 upgrade ever wins on quality, GPU + `torch.compile` is the mitigation that keeps it affordable |
| Fine-tuning / global models | infeasible (XGB univariate fit alone is ~12 s/stream on CPU; transformer fine-tuning is far beyond node budgets) | fine-tuning TimesFM/TTM on Satori data and training pooled cross-stream models (IMPROVEMENTS C.9) become realistic — this is the long-term payoff |
| Sub-daily cadence headroom | "hundreds of streams per 10-min window" on 2 vCPU | thousands; relevant only after firehose-scale multivariate exists |

### 2.5 Risks and honest caveats

| Risk | Assessment |
|---|---|
| **Rewards pay accuracy, not compute** | Scoring is inverse-absolute-error (`scoring/mae.py`); a GPU earns nothing unless it improves MAE or lets a node predict more streams. Until fine-tuning/global models exist, the GPU image is for power users and the team's own aggregator nodes, not the default recommendation |
| Single `_inference_lock` serializes the GPU | Correct for one resident model; contention becomes real only with many multivariate targets — the escape hatch is the cross-target batched peer-forecast service already sketched in MULTIVARIATE.md §5.3 |
| VRAM | TimesFM 200M ≈ 1–2 GB, chronos-t5-large ≈ 1.5 GB (bf16) — any ≥6 GB card is comfortable; both resident simultaneously still fits 8 GB |
| Image pull size | ~3–3.5 GB pulls on every `:gpu` update; registry layer caching (`:buildcache-gpu`) keeps rebuilds cheap, but node bandwidth is a real cost — mention in docs |
| Apple Silicon (`mps`) | dev-box nicety only (the chronos comment already names it); fleet is linux/NVIDIA; out of scope for v1 |
| numpy/pandas pins | the image already resolved the timesfm numpy≥1.26 conflict (root `requirements.txt` pins numpy 1.26.4 + pandas 1.5.3); the GPU image inherits it — no new dependency work |

### 2.6 Verification protocol (when implemented)

1. Build `Dockerfile.gpu`; on a CUDA host run the existing
   `docs/engine/timesfm/bench.py` and `latency_sweep.py` inside the
   container twice — `engine.device: cpu` vs `auto` — and record
   ms/series at batch 1 / 8 / 32. Publish the table next to the CPU
   numbers in `timesfm/README.md` §4.
2. `condition()` matrix: GPU node picks timesfm/chronos where gated; CPU
   image behavior byte-identical to today (guarded imports keep chronos
   invisible).
3. Two-container check (`satori` + `satori-2`): GPU node predicts a relay
   stream end-to-end; `nvidia-smi` shows the resident model; kill the GPU
   (force `device: cpu`) and confirm clean fallback with no engine changes.
4. Pull-size and cold-start timings recorded (cold start today is ~190 s
   incl. weight download; the persisted `HF_HOME` volume makes restarts
   ~3 s — confirm unchanged).

---

## 3. The roadmap: everything else, ranked

Five themes, ordered by measured impact per unit of work. Items reference
their source doc rather than re-designing them here.

### Theme 1 — Quality guardrails (first; tiny code; biggest MAE win)

| Item | Why it's first | Source |
|---|---|---|
| **Naive last-value floor** | The engine today ships XGB predictions measured ~4× worse than naive with nothing stopping it (`producePrediction`, `engine.py:1190`, has no floor). One comparison at serve time bounds worst-case error for every adapter, every cadence | IMPROVEMENTS B.5 — "non-negotiable" |
| **Rolling-origin backtest harness** | One shared one-step-ahead MAE yardstick. Fixes the deeper problem that foundation adapters *never compete on quality*: TimesFM/Chronos `score()` returns `inf` and `compare()` defaults to override, so they're chosen by preference order alone. Also the go/no-go gate the multivariate testground needs, and the instrument for any GPU cost/benefit claim | IMPROVEMENTS B.11 |
| **Fix adapter-selection stickiness** | `chooseAdapter` re-evaluates from scratch on every batch; the in-code TODO (`engine.py:1264`) already states the right design: gather acceptable options, keep the incumbent until it measurably degrades | engine.py TODO |

### Theme 2 — Ship the multivariate arc (designed; sequence it)

1. Multivariate backtest gate (MULTIVARIATE.md roadmap 3) using the Theme-1
   harness — go/no-go on real streams.
2. Push the 7 local commits; dev-neuron soak with
   `preferred_adapter: multivariate`.
3. Relay Phase 1 stability fixes (RELAY_ARCHITECTURE.md §3) — the firehose
   precondition.
4. Firehose (~75 lines, FIREHOSE.md) — the peer pool goes network-scale.
5. Publisher-served history backfill (kinds 34610/24610, MULTIVARIATE.md
   §5.1) — removes the weeks-long warm-up for slow streams.

### Theme 3 — Scale the framework (IMPROVEMENTS Layers A/B, GPU-adjacent)

- `StreamProfile` (A.1) → cadence-aware windows (B.1) → budget-aware
  selection (B.2): bounds memory and fit time per cadence class.
- `update(new_rows)` + model persistence (B.4/B.8): incremental fits,
  warm restarts.
- **Resource-aware training queue** (B.10): priorities by freshness
  deadline, per-adapter concurrency caps, multi-worker as config — and this
  is where a GPU becomes a *schedulable resource* (a `device`-tagged queue
  lane) rather than an adapter-private detail.
- **StreamStore retention**: today the store never prunes and has only its
  PK index; harmless at ~80 streams, but the 500-stream firehose makes a
  keep-last-N pruner and a size gauge necessary. (FIREHOSE.md lists this as
  an explicit follow-up.)

### Theme 4 — Better models

| Item | Note |
|---|---|
| Seasonal-naive + ensemble adapters (C.7/C.6) | ETS wins 72/77 and naive wins overall on dailies — a naive/seasonal/ETS blend with inverse-MAE weights from the Theme-1 harness is the cheapest expected MAE win after the floor |
| **Free probabilistic forecasts** | TimesFM is already compiled with `use_continuous_quantile_head=True` (`timesfm_adapter.py:73-79`) — quantiles are computed on every forecast and currently discarded. Surfacing them costs almost nothing and positions the network for calibration-aware scoring (C.4) |
| Global / pooled models (C.9) | One model over all streams of a class with stream-id as a feature; foundation models are already global by design — the multivariate peer machinery is the beachhead. GPU makes the training side practical |
| TimesFM fine-tuning | The genuine GPU payoff: a Satori-tuned checkpoint shared per class of streams. Gate on the harness showing zero-shot is being left on the table |
| Online learning (C.10) | For fast streams, replace refit-every-N with per-row updates |
| TimesFM 2.0 vs 2.5 | Shipped pin is 2.0.1; 2.5 reportedly ~15× slower at inference. Re-evaluate only with harness numbers on Satori data, and only alongside GPU/compile mitigation |

### Theme 5 — Network-level intelligence

- **Close the scoring feedback loop**: the network already computes
  per-peer skill metrics centrally; the engine never sees its own live
  network MAE. Feeding it back (adapter choice, drift detection B.9) aligns
  training with what actually pays.
- **Calibrated uncertainty as a first-class output** if bounty scoring ever
  rewards it — the quantile head above is the ready input.
- **Meta-model over peer predictions**: research-only flag. `_pred` streams
  are excluded from the multivariate pool for circularity reasons
  (FIREHOSE.md guard 2); any design that consumes other nodes' predictions
  needs an explicit anti-feedback story first.

---

## 4. Suggested sequence

| Phase | Work | Depends on |
|---|---|---|
| 1 | Naive floor + backtest harness + sticky selection (Theme 1) | nothing — do now |
| 2 | Multivariate backtest gate → push → soak (Theme 2.1–2.2) | harness |
| 3 | Relay Phase 1 → firehose → StreamStore retention (Theme 2.3–2.4, 3) | Phase 2 verdict |
| 4 | **GPU image** (`Dockerfile.gpu`, device plumbing, chronos registration, §2.6 benches) | worth doing once Phase 3 makes batched foundation-model inference a real workload; independent enough to parallelize with Phase 3 |
| 5 | Ensemble/seasonal-naive, quantile surfacing, queue upgrade (Themes 3/4 quick wins) | harness |
| 6 | Backfill protocol, fine-tuning, global models (Themes 2.5, 4 big bets) | GPU image + harness evidence |

The through-line: **measure first (harness), guard second (floor), scale
third (multivariate/firehose), and buy hardware only when the workload —
not the ambition — demands it.**

---

## 5. Verification

- Every number in this doc traces to a measured source: `IMPROVEMENTS.md`
  prototype findings (77-stream walk-forward), `timesfm/README.md` §4–5
  benchmarks (2 vCPU container), or cited external issues. GPU speedups are
  deliberately stated as "to be measured" — the acceptance criterion for
  §2 is the §2.6 protocol, not a promised multiplier.
- Doc-level checks for reviewers: the torch-layer seam
  (`Dockerfile`, torch/timesfm step), the CPU-hardcoded TimesFM load
  (`timesfm_adapter.py:53-84`), the GPU-ready chronos path
  (`chronos_adapter.py:24-42`), the missing device in the adapter contract
  (`engine.py:1286`), and the absent naive floor (`engine.py:1190`) are all
  verifiable by opening those files at those locations.
