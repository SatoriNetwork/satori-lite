# TimesFM as a Satori forecasting adapter

Research, benchmark, requirements, and scaling design for adding Google's
**TimesFM** time-series foundation model to the Satori engine as a pluggable,
opt-in adapter. All numbers here were measured inside the `satori-dev` docker
container (image `satorinet/satori-lite:dev`), with the container capped to
`--cpus=2` (aarch64 Linux) to simulate a small node.

Reproducible harness lives next to this doc: `bench.py`, `latency_sweep.py`.

---

## 1. Summary / TL;DR

- **CPU-only works.** Use TimesFM **2.5-200M**, PyTorch backend. It auto-selects
  CPU when no CUDA is present.
- **Latency: ~0.5 s per single forecast on 2 vCPU** with `per_core_batch_size=1`,
  flat across context length (32 to 512). Trivially within a 10-minute cadence.
- **RAM ~1.5 GB resident**, ~800 MB weights downloaded once.
- **Accuracy on current data: not a win.** Zero-shot TimesFM ties ETS, beats XGB,
  and loses to naive last-value on our ~90-point daily crypto streams. Daily
  near-random-walk series are its worst case. It should ship **default off**,
  opt-in per stream.
- **Scaling win: zero-shot = no per-stream training cost.** Unlike XGB (~12 s to
  fit per stream), TimesFM only ever does inference, so it scales far better as
  streams and history grow. Batch many streams into one `forecast()` call.

---

## 2. Model choice

| Model | Params | Backend | Context limit | Why / why not |
|---|---|---|---|---|
| **timesfm-2.5-200m-pytorch** | 200M | PyTorch | 16,384 | **Chosen.** Smallest, newest, best zero-shot, safe CPU path. |
| timesfm-2.0-500m-pytorch | 500M | PyTorch | 2,048 | Larger, needs >=16 GB RAM on CPU. Overkill. |
| timesfm-2.5-200m-flax | 200M | JAX/Flax | 16,384 | JAX-on-CPU has a known JIT bug (repo issue #51). Avoid. |

PyTorch over JAX specifically because JAX-on-CPU has had checkpoint-load JIT
failures; the torch path avoids the XLA compile stalls.

### Inference API (2.5)
```python
import numpy as np, torch, timesfm
torch.set_num_threads(N_VCPU)
model = timesfm.TimesFM_2p5_200M_torch.from_pretrained(
    "google/timesfm-2.5-200m-pytorch")           # ~800 MB, cached in HF_HOME
model.compile(timesfm.ForecastConfig(
    max_context=512, max_horizon=128,
    per_core_batch_size=1,                        # 1 for low-latency single stream
    normalize_inputs=True,                        # internal RevIN, no manual scaling
    use_continuous_quantile_head=True, fix_quantile_crossing=True))
point, quantiles = model.forecast(horizon=1, inputs=[series_1d_float32])
# point: (B, horizon)   quantiles: (B, horizon, 10)
```
- Input is `list[np.ndarray]`, one 1-D univariate series per element. No frequency
  codes in 2.5. Leading NaNs stripped and internal NaNs interpolated automatically;
  strip trailing NaNs yourself.
- Series longer than `max_context` are truncated to the last `max_context`; shorter
  are left-padded.

---

## 3. Requirements

### Hardware

| Resource | Minimum | Recommended | Notes |
|---|---|---|---|
| CPU | 2 vCPU | 2-4 vCPU | 2 vCPU -> ~0.5 s/forecast (batch=1). More cores help bulk batches, not single-stream latency. |
| RAM | 2 GB free | 4 GB system | Model ~1.5 GB resident, loaded once. |
| Disk | ~1 GB | - | ~800 MB weights to `HF_HOME` (`/Satori/Neuron/models/huggingface`). |

### Runtime config (required to hit these numbers)
- `per_core_batch_size=1` for per-stream latency. Batch=32 makes a single call
  ~4.3 s because it does 32-wide work.
- `max_context=512`, `max_horizon=128`. `torch_compile` makes no measurable
  difference on CPU; either is fine.
- `torch.set_num_threads = vCPU count`.
- **CPU torch wheel only**: `pip install torch --index-url
  https://download.pytorch.org/whl/cpu`. The default Linux wheel drags in the
  full CUDA toolkit (~2 GB of nvidia-* packages) for nothing on a CPU box.
- Load the model once and keep it resident. Cold start ~190 s including download;
  warm load ~3 s.

### Data-point requirement
- **Hard floor: >= 32 points** to run at all (below that it is mostly zero-padding;
  fall back to the linear/ETS path).
- **Sensible floor: >= 64 points.**
- **Where it earns its keep: a few hundred+ points at sub-daily frequency.** On
  ~90-point daily streams it ties ETS and loses to naive, so do not make it the
  default.

---

## 4. Benchmark

### Method
Walk-forward 1-step backtest inside the container. ETS/XGB run in the container's
native Python (numpy 1.24 / pandas 1.5); TimesFM runs in an isolated venv (numpy
2.x) to avoid the version conflict. Predictions are merged on a shared,
deterministic task set. Metric = **MAE**, which matches the network reward
(`neuron-lite/scoring/mae.py`, `central-lite/src/scripts/score.py` reward by
absolute distance). XGB is fit once per stream on the pre-holdout prefix and
reused (production pattern), since refitting per step is both unrealistic and
~12 s each.

Data used (the only real series available in-repo):
- `engine-lite/db/engine.db` -> `observations`: 80 crypto streams, ~88-96 pts each
  (~5 months, ~daily). 30 deepest streams x 10 walk-forward steps = 300 tasks.
- `neuron-lite/data/network.db` -> `satori-usdt-test`: 640 pts (longest single
  series; used mainly as a length/latency probe, not a meaningful accuracy test
  since a stablecoin barely moves).

### Accuracy

engine-panel (300 tasks, volatile daily crypto):

| method | MAE | RMSE | MAPE% |
|---|---|---|---|
| naive (last value) | **180.5** | 790.4 | 4.02 |
| TimesFM | 217.2 | 951.2 | 6.73 |
| ETS | 218.0 | 1071.4 | 4.49 |
| XGB | 224.8 | 937.8 | 5.36 |

Interpretation: zero-shot TimesFM is competitive with ETS and beats XGB, but
**naive wins**. Daily crypto is near-random-walk, where last-value is famously
hard to beat, and ~90 points is far too short for a long-context foundation
model. This is TimesFM's worst case, not a verdict on the model.

### Latency (single forecast, 2 vCPU, `per_core_batch_size=1`)

| context | median | p90 |
|---|---|---|
| 32 | ~520 ms | ~575 ms |
| 96 | ~520 ms | ~580 ms |
| 512 | ~520 ms | ~590 ms |

Flat across context. `torch_compile` on/off: no meaningful difference on CPU.
With `per_core_batch_size=32`, a single call is ~4.3 s (32-wide work) -> use
batch size 1 for single-stream latency, large batch only for bulk throughput.

---

## 5. Scaling: large history + 10-minute cadence + many streams

Key reframe: **dataset size is not the inference-cost driver, context window is.**
A 20k-row stream and a 90-row stream cost the same per forecast, because the model
only consumes the last `max_context` points. 20k rows is a storage/loading
concern, not a compute concern.

- **20k rows of history**: slice `series[-max_context:]`. Keep a rolling
  in-memory buffer (~2x context) per stream instead of re-reading the full table
  each tick. Optionally feed more context (512 -> 2048) or multi-resolution
  (recent fine + older downsampled) if accuracy benefits.
- **10-min cadence, one stream**: ~0.5 s every 600 s, <0.1% utilization. Non-issue.
- **The real axis is number of streams x cadence.** Batch all due streams into one
  `forecast()` call. Measured: batch of 32 ~4.3 s total = **~134 ms/series**, ~4x
  more efficient than solo. On 2 vCPU that is hundreds of streams per 10-min window.
- **Zero-shot is the scaling advantage**: no per-stream fit cost. XGB's ~12 s fit
  per stream makes periodic retraining across many streams infeasible; TimesFM
  stays flat as streams/data grow.

### Suggested prediction loop
```
keep model resident (load once)
keep a rolling buffer per stream (last ~2*context points, in memory)
every tick:
    due   = [streams whose next prediction is owed]
    batch = [buf[s][-context:] for s in due]          # chunk to MAX_BATCH (e.g. 64)
    points, _ = model.forecast(horizon=H, inputs=batch)   # ONE call per chunk
    distribute points[i] -> stream due[i]
```
Bound memory by capping retained history per stream; you do not need 20k rows in
RAM if context is 512-2048.

---

## 6. Integration as an adapter

The engine already has a foundation-model adapter precedent:
`engine-lite/adapters/xgbchronos/chronos_adapter.py` (Chronos). Model the TimesFM
adapter on it.

- New `TimesFmAdapter` in `engine-lite/adapters/timesfm/`:
  - Load model once under a class-level lock (Chronos pattern), keep resident.
  - `predict(self, data: pd.DataFrame) -> pd.DataFrame` returning
    `{'date_time': [next_ts], 'pred': [value]}` (engine checks `'pred' in columns`).
  - Context slice like Chronos: `series[-self.contextLen:]`.
  - `condition()` returns 0 unless: torch importable AND RAM >= 2 GB AND series
    >= 64 points, so it auto-disables on thin nodes and short streams.
- Register in `engine.py ADAPTER_REGISTRY` and add an optional try/except import
  in `adapters/__init__.py` (same pattern as the optional Chronos import).
- Config key `engine.preferred_adapter = timesfm` (default unchanged). When TimesFM
  is unavailable or ineligible, the engine falls back to the current predictor.

### Blocker to resolve before shipping
**numpy conflict**: TimesFM needs numpy >= 1.26; the neuron pins numpy 1.24 /
pandas 1.5 (pandas 1.5 breaks on numpy 2.x). Options:
1. Bump the image to a numpy/pandas pair compatible with both (e.g. numpy 1.26 +
   pandas 1.5.3 or 2.x) and re-verify ETS/XGB, or
2. Run TimesFM out-of-process (small local predictor service) to isolate deps at
   the cost of an IPC hop.

---

## 7. Reproduction

Harness: `bench.py` (modes: classic / timesfm / report), `latency_sweep.py`.

```bash
cd repos/satori-lite
docker run -d --name satori-bench --cpus=2 \
  -v "$(pwd)/../satorilib/src:/Satori/Lib" -v "$(pwd)/neuron-lite:/Satori/Neuron" \
  -v "$(pwd)/engine-lite:/Satori/Engine" \
  -v "$(pwd)/docs/engine/timesfm:/bench" \
  -e PYTHONPATH="/Satori/Lib:/Satori/Engine:/Satori" \
  satorinet/satori-lite:dev sleep infinity

# isolated venv for TimesFM (CPU wheel recommended for real deploys)
docker exec satori-bench python -m venv /opt/tfmvenv
docker exec satori-bench /opt/tfmvenv/bin/pip install 'timesfm[torch]' pandas numpy

# three passes (classic uses native python; timesfm uses the venv)
docker exec -e MODE=classic satori-bench python -u /bench/bench.py
docker exec -e MODE=timesfm -e TFM_THREADS=2 satori-bench /opt/tfmvenv/bin/python -u /bench/bench.py
docker exec -e MODE=report  satori-bench /opt/tfmvenv/bin/python    /bench/bench.py

# latency sweep across context lengths
docker exec -e TFM_THREADS=2 satori-bench /opt/tfmvenv/bin/python -u /bench/latency_sweep.py
```

Env knobs (`bench.py`): `MODE`, `TFM_THREADS` (default 2), `TFM_CONTEXT` (512),
`PANEL_MIN` (40), `MAX_STREAMS` (30), `MAX_HOLDOUT` (10), `ENGINE_DB`, `NETWORK_DB`,
`OUT_DIR`.

---

## 8. Decision

Cheap to run (2 vCPU / 2 GB / ~0.5 s), safe as an opt-in toggle, and it scales
better than ETS/XGB because it needs no training. But on today's short daily data
it does not beat naive, so ship it **default off** and let users enable it for
streams with real history and sub-daily frequency. Re-benchmark on a volatile pair
at 1-min or 1-hour over a few thousand points before considering it as a default.
