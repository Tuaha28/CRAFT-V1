"""
One-off diagnostic (not part of the pipeline): does the per-window on-time
collapse actually reproduce against TODAY's resampled synthetic order book (1,029 orders,
Module 6's grounded count, real front-loaded seasonal shape), or does it
look different now that the underlying data has changed?

The historical collapse (100%->98%->77%->7.3%->0.3% across 5 windows,
PROJECT_STATUS.txt) was measured against the OLD invented-1,500-order book.
GA_AUDIT.md's Section 3 found the CURRENT order book's windows are 25x
uneven in order density (15.00 orders/day in window 1 down to 0.60 in
window 4) -- structurally consistent with the historical collapse
mechanism, but not yet confirmed with a fresh run. This script is that
fresh run: same production rolling-horizon settings as
run_final_pipeline.py's do_schedule() (commit=300, lookahead=300, pop=50,
gen=50, polish=400, seed=42), against the current real database and the
current (split-encoder) production checkpoint, export=False (cheaper,
matches the lookahead-sweep style -- per-window on-time/margin don't need
per-window JSON export).
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import torch
import pandas as pd

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

COMMIT_SIZE = 300
LOOKAHEAD_SIZE = 300
POP_SIZE = 50
GENERATIONS = 50
POLISH_EVALS = 400
SEED = 42

print("Building graph + loading production (split-encoder) checkpoint...")
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
from datetime import datetime
ORIGIN = datetime.now()
print(f"origin: {ORIGIN.isoformat()}")

eng = AIDecisionEngine()
op_index = eng._build_operator_index(
    fetch_assignable_operators(), df_lines, fetch_line_factory_map())

print(f"commit={COMMIT_SIZE} lookahead={LOOKAHEAD_SIZE} pop={POP_SIZE} "
      f"gen={GENERATIONS} polish={POLISH_EVALS} seed={SEED}")
print()

rolling_result = run_rolling_horizon(
    builder.df_routings, builder.df_machines, df_lines, preds_all, capacity, ORIGIN,
    commit_size=COMMIT_SIZE, lookahead_size=LOOKAHEAD_SIZE,
    pop_size=POP_SIZE, generations=GENERATIONS, polish_evals=POLISH_EVALS, seed=SEED,
    export=False, decision_engine=eng, operator_index=op_index,
    log_fn=print,
)

print()
print("="*70)
print("PER-WINDOW ON-TIME / LOCAL-SEARCH GAIN (today's real order book)")
print("="*70)
for w in rolling_result['windows']:
    print(f"  window {w['window']:>2}: lookahead {w.get('lookahead_orders')} orders, "
          f"committed {w.get('committed_orders')} — on-time {w.get('on_time_pct', 0):>6.1f}%   "
          f"local search {w.get('local_search_gain_pct', 0):+.2f}%   "
          f"margin {w.get('margin_pct')}")

agg = rolling_result['aggregate']
print()
print("AGGREGATE:")
for k in ['on_time_pct', 'makespan_days', 'throughput_orders_per_day',
          'mean_tardiness_h', 'labour_utilisation_pct', 'mean_margin_over_baseline_pct',
          'n_windows', 'total_runtime_s']:
    print(f"  {k:<32} {agg.get(k)}")

print("\nDONE")
