"""Just the real-stream XGB +delta validation. Faster iteration than the
full bench."""
from __future__ import annotations
import sys
sys.path.insert(0, '/Satori/Engine/testground')
from ets_warmstart_bench import scenario_xgb_delta_real_streams

if __name__ == '__main__':
    scenario_xgb_delta_real_streams(max_streams=20, holdout_frac=0.20, min_history=30)
    print('\ndone')
