"""Latency sweep: TimesFM single-forecast wall time vs context length and
torch.compile, at a fixed CPU-thread cap. Answers 'seconds per prediction on N vCPU'."""
import os, time, numpy as np, torch, timesfm

THREADS = int(os.environ.get("TFM_THREADS", "2"))
torch.set_num_threads(THREADS)
torch.set_float32_matmul_precision("high")

def bench(compile_on, contexts=(32, 64, 96, 128, 256, 512), reps=15):
    print(f"\n### torch_compile={compile_on}, threads={THREADS}")
    m = timesfm.TimesFM_2p5_200M_torch.from_pretrained(
        "google/timesfm-2.5-200m-pytorch", torch_compile=compile_on)
    m.compile(timesfm.ForecastConfig(
        max_context=512, max_horizon=128, per_core_batch_size=1,
        normalize_inputs=True, use_continuous_quantile_head=True, fix_quantile_crossing=True))
    rng = np.sin(np.linspace(0, 40, 512)).astype("float32") + 1000.0
    for c in contexts:
        ctx = rng[-c:]
        for _ in range(3): m.forecast(horizon=1, inputs=[ctx])  # warm
        ts = []
        for _ in range(reps):
            t = time.time(); m.forecast(horizon=1, inputs=[ctx]); ts.append(time.time()-t)
        ts.sort()
        print(f"  ctx={c:<4} median={ts[len(ts)//2]*1000:7.0f}ms  p90={ts[int(len(ts)*0.9)]*1000:7.0f}ms  min={ts[0]*1000:7.0f}ms")

bench(compile_on=True)
bench(compile_on=False)
