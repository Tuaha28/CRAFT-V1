"""
CP_SAT_PROPOSAL.md Section 8 follow-up, per instruction: one targeted
test, not a broader investigation. Same window 1, same frozen snapshot
"20260825", same 235s budget, same model exactly as
scratch/cp_sat_window1_prototype.py (3-line candidate cap unchanged) --
the ONE variable that changes is a warm start from the GA's own real,
already-reported baseline solution for this window (window 1, seed 42,
the exact run that produced the 800.27 final fitness in
GA_AGENT_POSTMORTEM.md Section 11).

The real question: can CP-SAT improve on a schedule it doesn't have to
find from scratch, even under the same simplified/capped model that
found no feasible solution unaided? Not: does a differently-scoped model
do better (that would be a second variable, not this test).

Mechanics: run the real GA (optimize + polish, seed=42, matching Section
11's baseline arm exactly) to get its actual chromosome, then replicate
evaluate_schedule's own simulation loop (same select_machines_and_start,
same calculate_operation_work, same apply_risk_buffer -- the same
functions the real scoring uses, not a re-derivation) to recover each
operation's REAL chosen line, machine count, and start time. For every
operation whose GA-chosen line happens to fall within this model's
top-3-by-pool-size candidate set, hint that exact (line, mc, start) --
for the rest (a real, reported fraction -- the GA's work-aware line
choice is not the same criterion as "top-3 by raw pool size", so full
coverage is not assumed), no hint is given and CP-SAT decides freely.
Coverage is reported honestly, not assumed complete.

Does NOT modify ga/scheduler.py, ga/fitness.py, or ga/operators.py.
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database.ga_snapshot as ga_snapshot
SNAPSHOT_NAME = "20260825"
ga_snapshot.activate_snapshot(SNAPSHOT_NAME)

import math
from datetime import datetime

import pandas as pd
import torch
from ortools.sat.python import cp_model

from database.db import get_engine
from database.queries import fetch_concurrent_operator_capacity
from graph.graph_builder import FactoryGraphBuilder
from models.gnn import MultiTaskFactoryGNN
from ga.scheduler import GAScheduler, make_key
from ga.schedule_utils import apply_risk_buffer, calculate_operation_work, select_machines_and_start
from ga.fitness import CHANGEOVER_MINUTES
from utils.config import GNN_HIDDEN_CHANNELS, OUTPUT_DIR

COMMIT_SIZE = 300
CANDIDATE_LINES_PER_OP = 3
SOLVE_TIME_BUDGET_S = 235.0
TARDINESS_WEIGHT = 1000
MAKESPAN_WEIGHT = 1
HORIZON_MINS = 60 * 24 * 60
SEED = 42

print("=" * 70)
print(f"SETUP -- frozen snapshot '{SNAPSHOT_NAME}', window 1")
print("=" * 70)

ORIGIN = ga_snapshot.get_snapshot_origin(SNAPSHOT_NAME)

builder = FactoryGraphBuilder()
graph, _ = builder.build_graph()
df_lines = pd.read_sql(
    "SELECT line_id, department_id, line_number, target_efficiency_percentage, is_active "
    "FROM Production_Lines WHERE is_active = 1", get_engine())
df_machines = builder.df_machines

model_gnn = MultiTaskFactoryGNN(metadata=graph.metadata(), hidden_channels=GNN_HIDDEN_CHANNELS)
model_gnn.load_state_dict(torch.load(os.path.join(OUTPUT_DIR, "checkpoints", "gnn_multitask_latest.pth")))
model_gnn.eval()
with torch.no_grad():
    out = model_gnn(graph.x_dict, graph.edge_index_dict, graph['operation'].type_idx)
delay = torch.sigmoid(out['delay']).squeeze(-1)
preds_all = {k: {'delay': delay[i].item(), 'priority': out['priority'][i].item()}
             for i, k in enumerate(builder.op_map.keys())}
capacity = fetch_concurrent_operator_capacity()

order_release = builder.df_routings.groupby('production_order_id')['release_date'].min()
all_orders_sorted = order_release.reset_index().sort_values(['release_date', 'production_order_id'])
window1_ids = set(all_orders_sorted['production_order_id'].head(COMMIT_SIZE).tolist())
sub = builder.df_routings[builder.df_routings['production_order_id'].isin(window1_ids)]
keys = {make_key(r['production_order_id'], r['operation_id']) for _, r in sub.iterrows()}
preds_window = {k: v for k, v in preds_all.items() if k in keys}

sched = GAScheduler(sub, df_machines, df_lines, preds_window,
                     current_time=ORIGIN, present_operator_count=capacity)
op_keys = list(sched.op_details.keys())
print(f"window 1: {sub['production_order_id'].nunique()} orders, {len(op_keys)} operations")

# ---------------------------------------------------------------------------
# Step 1: the real GA baseline solution (window 1, seed 42) -- same config
# as GA_AGENT_POSTMORTEM.md Section 11's baseline arm.
# ---------------------------------------------------------------------------
print("\nRunning GA baseline (seed=42) to get the real warm-start solution...")
t_ga0 = datetime.now()
sols, logbook = sched.optimize(pop_size=50, generations=50, n_solutions=1, random_seed=SEED)
polished, pstats = sched.polish(sols[0][0], max_evals=400, random_seed=SEED)
ga_elapsed = (datetime.now() - t_ga0).total_seconds()
print(f"  GA done in {ga_elapsed:.0f}s, final fitness {polished.fitness.values[0]:.2f} "
      f"(expected: matches Section 11's 800.27 baseline, same seed/config)")

n = sched.num_ops
os_array, la_array, mc_array = list(polished[:n]), list(polished[n:2*n]), list(polished[2*n:])

# Replicate evaluate_schedule's own simulation loop (same primitives it
# uses: calculate_operation_work, apply_risk_buffer, select_machines_and_start)
# to recover each operation's REAL chosen line, machine count, and start
# time -- not a re-derivation, the same functions the real scoring calls.
machine_free = dict(sched.initial_machine_free) if sched.initial_machine_free else {}
machine_last_order = dict(sched.initial_machine_last_order) if sched.initial_machine_last_order else {}
order_ready_times = {}
ga_op_line = {}
ga_op_mc = {}
ga_op_start = {}

for i, op_key in enumerate(os_array):
    line_id = la_array[i]
    order_id = op_key.split('_')[0]
    details = sched.op_details.get(op_key, {})
    machine_count = max(1, int(mc_array[i]))
    pool_ids = sched.factory.machine_ids_per_line_map.get(op_key, {}).get(line_id, [])
    if pool_ids:
        machine_count = min(machine_count, len(pool_ids))
    duration_mins = calculate_operation_work(details, machine_count)

    release_floor = sched.order_release_minutes.get(order_id, 0.0)
    order_ready = max(order_ready_times.get(order_id, 0.0), release_floor)

    delay_prob = min(max(preds_window.get(op_key, {}).get('delay', 0.0), 0.0), 1.0)
    buffer_mins = apply_risk_buffer(duration_mins, delay_prob)

    is_protected_type = bool(
        sched.protected_machine_type is not None
        and sched.factory.optype_for_op.get(op_key) == sched.protected_machine_type
    )
    start_time, chosen_ids = select_machines_and_start(
        pool_ids, machine_free, machine_last_order,
        order_id, machine_count, order_ready, CHANGEOVER_MINUTES,
        is_protected_type=is_protected_type,
    )
    cushioned_end_time = start_time + buffer_mins
    for asset_id in chosen_ids:
        machine_free[asset_id] = cushioned_end_time
        machine_last_order[asset_id] = order_id
    order_ready_times[order_id] = cushioned_end_time

    ga_op_line[op_key] = line_id
    ga_op_mc[op_key] = machine_count
    ga_op_start[op_key] = start_time

print(f"  recovered real start times for {len(ga_op_start)} operations from the polished chromosome")

# ---------------------------------------------------------------------------
# Step 2: build the SAME CP-SAT model as cp_sat_window1_prototype.py
# (identical 3-line cap, identical candidates, identical objective).
# ---------------------------------------------------------------------------
print("\nBuilding CP-SAT model (identical to the unhinted prototype)...")
t_build0 = datetime.now()

cp = cp_model.CpModel()
op_start = {}
op_end = {}
op_candidates = {}
candidate_lookup = {}        # (op, line, mc) -> (presence_lit, cand_start_var, dur_int)
pool_intervals = {}
pool_demands = {}
pool_capacity = {}
total_candidates = 0

for op in op_keys:
    details = sched.op_details[op]
    setup_mins = details['setup_mins']
    serial_mins = details['serial_processing_mins']
    delay_prob = preds_window.get(op, {}).get('delay', 0.0)
    optype = sched.factory.optype_for_op.get(op)

    eligible_lines = sched.factory.eligibility_map.get(op, [])
    if not eligible_lines:
        continue

    pool_sizes = {ln: sched.factory.machines_per_line_map.get(op, {}).get(ln, 1) for ln in eligible_lines}
    candidate_lines = sorted(eligible_lines, key=lambda ln: pool_sizes[ln], reverse=True)[:CANDIDATE_LINES_PER_OP]

    op_start[op] = cp.NewIntVar(0, HORIZON_MINS, f"start_{op}")
    op_end[op] = cp.NewIntVar(0, HORIZON_MINS, f"end_{op}")
    candidates = []

    for line_id in candidate_lines:
        max_mc = sched.factory._max_machines_for(op, line_id)
        pool_key = (line_id, optype)
        pool_capacity.setdefault(pool_key, pool_sizes[line_id])

        for mc in range(1, max_mc + 1):
            base_dur = setup_mins + (serial_mins / mc)
            cushioned = apply_risk_buffer(base_dur, delay_prob)
            dur_int = max(1, math.ceil(cushioned))

            presence = cp.NewBoolVar(f"presence_{op}_{line_id}_{mc}")
            cand_start = cp.NewIntVar(0, HORIZON_MINS, f"cstart_{op}_{line_id}_{mc}")
            interval = cp.NewOptionalIntervalVar(
                cand_start, dur_int, cand_start + dur_int, presence, f"ivl_{op}_{line_id}_{mc}")

            cp.Add(op_start[op] == cand_start).OnlyEnforceIf(presence)
            cp.Add(op_end[op] == cand_start + dur_int).OnlyEnforceIf(presence)

            candidates.append(presence)
            candidate_lookup[(op, line_id, mc)] = (presence, cand_start, dur_int)
            pool_intervals.setdefault(pool_key, []).append(interval)
            pool_demands.setdefault(pool_key, []).append(mc)
            total_candidates += 1

    cp.AddExactlyOne(candidates)
    op_candidates[op] = candidates

for pool_key, intervals in pool_intervals.items():
    cp.AddCumulative(intervals, pool_demands[pool_key], pool_capacity[pool_key])

orders = {}
for op in op_start:
    order_id = op.split('_')[0]
    orders.setdefault(order_id, []).append(op)

for order_id, ops in orders.items():
    ops_sorted = sorted(ops, key=lambda k: sched.seq_lookup[k])
    release_floor = int(math.ceil(sched.order_release_minutes.get(order_id, 0.0)))
    cp.Add(op_start[ops_sorted[0]] >= release_floor)
    for a, b in zip(ops_sorted, ops_sorted[1:]):
        cp.Add(op_end[a] <= op_start[b])

order_completion = {}
tardiness_terms = []
for order_id, ops in orders.items():
    last_op = max(ops, key=lambda k: sched.seq_lookup[k])
    completion = op_end[last_op]
    order_completion[order_id] = completion
    deadline = int(math.floor(sched.order_deadlines_minutes.get(order_id, HORIZON_MINS)))
    tardiness = cp.NewIntVar(0, HORIZON_MINS, f"tardy_{order_id}")
    cp.AddMaxEquality(tardiness, [0, completion - deadline])
    tardiness_terms.append(tardiness)

makespan = cp.NewIntVar(0, HORIZON_MINS, "makespan")
cp.AddMaxEquality(makespan, list(op_end.values()))
cp.Minimize(TARDINESS_WEIGHT * sum(tardiness_terms) + MAKESPAN_WEIGHT * makespan)

build_elapsed = (datetime.now() - t_build0).total_seconds()
print(f"  {total_candidates} candidate intervals, {len(pool_intervals)} pools, built in {build_elapsed:.1f}s")

# ---------------------------------------------------------------------------
# Step 3: warm start -- hint every operation whose GA-chosen line is
# among this model's own top-3 candidates for that operation. Report
# coverage honestly; unmatched operations are left unhinted.
# ---------------------------------------------------------------------------
hinted = 0
unmatched = 0
for op in op_keys:
    real_line = ga_op_line.get(op)
    real_mc = ga_op_mc.get(op)
    real_start = ga_op_start.get(op)
    if real_line is None:
        continue
    key = (op, real_line, real_mc)
    if key not in candidate_lookup:
        unmatched += 1
        continue
    presence, cand_start, dur_int = candidate_lookup[key]
    start_int = max(0, round(real_start))
    cp.AddHint(presence, 1)
    cp.AddHint(cand_start, start_int)
    cp.AddHint(op_start[op], start_int)
    cp.AddHint(op_end[op], start_int + dur_int)
    hinted += 1

coverage_pct = 100.0 * hinted / max(1, len(op_keys))
print(f"\nWarm-start coverage: {hinted}/{len(op_keys)} operations hinted ({coverage_pct:.1f}%), "
      f"{unmatched} unmatched (GA's real line not among this model's top-{CANDIDATE_LINES_PER_OP} candidates)")

# ---------------------------------------------------------------------------
# Step 4: solve, same budget as the unhinted run.
# ---------------------------------------------------------------------------
print(f"\nSolving with warm start (time budget: {SOLVE_TIME_BUDGET_S:.0f}s)...")
solver = cp_model.CpSolver()
solver.parameters.max_time_in_seconds = SOLVE_TIME_BUDGET_S
solver.parameters.num_search_workers = 8
solver.parameters.log_search_progress = False

t_solve0 = datetime.now()
status = solver.Solve(cp)
solve_elapsed = (datetime.now() - t_solve0).total_seconds()

status_name = solver.StatusName(status)
print()
print("=" * 70)
print("RESULT -- warm-started")
print("=" * 70)
print(f"status: {status_name}  (solve time: {solve_elapsed:.1f}s of {SOLVE_TIME_BUDGET_S:.0f}s budget)")

if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
    obj = solver.ObjectiveValue()
    bound = solver.BestObjectiveBound()
    gap_pct = 100.0 * abs(obj - bound) / max(1.0, abs(obj))
    on_time = sum(1 for oid, c in order_completion.items()
                  if solver.Value(c) <= sched.order_deadlines_minutes.get(oid, HORIZON_MINS))
    on_time_rate = 100.0 * on_time / len(order_completion)
    makespan_days = solver.Value(makespan) / 1440.0
    print(f"objective: {obj:.1f}  best bound: {bound:.1f}  gap: {gap_pct:.2f}%")
    print(f"on-time: {on_time_rate:.1f}%  makespan: {makespan_days:.2f}d")
else:
    print("NO FEASIBLE SOLUTION FOUND within the time budget, EVEN WITH the warm start.")
    print("A more decisive negative than the unhinted run -- CP-SAT (this configuration) "
          "could not improve on, or even reconstruct/validate, a feasible starting point.")

print("\n--- For direct comparison ---")
print(f"GA baseline (this run): final fitness {polished.fitness.values[0]:.2f}, "
      f"on-time {pstats.get('on_time_rate', 'n/a')}")
print("Unhinted CP-SAT (earlier run): UNKNOWN, no feasible solution, stopped at 144.7s of 235s")

print("\nDONE")
