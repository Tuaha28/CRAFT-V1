"""
FINAL approved piece of the WLC release-gate work
(WLC_RELEASE_GATE_PROPOSAL.md): the gated-buffer x release-gate
interaction test. Neither mechanism has been measured against the other
before this run -- the confidence-gated risk buffer (GA_AUDIT.md Section
6) was adopted BEFORE the release-date-enforcement fix (Section 8) even
existed, and the release gate (Section 10) has never run alongside it.

Same methodology as every other ablation this project has run (one
process, one origin, one seed, real GNN predictions, full production
settings): scratch/gated_buffer_ablation.py, scratch/corrected_simulation_run.py.

Three arms, not a full 2x2 -- the decision this needs to serve is "should
the gate be adopted on top of the current production default (buffer on)",
with a third arm to separate the gate's own contribution from the
buffer's:

  A_current_default: buffer ON  (0.5, gated at 0.14 -- current shipped
                      default), gate OFF. This is what the system does today.
  B_both:             buffer ON, gate ON (norm_days=2.0, the Section 4
                      starting point). The proposed combination.
  C_gate_only:        buffer OFF, gate ON. Isolates the gate's own effect,
                      holding the buffer fixed off.

Report whichever way this lands -- a conflict, redundancy, or a clean
combined win are all valid, reportable outcomes.
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
from datetime import datetime

import pandas as pd
import torch

import ga.schedule_utils as su
from database.db import get_engine
from database.queries import fetch_concurrent_operator_capacity
from graph.graph_builder import FactoryGraphBuilder
from models.gnn import MultiTaskFactoryGNN
from ga.rolling_horizon import run_rolling_horizon
from ga.release_gate import ReleaseGate
from utils.config import GNN_HIDDEN_CHANNELS, OUTPUT_DIR

COMMIT_SIZE = 300
POP_SIZE = 50
GENERATIONS = 50
POLISH_EVALS = 400
SEED = 42
NORM_DAYS = 2.0  # WLC_RELEASE_GATE_PROPOSAL.md Section 4 starting point

print("=" * 70)
print("SETUP (shared across all 3 arms)")
print("=" * 70)

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

capacity = fetch_concurrent_operator_capacity()
ORIGIN = datetime.now()
print(f"fixed origin timestamp: {ORIGIN.isoformat()}")
print(f"commit={COMMIT_SIZE} pop={POP_SIZE} gen={GENERATIONS} polish={POLISH_EVALS} seed={SEED} "
      f"norm_days={NORM_DAYS}")

BUFFER_ON = (0.5, 0.14)
BUFFER_OFF = (0.0, 0.14)


def set_buffer(on_off):
    su.RISK_BUFFER_AT_CERTAINTY, su.RISK_BUFFER_GATE_THRESHOLD = on_off


ARMS = ['A_current_default', 'B_both', 'C_gate_only']
results = {}

for arm in ARMS:
    print()
    print("=" * 70)
    print(f"ARM: {arm}")
    print("=" * 70)

    if arm == 'A_current_default':
        set_buffer(BUFFER_ON)
        release_gate = None
    elif arm == 'B_both':
        set_buffer(BUFFER_ON)
        release_gate = ReleaseGate(builder.df_routings, builder.df_machines, df_lines, ORIGIN,
                                    norm_days=NORM_DAYS, log_fn=print)
    else:  # C_gate_only
        set_buffer(BUFFER_OFF)
        release_gate = ReleaseGate(builder.df_routings, builder.df_machines, df_lines, ORIGIN,
                                    norm_days=NORM_DAYS, log_fn=print)

    t0 = time.time()
    result = run_rolling_horizon(
        builder.df_routings, builder.df_machines, df_lines, preds_all, capacity, ORIGIN,
        commit_size=COMMIT_SIZE, lookahead_size=None,
        pop_size=POP_SIZE, generations=GENERATIONS, polish_evals=POLISH_EVALS, seed=SEED,
        export=False, decision_engine=None, operator_index=None,
        log_fn=print, release_gate=release_gate,
    )
    elapsed = time.time() - t0
    agg = result['aggregate']
    results[arm] = agg
    n_windows = agg.get('n_windows')
    print(f"[{arm}] done in {elapsed:.0f}s, {n_windows} GA rounds"
          + (f", {release_gate.pool_size()} orders still pooled (should be 0), "
             f"{len(release_gate.infeasible_orders)} infeasible reported" if release_gate is not None else ""))

set_buffer(BUFFER_ON)  # restore the real production default before exiting

print()
print("=" * 70)
print("SUMMARY")
print("=" * 70)
print(f"{'arm':<20}{'on-time':>10}{'makespan':>11}{'throughput':>12}{'mean-tard':>11}{'labour':>9}{'margin':>9}{'rounds':>8}")
for arm, agg in results.items():
    margin = agg.get('mean_margin_over_baseline_pct')
    margin_str = f"{margin:+.1f}%" if margin is not None else "n/a"
    print(f"{arm:<20}"
          f"{agg['on_time_pct']:>9.1f}%"
          f"{agg['makespan_days']:>10.1f}d"
          f"{agg['throughput_orders_per_day']:>11.1f}/d"
          f"{agg['mean_tardiness_h']:>10.0f}h"
          f"{agg['labour_utilisation_pct']:>8.1f}%"
          f"{margin_str:>9}"
          f"{agg.get('n_windows', 0):>8}")

print("\nDONE")
