"""
ALNS_REFINEMENT_PROPOSAL.md Section 6: the validation run. Baseline
(heuristic-seeded construction + today's existing one-shot polish(),
ZERO new code) vs. the new ALNS refinement, at three removal-size values
(2%, 3.5%, 5% of num_ops, per instruction -- test the sensitivity rather
than assume one value), across the same 3 frozen-snapshot cases used
throughout this investigation.

Neither arm runs the GA's evolutionary generations loop -- both start
from the IDENTICAL heuristic-seeded individual (the same one optimize()
itself seeds into generation 0 today), so any difference measured is
attributable ONLY to the refinement stage. "Seed" here controls the
refinement stage's own random draws (destroy-operator ties, repair
randomness, SA acceptance draws for ALNS), not which heuristic gets
seeded, which is deterministic -- so window 1's scheduler is built ONCE
and reused for both the seed-42 and seed-123 cases.

Pre-committed decision rule (Section 6): ALNS is worth adopting if it
beats the one-shot-polish baseline's final fitness on >= 2 of 3 cases,
with no real regression on the third.

Does NOT modify ga/scheduler.py, ga/local_search.py, or ga/alns.py.
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database.ga_snapshot as ga_snapshot
SNAPSHOT_NAME = "20260825"
ga_snapshot.activate_snapshot(SNAPSHOT_NAME)

import pandas as pd
import torch
from datetime import datetime

from database.db import get_engine
from database.queries import fetch_concurrent_operator_capacity
from graph.graph_builder import FactoryGraphBuilder
from models.gnn import MultiTaskFactoryGNN
from ga.scheduler import GAScheduler, make_key
from ga.fitness import evaluate_schedule
from ga.heuristics import baseline_chromosomes
from utils.config import GNN_HIDDEN_CHANNELS, OUTPUT_DIR

COMMIT_SIZE = 300
POLISH_MAX_EVALS = 400
REMOVAL_FRACS = [0.02, 0.035, 0.05]

print("=" * 70)
print(f"SETUP -- frozen snapshot '{SNAPSHOT_NAME}'")
print("=" * 70)

ORIGIN = ga_snapshot.get_snapshot_origin(SNAPSHOT_NAME)
print(f"Frozen origin: {ORIGIN}")

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

order_release = builder.df_routings.groupby('production_order_id')['release_date'].min()
all_orders_sorted = order_release.reset_index().sort_values(['release_date', 'production_order_id'])
all_ids = all_orders_sorted['production_order_id'].tolist()


def window_data(pos, size):
    ids = set(all_ids[pos:pos + size])
    sub = builder.df_routings[builder.df_routings['production_order_id'].isin(ids)]
    keys = {make_key(r['production_order_id'], r['operation_id']) for _, r in sub.iterrows()}
    preds = {k: v for k, v in preds_all.items() if k in keys}
    return sub, preds


window1_sub, window1_preds = window_data(0, COMMIT_SIZE)
window2_sub, window2_preds = window_data(COMMIT_SIZE, COMMIT_SIZE)


def build_scheduler_and_seed(sub, preds_window):
    sched = GAScheduler(sub, df_machines, df_lines, preds_window,
                         current_time=ORIGIN, present_operator_count=capacity)
    baselines = sched.run_baselines()
    best_name = min(baselines, key=lambda k: baselines[k]['fitness'])
    best_genes = baseline_chromosomes(
        sched.factory, sched.op_details, sched.order_deadlines_minutes, sched.seq_lookup
    )[best_name]
    print(f"  seeded from best pure heuristic '{best_name}', fitness {baselines[best_name]['fitness']:.2f}")
    return sched, best_genes


print("\nBuilding window 1 scheduler (shared by seed-42 and seed-123 cases)...")
sched_w1, start_w1 = build_scheduler_and_seed(window1_sub, window1_preds)
print("\nBuilding window 2 scheduler...")
sched_w2, start_w2 = build_scheduler_and_seed(window2_sub, window2_preds)

CASES = [
    ('window1_seed42', sched_w1, start_w1, 42),
    ('window1_seed123', sched_w1, start_w1, 123),
    ('window2_seed42', sched_w2, start_w2, 42),
]


def score(sched, chromosome):
    _f, det = evaluate_schedule(
        list(chromosome), sched.num_ops, sched.op_details, sched.gnn_predictions,
        all_line_ids=sched.active_line_ids, order_deadlines=sched.order_deadlines_minutes,
        order_release_minutes=sched.order_release_minutes,
        machine_pools=sched.factory.machine_ids_per_line_map,
        machine_line_map=sched.machine_line_map,
        usable_machine_count=sched.usable_machine_count,
        present_operator_count=sched.present_operator_count,
        return_details=True,
        op_optype=sched.factory.optype_for_op,
        protected_machine_type=sched.protected_machine_type,
    )
    return det


results = []

for case_name, sched, start, seed in CASES:
    print()
    print("=" * 70)
    print(f"CASE: {case_name}")
    print("=" * 70)

    t0 = datetime.now()
    polished, pstats = sched.polish(list(start), max_evals=POLISH_MAX_EVALS, random_seed=seed)
    elapsed = (datetime.now() - t0).total_seconds()
    det = score(sched, polished)
    print(f"[baseline: one-shot polish] final={pstats['final_fitness']:.2f} "
          f"({pstats['improvement_pct']:+.3f}%) on-time={det['on_time_rate']*100:.1f}% "
          f"makespan={det['makespan_days']:.2f}d  {elapsed:.1f}s")
    results.append({
        'case': case_name, 'arm': 'baseline_polish', 'removal_frac': None,
        'start_fitness': pstats['start_fitness'], 'final_fitness': pstats['final_fitness'],
        'improvement_pct': pstats['improvement_pct'], 'on_time_rate': det['on_time_rate'],
        'makespan_days': det['makespan_days'], 'elapsed_s': elapsed, 'weight_history': None,
    })

    for frac in REMOVAL_FRACS:
        t0 = datetime.now()
        refined, rstats = sched.alns_refine(list(start), max_evals=POLISH_MAX_EVALS,
                                             removal_frac=frac, random_seed=seed)
        elapsed = (datetime.now() - t0).total_seconds()
        det = score(sched, refined)
        print(f"[ALNS removal_frac={frac:.3f}] final={rstats['final_fitness']:.2f} "
              f"({rstats['improvement_pct']:+.3f}%) on-time={det['on_time_rate']*100:.1f}% "
              f"makespan={det['makespan_days']:.2f}d  "
              f"new_best={rstats['accepted_new_best']} improved={rstats['accepted_improved']} "
              f"worse_accepted={rstats['accepted_worse']} rejected={rstats['rejected']}  {elapsed:.1f}s")
        first_w = rstats['weight_history'][0][1] if rstats['weight_history'] else {}
        last_w = rstats['weight_history'][-1][1] if rstats['weight_history'] else {}
        print(f"    weights: first-segment={ {k: round(v,2) for k,v in first_w.items()} }  "
              f"final={ {k: round(v,2) for k,v in last_w.items()} }")
        results.append({
            'case': case_name, 'arm': f'alns_{frac}', 'removal_frac': frac,
            'start_fitness': rstats['start_fitness'], 'final_fitness': rstats['final_fitness'],
            'improvement_pct': rstats['improvement_pct'], 'on_time_rate': det['on_time_rate'],
            'makespan_days': det['makespan_days'], 'elapsed_s': elapsed,
            'weight_history': rstats['weight_history'],
        })

print()
print("=" * 70)
print("SUMMARY")
print("=" * 70)
print(f"{'case':<20}{'arm':<20}{'final':>10}{'vs baseline':>14}{'on-time':>10}{'makespan':>11}")
for case_name, *_ in CASES:
    base = next(r for r in results if r['case'] == case_name and r['arm'] == 'baseline_polish')
    for r in [x for x in results if x['case'] == case_name]:
        delta = 100.0 * (base['final_fitness'] - r['final_fitness']) / base['final_fitness']
        delta_str = f"{delta:+.3f}%" if r['arm'] != 'baseline_polish' else '--'
        print(f"{r['case']:<20}{r['arm']:<20}{r['final_fitness']:>10.2f}{delta_str:>14}"
              f"{r['on_time_rate']*100:>9.1f}%{r['makespan_days']:>10.2f}d")

print("\n--- Decision rule check: does ANY removal_frac beat baseline on >= 2 of 3 cases, "
      "no real regression on the third? ---")
for frac in REMOVAL_FRACS:
    wins = 0
    regressions = 0
    detail = []
    for case_name, *_ in CASES:
        base = next(r for r in results if r['case'] == case_name and r['arm'] == 'baseline_polish')
        arm = next(r for r in results if r['case'] == case_name and r['arm'] == f'alns_{frac}')
        delta = 100.0 * (base['final_fitness'] - arm['final_fitness']) / base['final_fitness']
        if delta > 0.05:
            wins += 1
        elif delta < -0.05:
            regressions += 1
        detail.append(f"{case_name}:{delta:+.3f}%")
    print(f"removal_frac={frac:.3f}: wins={wins}/3 regressions={regressions}  [{', '.join(detail)}]")

print("\nDONE")
