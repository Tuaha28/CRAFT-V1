"""
GA/agent post-mortem, Parts 1-2: order intake + GA internals. Real numbers
from ONE real, instrumented run (window 1: 300 orders, the dense early
period), full production settings (pop=50, gen=50, polish=400), current
production defaults (buffer ON at 0.5/0.14, release gate OFF -- not
adopted). No summarizing of intended behaviour -- every number here comes
from this actual run's real output.
"""
import sys, os, json
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime
from collections import Counter

import pandas as pd
import torch

from database.db import get_engine
from database.queries import (
    fetch_active_orders_and_routings, fetch_machine_state,
    fetch_concurrent_operator_capacity, fetch_assignable_operators, fetch_line_factory_map,
)
from graph.graph_builder import FactoryGraphBuilder
from models.gnn import MultiTaskFactoryGNN
from ga.scheduler import GAScheduler, make_key
from ga.heuristics import baseline_chromosomes
from agent.decision_engine import AIDecisionEngine
from utils.config import GNN_HIDDEN_CHANNELS, OUTPUT_DIR

MINS_PER_DAY = 1440.0
COMMIT_SIZE = 300
POP_SIZE = 50
GENERATIONS = 50
POLISH_EVALS = 400
SEED = 42

print("=" * 70)
print("PART 1a: ORDER INTAKE -- day-0 eligible count, real query, no GA")
print("=" * 70)

df_routings_all = fetch_active_orders_and_routings()
df_routings_all['release_date'] = pd.to_datetime(df_routings_all['release_date'])
order_release = df_routings_all.groupby('production_order_id')['release_date'].min()
ORIGIN = datetime.now()
total_orders = len(order_release)
day0_eligible = int((order_release <= ORIGIN).sum())
print(f"ORIGIN (now): {ORIGIN.isoformat()}")
print(f"Total active orders: {total_orders}")
print(f"Orders with release_date <= now (genuinely eligible to start immediately, "
      f"i.e. the real day-0 concurrent figure): {day0_eligible} "
      f"({100.0*day0_eligible/total_orders:.1f}%)")
print("Current production default: release gate is OFF (not adopted, WLC_RELEASE_GATE_PROPOSAL.md "
      "Section 11 -- a business tradeoff decision, not resolved). So under the CURRENT default, "
      "every one of these day0_eligible orders is immediately admitted into GA scheduling with no "
      "workload-norm pacing -- this number IS what the factory is actually asked to start with.")

print()
print("=" * 70)
print("SETUP -- graph, real GNN predictions, window 1 (300 orders)")
print("=" * 70)

builder = FactoryGraphBuilder()
graph, _ = builder.build_graph()
df_lines = pd.read_sql(
    "SELECT line_id, department_id, line_number, target_efficiency_percentage, is_active "
    "FROM Production_Lines WHERE is_active = 1", get_engine())
df_machines = builder.df_machines

model = MultiTaskFactoryGNN(metadata=graph.metadata(), hidden_channels=GNN_HIDDEN_CHANNELS)
model.load_state_dict(torch.load(os.path.join(OUTPUT_DIR, "checkpoints", "gnn_multitask_latest.pth")))
model.eval()
with torch.no_grad():
    out = model(graph.x_dict, graph.edge_index_dict, graph['operation'].type_idx)
delay = torch.sigmoid(out['delay']).squeeze(-1)
preds_all = {k: {'delay': delay[i].item(), 'priority': out['priority'][i].item()}
             for i, k in enumerate(builder.op_map.keys())}

capacity = fetch_concurrent_operator_capacity()

all_orders_sorted = order_release.reset_index().sort_values(['release_date', 'production_order_id'])
window1_ids = set(all_orders_sorted['production_order_id'].head(COMMIT_SIZE).tolist())
sub = builder.df_routings[builder.df_routings['production_order_id'].isin(window1_ids)]
keys = {make_key(r['production_order_id'], r['operation_id']) for _, r in sub.iterrows()}
preds_window = {k: v for k, v in preds_all.items() if k in keys}
print(f"Window 1: {len(window1_ids)} orders, {len(sub.drop_duplicates(subset=['production_order_id','operation_id']))} operations")

# Order to trace end-to-end: the earliest-released order in window 1.
trace_order_id = str(all_orders_sorted.iloc[0]['production_order_id'])
print(f"Order chosen for end-to-end trace: {trace_order_id} "
      f"(earliest release date in the whole book: {all_orders_sorted.iloc[0]['release_date']})")

print()
print("=" * 70)
print("PART 2b: MANAGER-PRIORITY GENE FRACTION (cheap: construction only, no optimize())")
print("=" * 70)

sched_plain = GAScheduler(sub, df_machines, df_lines, preds_window,
                           current_time=ORIGIN, present_operator_count=capacity)
sched_forced = GAScheduler(sub, df_machines, df_lines, preds_window,
                            current_time=ORIGIN, present_operator_count=capacity,
                            manager_prioritized_orders=[trace_order_id])

total_genes = len(sched_forced.gnn_predictions)
boosted = [k for k, v in sched_forced.gnn_predictions.items() if v.get('priority_multiplier', 1.0) != 1.0]
print(f"Total operations (genes) in window 1: {total_genes}")
print(f"Operations belonging to manager-forced order {trace_order_id}: {len(boosted)}")
print(f"Fraction of window's genes carrying the boost: {100.0*len(boosted)/total_genes:.3f}%")
if boosted:
    sample_key = boosted[0]
    print(f"Sample boosted gene: {sample_key} -> priority_multiplier={sched_forced.gnn_predictions[sample_key]['priority_multiplier']:.3f} "
          f"(plain run's same gene: {sched_plain.gnn_predictions[sample_key].get('priority_multiplier', 1.0):.3f})")

print()
print("Real fitness delta this boost causes on the SAME baseline heuristics (no full GA needed):")
base_plain = sched_plain.run_baselines()
base_forced = sched_forced.run_baselines()
for name in base_plain:
    fp, ff = base_plain[name]['fitness'], base_forced[name].get('fitness')
    if ff is not None:
        print(f"  {name:<15} plain={fp:>9.2f}  forced={ff:>9.2f}  delta={ff-fp:>+8.3f}")

print()
print("=" * 70)
print("PART 2a: POPULATION SEEDING -- real strategy composition")
print("=" * 70)
print(f"pop_size={POP_SIZE}, 5 strategies, round-robin i%5, but pop[-1] gets force-overwritten "
      f"by the single best pure heuristic (ga/scheduler.py::optimize) -- so real composition is:")
strategy_names = ['workload-balanced', 'edf', 'critical-ratio', 'fifo', 'randomized']
counts = Counter(strategy_names[i % 5] for i in range(POP_SIZE))
last_strategy = strategy_names[(POP_SIZE - 1) % 5]
counts[last_strategy] -= 1
print(f"  {dict(counts)}  (+1 best-heuristic clone overwriting the last '{last_strategy}' slot)")

print()
print("=" * 70)
print("RUNNING THE REAL GA -- window 1, n_solutions=3, full production settings")
print("=" * 70)
t0 = datetime.now()
sols, logbook = sched_plain.optimize(pop_size=POP_SIZE, generations=GENERATIONS,
                                      n_solutions=3, random_seed=SEED)
elapsed = (datetime.now() - t0).total_seconds()
print(f"optimize() done in {elapsed:.0f}s, {len(sols)} solution(s) returned")

print()
print("=" * 70)
print("PART 2 (main): REAL GENERATION-BY-GENERATION FITNESS TRAJECTORY")
print("=" * 70)
gen0_min = None
final_min = None
for rec in logbook:
    if rec['gen'] == 0:
        gen0_min = rec['min']
    final_min = rec['min']
    print(f"  gen {rec['gen']:>3}  nevals {rec['nevals']:>3}  avg {rec['avg']:>10.2f}  min {rec['min']:>10.2f}")

improvement_pct = 100.0 * (gen0_min - final_min) / gen0_min if gen0_min else 0.0
print(f"\nGen-0 min fitness: {gen0_min:.2f}")
print(f"Final (gen {GENERATIONS}) min fitness: {final_min:.2f}")
print(f"Real improvement from evolution alone (gen0 -> final, BEFORE local search): {improvement_pct:.3f}%")

print()
print("Local search on top of the winning individual:")
polished, pstats = sched_plain.polish(sols[0][0], max_evals=POLISH_EVALS, random_seed=SEED)
print(f"  {sols[0][1]:.2f} -> {polished.fitness.values[0]:.2f} ({pstats['improvement_pct']:+.3f}%) "
      f"in {pstats.get('evaluations', '?')} evaluations; accepted {pstats.get('accepted', '?')}, "
      f"rejected {pstats.get('rejected', '?')}")

print()
print("=" * 70)
print("PART 2c: MULTI-SOLUTION DIVERSITY -- real fitness AND real decision differences")
print("=" * 70)
num_ops = sched_plain.num_ops
for i, (chromo, fit) in enumerate(sols, start=1):
    la = chromo[num_ops:2*num_ops]
    print(f"  solution {i}: fitness={fit:.2f}")
for i in range(len(sols)):
    for j in range(i+1, len(sols)):
        la_i = sols[i][0][num_ops:2*num_ops]
        la_j = sols[j][0][num_ops:2*num_ops]
        diff = sum(1 for a, b in zip(la_i, la_j) if a != b)
        print(f"  solution {i+1} vs {j+1}: fitness diff={abs(sols[i][1]-sols[j][1]):.2f}, "
              f"LINE ASSIGNMENT differs on {diff}/{num_ops} operations ({100.0*diff/num_ops:.1f}%)")

print()
print("=" * 70)
print("EXPORTING winning solution for the order trace (real operator assignment)")
print("=" * 70)
eng = AIDecisionEngine()
operator_index = eng._build_operator_index(fetch_assignable_operators(), df_lines, fetch_line_factory_map())
committed_ids = {str(x) for x in window1_ids}
summary = eng._export_proposed_schedule(
    polished, sched_plain.num_ops, sched_plain.op_details, preds_window, df_lines,
    sched_plain.factory.eligibility_map, sched_plain.order_deadline_abs,
    sub, df_machines, label="postmortem",
    machine_pools=sched_plain.factory.machine_ids_per_line_map,
    operator_index=operator_index, present_operator_count=capacity,
    usable_machine_count=sched_plain.usable_machine_count,
    op_optype=sched_plain.factory.optype_for_op,
    protected_machine_type=sched_plain.protected_machine_type,
)
print(f"Export summary: {summary}")

print()
print("=" * 70)
print(f"PART 1b: ORDER {trace_order_id} TRACED END-TO-END (real exported decisions)")
print("=" * 70)
out_path = os.path.join(OUTPUT_DIR, "proposed_schedule_postmortem.json")
with open(out_path) as f:
    proposed = json.load(f)
order_ops = [p for p in proposed if p['production_order_id'] == trace_order_id]
order_ops.sort(key=lambda p: p['proposed_start_time'])
print(f"Order {trace_order_id}: {len(order_ops)} operation(s) in this window's committed schedule")
for op in order_ops:
    print(f"\n  operation {op['operation_id']}:")
    print(f"    line: {op['assigned_line_number']} (line_id={op['assigned_line_id']})")
    print(f"    machines: {op['machine_ids_used']} ({op['machines_used']})")
    print(f"    employees: {op['employee_ids_used']} (sourcing tier: {op['operator_sourcing_tier']})")
    print(f"    start: {op['proposed_start_time']}  base_end: {op['base_end_time']}  "
          f"cushioned_end: {op['cushioned_end_time']}")
    print(f"    deadline: {op['order_deadline']}")
    print(f"    XAI reason: {op['xai_reason']}")

print("\nDONE")
