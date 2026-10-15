"""
One-off diagnostic (not part of the pipeline): 3-arm controlled ablation
of RISK_BUFFER_AT_CERTAINTY, gated version, against the Option B
production checkpoint. Same methodology as PROJECT_STATUS.txt 0H.5 (one
process, one origin, one seed, production rolling-horizon settings:
commit=300, lookahead==commit, pop=50, gen=50, polish=400) -- the ONLY
variable across arms is how apply_risk_buffer behaves.

WHY A THIRD ARM: 0H.5 already tested disabled (0.0) vs. broadly-enabled
(0.5) under the current real (if modest) delay predictions and found
broad enable hurts on-time/makespan/GA-margin -- diagnosed as "the
classifier still pads a lot of low-risk operations close to as much as
genuinely high-risk ones." This ablation adds a GATED arm: same 0.5
cushion formula, but only applied to operations at or above a confidence
threshold (~0.14, the middle of the ~0.13-0.15 band the confidence-gate
analysis in scratch/delay_calibration_and_confidence_gate.py identified
as roughly the top 10-20% most confident predictions) -- everyone else
gets zero cushion. This directly tests whether 0H.5's failure was about
the BUFFER CONCEPT or about applying it with too little discriminative
signal behind it, now that there is real (if modest) signal to gate on.

  arm A: disabled     -- buffer_mins = duration_mins always (current default)
  arm B: broad_0.5    -- buffer_mins = duration * (1 + p*0.5) for EVERYONE
                          (replicates 0H.5's enabled arm under the CURRENT model)
  arm C: gated_0.5     -- buffer_mins = duration * (1 + p*0.5) IF p >= threshold,
                          else duration_mins unchanged

Monkeypatches ga.schedule_utils.apply_risk_buffer, ga.fitness.apply_risk_buffer,
and agent.decision_engine.apply_risk_buffer (all three hold their own
reference to the function after `from ... import apply_risk_buffer`, so all
three must be repointed for a correct patch) between arms. Graph, real GNN
predictions, operator index, and capacity are built ONCE and reused across
all three arms -- only the buffer function differs.
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
from datetime import datetime

import torch
import pandas as pd

import ga.schedule_utils as su
import ga.fitness as gf
import agent.decision_engine as de

from database.db import get_engine
from database.queries import (
    fetch_concurrent_operator_capacity, fetch_assignable_operators,
    fetch_line_factory_map,
)
from graph.graph_builder import FactoryGraphBuilder
from models.gnn import MultiTaskFactoryGNN
from agent.decision_engine import AIDecisionEngine
from ga.rolling_horizon import run_rolling_horizon
from utils.config import OUTPUT_DIR, GNN_HIDDEN_CHANNELS

GATE_THRESHOLD = 0.14  # middle of the ~0.13-0.15 band identified separately

# 0H.5's exact production settings, hardcoded here (not read from current
# config) so this ablation cannot silently drift from that methodology if
# config defaults change later.
COMMIT_SIZE = 300
LOOKAHEAD_SIZE = None  # None -> lookahead_size = commit_size inside run_rolling_horizon (L==C)
POP_SIZE = 50
GENERATIONS = 50
POLISH_EVALS = 400
SEED = 42


def _clamp(p):
    return min(max(float(p), 0.0), 1.0)


def buffer_disabled(duration_mins, delay_prob):
    return duration_mins


def buffer_broad_0_5(duration_mins, delay_prob):
    p = _clamp(delay_prob)
    return duration_mins * (1.0 + p * 0.5)


def buffer_gated_0_5(duration_mins, delay_prob):
    p = _clamp(delay_prob)
    if p >= GATE_THRESHOLD:
        return duration_mins * (1.0 + p * 0.5)
    return duration_mins


ARMS = {
    'A_disabled': buffer_disabled,
    'B_broad_0.5': buffer_broad_0_5,
    'C_gated_0.5': buffer_gated_0_5,
}


def patch_buffer(fn):
    su.apply_risk_buffer = fn
    gf.apply_risk_buffer = fn
    de.apply_risk_buffer = fn


print("="*70)
print("SETUP (shared across all 3 arms)")
print("="*70)

builder = FactoryGraphBuilder()
graph, _ = builder.build_graph()
df_lines = pd.read_sql(
    "SELECT line_id, department_id, line_number, target_efficiency_percentage, is_active "
    "FROM Production_Lines WHERE is_active = 1", get_engine())

model = MultiTaskFactoryGNN(metadata=graph.metadata(), hidden_channels=GNN_HIDDEN_CHANNELS)
model.load_state_dict(torch.load(os.path.join(OUTPUT_DIR, "checkpoints", "gnn_multitask_latest.pth")))
model.eval()
with torch.no_grad():
    out = model(graph.x_dict, graph.edge_index_dict, graph['operation'].type_idx)
delay = torch.sigmoid(out['delay']).squeeze(-1)
preds_all = {k: {'delay': delay[i].item(), 'priority': out['priority'][i].item()}
             for i, k in enumerate(builder.op_map.keys())}

dvals = sorted(p['delay'] for p in preds_all.values())
n_at_or_above = sum(1 for v in dvals if v >= GATE_THRESHOLD)
print(f"GNN delay probability range: {min(dvals):.4f} - {max(dvals):.4f}")
print(f"gate threshold {GATE_THRESHOLD}: {n_at_or_above}/{len(dvals)} operations "
      f"({100.0 * n_at_or_above / len(dvals):.1f}%) would be gated IN (cushion applied)")

capacity = fetch_concurrent_operator_capacity()

# ONE fixed origin, shared by all 3 arms -- 0H.5's "one process, one origin, one seed"
ORIGIN = datetime.now()
print(f"fixed origin timestamp: {ORIGIN.isoformat()}")

eng = AIDecisionEngine()
op_index = eng._build_operator_index(
    fetch_assignable_operators(), df_lines, fetch_line_factory_map())

print(f"commit={COMMIT_SIZE}  lookahead={LOOKAHEAD_SIZE or COMMIT_SIZE}  "
      f"pop={POP_SIZE}  gen={GENERATIONS}  polish={POLISH_EVALS}  seed={SEED}")

results = {}
for arm_name, buffer_fn in ARMS.items():
    print()
    print("="*70)
    print(f"ARM: {arm_name}")
    print("="*70)
    patch_buffer(buffer_fn)

    t0 = time.time()
    rolling_result = run_rolling_horizon(
        builder.df_routings, builder.df_machines, df_lines, preds_all, capacity, ORIGIN,
        commit_size=COMMIT_SIZE, lookahead_size=LOOKAHEAD_SIZE,
        pop_size=POP_SIZE, generations=GENERATIONS, polish_evals=POLISH_EVALS, seed=SEED,
        export=False, decision_engine=eng, operator_index=op_index,
        log_fn=print,
    )
    elapsed = time.time() - t0
    agg = rolling_result['aggregate']
    results[arm_name] = agg
    print(f"[{arm_name}] done in {elapsed:.0f}s")

print()
print("="*70)
print("SUMMARY (same columns as PROJECT_STATUS.txt 0H.5)")
print("="*70)
print(f"{'arm':<14}{'on-time':>10}{'makespan':>11}{'throughput':>12}{'mean-tard':>11}{'labour':>9}{'margin':>9}")
for arm_name, agg in results.items():
    margin = agg.get('mean_margin_over_baseline_pct')
    margin_str = f"{margin:+.1f}%" if margin is not None else "n/a"
    print(f"{arm_name:<14}"
          f"{agg['on_time_pct']:>9.1f}%"
          f"{agg['makespan_days']:>10.1f}d"
          f"{agg['throughput_orders_per_day']:>11.1f}/d"
          f"{agg['mean_tardiness_h']:>10.0f}h"
          f"{agg['labour_utilisation_pct']:>8.1f}%"
          f"{margin_str:>9}")

print("\nDONE")
