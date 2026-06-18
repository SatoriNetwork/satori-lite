"""
TimesFM vs ETS/XGB bake-off on real Satori data — runs INSIDE the satori-dev
docker container. Two-pass to avoid the numpy conflict (adapters need numpy 1.24
/ pandas 1.5; timesfm needs numpy >=1.26 in its own venv):

  MODE=classic  python bench.py   # naive/ETS/XGB in container python  -> classic.json
  MODE=timesfm  python bench.py   # TimesFM in isolated venv           -> timesfm.json (+ latency)
  MODE=report   python bench.py   # merge + metrics table

Metric = MAE (matches network reward: neuron-lite/scoring/mae.py, central score.py).

Env paths (container defaults shown):
  ENGINE_DB   /Satori/Engine/db/engine.db
  NETWORK_DB  /Satori/Neuron/data/network.db
  OUT_DIR     /bench/out
  TFM_THREADS torch CPU threads (default 2)   TFM_CONTEXT max context (default 512)
  PANEL_MIN   min points per stream (default 40)
"""
import os, sys, json, time, sqlite3
import numpy as np

ENGINE_DB = os.environ.get("ENGINE_DB", "/Satori/Engine/db/engine.db")
NETWORK_DB = os.environ.get("NETWORK_DB", "/Satori/Neuron/data/network.db")
OUT_DIR = os.environ.get("OUT_DIR", "/bench/out")
MODE = os.environ.get("MODE", "report")
CONTEXT = int(os.environ.get("TFM_CONTEXT", "512"))
THREADS = int(os.environ.get("TFM_THREADS", "2"))
PANEL_MIN = int(os.environ.get("TFM_PANEL_MIN", "40"))
MAX_STREAMS = int(os.environ.get("MAX_STREAMS", "30"))   # cap panel breadth (0=all)
MAX_HOLDOUT = int(os.environ.get("MAX_HOLDOUT", "10"))   # walk-forward steps per stream
os.makedirs(OUT_DIR, exist_ok=True)

# ---------------------------------------------------------------- data + tasks
def load_engine_panel():
    con = sqlite3.connect(ENGINE_DB)
    rows = con.execute(
        "SELECT s.name, o.epoch, o.value FROM observations o "
        "JOIN streams s ON s.uuid=o.stream_uuid ORDER BY s.name, o.epoch").fetchall()
    con.close()
    series = {}
    for name, epoch, value in rows:
        series.setdefault(name, []).append((float(epoch), float(value)))
    out = {}
    for name, pts in series.items():
        if len(pts) >= PANEL_MIN:
            out[name] = (np.array([p[0] for p in pts]), np.array([p[1] for p in pts]))
    return out

def _parse_blob(blob):
    try:
        j = json.loads(blob)
        return float(j["value"]) if isinstance(j, dict) else float(j)
    except Exception:
        try: return float(blob)
        except Exception: return None

def load_usdt():
    con = sqlite3.connect(NETWORK_DB)
    rows = con.execute("SELECT observed_at, value FROM observations "
                       "WHERE stream_name='satori-usdt-test' ORDER BY observed_at").fetchall()
    con.close()
    ep, vv = [], []
    for oa, blob in rows:
        v = _parse_blob(blob)
        if v is not None and np.isfinite(v):
            ep.append(float(oa)); vv.append(v)
    return np.array(ep), np.array(vv)

def datasets():
    panel = load_engine_panel()
    usdt_ep, usdt_v = load_usdt()
    return [("engine-panel", panel),
            ("usdt-test", {"satori-usdt-test": (usdt_ep, usdt_v)})]

def build_tasks(ds_name, panel, holdout_frac=0.2, max_holdout=None, min_hist=24):
    """Deterministic task list. key = 'ds|stream|i'. Returns list of (key, ep, v, actual)."""
    if max_holdout is None:
        max_holdout = MAX_HOLDOUT
    tasks = []
    names = sorted(panel)
    if MAX_STREAMS and ds_name == "engine-panel":
        # deepest streams first, deterministic
        names = sorted(names, key=lambda nm: (-len(panel[nm][1]), nm))[:MAX_STREAMS]
    for name in names:
        ep, vv = panel[name]
        n = len(vv)
        h = min(max_holdout, max(3, int(round(n * holdout_frac))))
        start = max(n - h, min_hist)
        for i in range(start, n):
            tasks.append((f"{ds_name}|{name}|{i}", ep[:i], vv[:i], float(vv[i])))
    return tasks

# ---------------------------------------------------------------- classic pass
def run_classic():
    sys.path.insert(0, os.environ.get("SATORI_ENGINE", "/Satori/Engine"))
    sys.path.insert(0, os.environ.get("SATORI_LIB", "/Satori/Lib"))
    import pandas as pd
    from satoriengine.veda.adapters.ets.ets_model import ETSAdapter
    from satoriengine.veda.adapters.xgboost.xgb import XgbAdapter

    def frame(ep, v, with_id=False):
        d = {"date_time": pd.to_datetime(ep, unit="s"), "value": v}
        if with_id: d["id"] = "s"
        return pd.DataFrame(d)

    def ets_pred(ep, v):
        out = ETSAdapter().predict(frame(ep, v))  # cold-fits internally each call (cheap)
        return float(out["pred"].iloc[0]) if out is not None and "pred" in out else None

    # group tasks by stream so XGB is fit ONCE per stream (production pattern:
    # periodic training, cheap prediction), trained on the pre-holdout prefix only.
    from collections import OrderedDict
    groups = OrderedDict()
    for ds_name, panel in datasets():
        for key, ep, v, actual in build_tasks(ds_name, panel):
            groups.setdefault(key.rsplit("|", 1)[0], []).append((key, ep, v, actual))
    total = sum(len(g) for g in groups.values())
    print(f"[classic] {len(groups)} streams, {total} tasks "
          f"(MAX_STREAMS={MAX_STREAMS} MAX_HOLDOUT={MAX_HOLDOUT})", flush=True)

    preds = {}
    t0 = time.time(); n_done = 0
    for gi, (stream, tasks) in enumerate(groups.items(), 1):
        tasks.sort(key=lambda t: int(t[0].rsplit("|", 1)[1]))
        # fit XGB once on the earliest prefix (data before the holdout window)
        xa = XgbAdapter()
        try:
            ep0, v0 = tasks[0][1], tasks[0][2]
            xa.fit(frame(ep0, v0, with_id=True))
            xfit_ok = xa.model is not None
        except Exception:
            xfit_ok = False
        for key, ep, v, actual in tasks:
            rec = {"actual": actual, "naive": float(v[-1])}
            try: rec["ETS"] = ets_pred(ep, v)
            except Exception: rec["ETS"] = None
            try:
                out = xa.predict(frame(ep, v, with_id=True)) if xfit_ok else None
                rec["XGB"] = float(out["pred"].iloc[0]) if out is not None and "pred" in out else None
            except Exception:
                rec["XGB"] = None
            preds[key] = rec; n_done += 1
        print(f"  [{gi}/{len(groups)}] {stream} ({n_done}/{total}, {time.time()-t0:.0f}s)", flush=True)
    json.dump(preds, open(f"{OUT_DIR}/classic.json", "w"))
    print(f"[classic] done {len(preds)} tasks in {time.time()-t0:.1f}s -> {OUT_DIR}/classic.json", flush=True)

# ---------------------------------------------------------------- timesfm pass
def run_timesfm():
    import torch, timesfm
    torch.set_num_threads(THREADS)
    torch.set_float32_matmul_precision("high")
    t0 = time.time()
    model = timesfm.TimesFM_2p5_200M_torch.from_pretrained("google/timesfm-2.5-200m-pytorch")
    model.compile(timesfm.ForecastConfig(
        max_context=CONTEXT, max_horizon=128, per_core_batch_size=32,
        normalize_inputs=True, use_continuous_quantile_head=True, fix_quantile_crossing=True))
    print(f"[timesfm] loaded+compiled in {time.time()-t0:.1f}s, threads={torch.get_num_threads()}")

    def clean(p):
        a = np.asarray(p[-CONTEXT:], dtype="float32"); a = a[np.isfinite(a)]
        return a if a.size else np.zeros(1, dtype="float32")

    keys, prefixes = [], []
    for ds_name, panel in datasets():
        for key, ep, v, actual in build_tasks(ds_name, panel):
            keys.append(key); prefixes.append(clean(v))

    preds = {}
    t0 = time.time(); CH = 32
    for i in range(0, len(prefixes), CH):
        batch = prefixes[i:i+CH]
        pt, _ = model.forecast(horizon=1, inputs=batch)
        if pt.shape[0] == len(batch):
            for j, k in enumerate(keys[i:i+CH]): preds[k] = float(pt[j, 0])
        else:
            for k, arr in zip(keys[i:i+CH], batch):
                pt1, _ = model.forecast(horizon=1, inputs=[arr]); preds[k] = float(pt1[0, 0])
    json.dump(preds, open(f"{OUT_DIR}/timesfm.json", "w"))
    print(f"[timesfm] {len(preds)} forecasts in {time.time()-t0:.1f}s")

    # latency micro-benchmark (single-series, realistic per-prediction cost)
    base = load_usdt()[1]
    ctx = np.asarray((base if len(base) >= CONTEXT else
                      np.tile(base, CONTEXT // max(1, len(base)) + 1))[-CONTEXT:], dtype="float32")
    for _ in range(3): model.forecast(horizon=1, inputs=[ctx])  # warm
    lat = {}
    for horizon in (1, 12, 64):
        ts = []
        for _ in range(20):
            t = time.time(); model.forecast(horizon=horizon, inputs=[ctx]); ts.append(time.time()-t)
        ts.sort()
        lat[horizon] = {"median_ms": ts[len(ts)//2]*1000, "p90_ms": ts[int(len(ts)*0.9)]*1000,
                        "min_ms": ts[0]*1000}
        print(f"[latency] horizon={horizon:<3} median={lat[horizon]['median_ms']:.1f}ms "
              f"p90={lat[horizon]['p90_ms']:.1f}ms min={lat[horizon]['min_ms']:.1f}ms (ctx={len(ctx)})")
    json.dump({"threads": THREADS, "context": CONTEXT, "latency": lat},
              open(f"{OUT_DIR}/latency.json", "w"))

# ---------------------------------------------------------------- report
def run_report():
    classic = json.load(open(f"{OUT_DIR}/classic.json"))
    tfm = json.load(open(f"{OUT_DIR}/timesfm.json"))
    methods = ["naive", "ETS", "XGB", "TimesFM"]
    for ds_filter in ("engine-panel", "usdt-test"):
        keys = [k for k in classic if k.startswith(ds_filter) and k in tfm]
        def val(k, m): return tfm[k] if m == "TimesFM" else classic[k].get(m)
        valid = [k for k in keys
                 if all(val(k, m) is not None and np.isfinite(val(k, m)) for m in methods)]
        print(f"\n=== {ds_filter}: {len(valid)}/{len(keys)} tasks scored (all methods finite) ===")
        rows = []
        for m in methods:
            ae, pe = [], []
            for k in valid:
                a = classic[k]["actual"]; p = val(k, m)
                ae.append(abs(p - a))
                if a != 0: pe.append(abs(p - a) / abs(a))
            rows.append((m, float(np.mean(ae)) if ae else float("nan"),
                         float(np.sqrt(np.mean(np.square(ae)))) if ae else float("nan"),
                         100*float(np.mean(pe)) if pe else float("nan")))
        rows.sort(key=lambda r: r[1])
        print(f"  {'method':<10}{'MAE':>16}{'RMSE':>16}{'MAPE%':>10}")
        for m, mae, rmse, mp in rows:
            print(f"  {m:<10}{mae:>16.6f}{rmse:>16.6f}{mp:>10.3f}")
    try:
        lat = json.load(open(f"{OUT_DIR}/latency.json"))
        print(f"\n=== TimesFM CPU latency (threads={lat['threads']}, context={lat['context']}) ===")
        for h, d in lat["latency"].items():
            print(f"  horizon={h:<3} median={d['median_ms']:.1f}ms  p90={d['p90_ms']:.1f}ms  min={d['min_ms']:.1f}ms")
    except FileNotFoundError:
        pass

# ---------------------------------------------------------------- main
if __name__ == "__main__":
    print(f"MODE={MODE} CONTEXT={CONTEXT} THREADS={THREADS} PANEL_MIN={PANEL_MIN}")
    {"classic": run_classic, "timesfm": run_timesfm, "report": run_report}[MODE]()
