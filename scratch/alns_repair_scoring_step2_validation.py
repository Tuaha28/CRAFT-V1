"""
ALNS_REPAIR_SCORING_PROPOSAL.md Section 6/13, Step 2: the real validation
run. Corrected repair-line scoring (ga/alns.py::_repair_reassign,
repair_score_weights opt-in, Section 13) vs. (a) the one-shot-polish
baseline and (b) today's already-HOLD-adopted plain-load ALNS, across 4
real frozen-snapshot cases -- window1/seed42, window1/seed123,
window2/seed42, window3/seed42 (snapshot "20260825", same 3 windows Step
1's diverse-sampling re-run used, extended from the original 2-window/3-
case ALNS_REFINEMENT_PROPOSAL.md methodology by adding window 3, since
window 2 has now shown up as a structural outlier in TWO independent
tests and deserves its own comparison point rather than being folded
into an aggregate with window 1).

Per instruction: window 2's result is reported on its own, never folded
into a single aggregate percentage with windows 1/3.

removal_frac fixed at 0.035 (this project's standing middle default,
already shown in the recalibrated ALNS validation log to barely move
plain-load ALNS's final fitness within a case -- the dimension actually
under test here is repair SCORING, not removal size). 4 weight
combinations tested (Section 6: "3-4 combinations, not open-ended
tuning"):
  w1 = (1.0, 1.0, 0.3)  -- the placeholder used throughout Step 1
  w2 = (1.0, 0.5, 0.3)  -- lower risk weight
  w3 = (1.0, 2.0, 0.3)  -- higher risk weight
  w4 = (1.0, 1.0, 0.0)  -- interaction term only, impact term zeroed

Pre-committed decision rule, same standard ALNS itself was held to
(ALNS_REFINEMENT_PROPOSAL.md Section 6): worth adopting if it beats
BOTH baseline_polish and the already-adopted plain-load ALNS on a real
majority of cases with no real regression elsewhere -- reported plainly
per case, not forced into one pass/fail number, so window 2's separate
behaviour stays visible.

Does NOT change ga/scheduler.py's optimize() default -- richer scoring
stays opt-in (repair_score_weights=None unchanged default), same as
memetic_interval/TCOX/plain ALNS itself.
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
REMOVAL_FRAC = 0.035
WEIGHT_COMBOS = [
    ('w1_placeholder', (1.0, 1.0, 0.3)),
    ('w2_lower_risk', (1.0, 0.5, 0.3)),
    ('w3_higher_risk', (1.0, 2.0, 0.3)),
    ('w4_interaction_only', (1.0, 1.0, 0.0)),
]

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
sched_w1, start_w1 = build_scheduler_and_seed(*window_data(0, COMMIT_SIZE))
print("\nBuilding window 2 scheduler...")
sched_w2, start_w2 = build_scheduler_and_seed(*window_data(COMMIT_SIZE, COMMIT_SIZE))
print("\nBuilding window 3 scheduler...")
sched_w3, start_w3 = build_scheduler_and_seed(*window_data(2 * COMMIT_SIZE, COMMIT_SIZE))

CASES = [
    ('window1_seed42', sched_w1, start_w1, 42),
    ('window1_seed123', sched_w1, start_w1, 123),
    ('window2_seed42', sched_w2, start_w2, 42),
    ('window3_seed42', sched_w3, start_w3, 42),
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
    results.append({'case': case_name, 'arm': 'baseline_polish', 'final_fitness': pstats['final_fitness']})

    t0 = datetime.now()
    plain, plstats = sched.alns_refine(list(start), max_evals=POLISH_MAX_EVALS,
                                        removal_frac=REMOVAL_FRAC, random_seed=seed)
    elapsed = (datetime.now() - t0).total_seconds()
    det = score(sched, plain)
    print(f"[ALNS plain-load (already adopted, opt-in)] final={plstats['final_fitness']:.2f} "
          f"({plstats['improvement_pct']:+.3f}%) on-time={det['on_time_rate']*100:.1f}% "
          f"makespan={det['makespan_days']:.2f}d  {elapsed:.1f}s")
    results.append({'case': case_name, 'arm': 'alns_plain', 'final_fitness': plstats['final_fitness']})

    for combo_name, weights in WEIGHT_COMBOS:
        t0 = datetime.now()
        refined, rstats = sched.alns_refine(list(start), max_evals=POLISH_MAX_EVALS,
                                             removal_frac=REMOVAL_FRAC, random_seed=seed,
                                             repair_score_weights=weights)
        elapsed = (datetime.now() - t0).total_seconds()
        det = score(sched, refined)
        print(f"[ALNS richer {combo_name} {weights}] final={rstats['final_fitness']:.2f} "
              f"({rstats['improvement_pct']:+.3f}%) on-time={det['on_time_rate']*100:.1f}% "
              f"makespan={det['makespan_days']:.2f}d  "
              f"new_best={rstats['accepted_new_best']} improved={rstats['accepted_improved']} "
              f"worse_accepted={rstats['accepted_worse']} rejected={rstats['rejected']}  {elapsed:.1f}s")
        results.append({'case': case_name, 'arm': combo_name, 'final_fitness': rstats['final_fitness']})

print()
print("=" * 70)
print("SUMMARY -- final fitness by case x arm (lower is better)")
print("=" * 70)
arms = ['baseline_polish', 'alns_plain'] + [c for c, _ in WEIGHT_COMBOS]
header = f"{'case':<20}" + "".join(f"{a:>20}" for a in arms)
print(header)
for case_name, *_ in CASES:
    row = f"{case_name:<20}"
    for arm in arms:
        r = next(x for x in results if x['case'] == case_name and x['arm'] == arm)
        row += f"{r['final_fitness']:>20.2f}"
    print(row)

print()
print("=" * 70)
print("DELTA vs baseline_polish (%, positive = richer/ALNS wins) -- BY CASE, NOT AGGREGATED")
print("=" * 70)
print(f"{'case':<20}" + "".join(f"{a:>20}" for a in arms[1:]))
for case_name, *_ in CASES:
    base = next(x for x in results if x['case'] == case_name and x['arm'] == 'baseline_polish')
    row = f"{case_name:<20}"
    for arm in arms[1:]:
        r = next(x for x in results if x['case'] == case_name and x['arm'] == arm)
        delta = 100.0 * (base['final_fitness'] - r['final_fitness']) / base['final_fitness']
        row += f"{delta:>19.3f}%"
    print(row)

print()
print("=" * 70)
print("DELTA vs alns_plain (%, positive = richer scoring wins over ALREADY-ADOPTED plain ALNS)")
print("=" * 70)
print(f"{'case':<20}" + "".join(f"{c:>20}" for c, _ in WEIGHT_COMBOS))
for case_name, *_ in CASES:
    plain = next(x for x in results if x['case'] == case_name and x['arm'] == 'alns_plain')
    row = f"{case_name:<20}"
    for combo_name, _ in WEIGHT_COMBOS:
        r = next(x for x in results if x['case'] == case_name and x['arm'] == combo_name)
        delta = 100.0 * (plain['final_fitness'] - r['final_fitness']) / plain['final_fitness']
        row += f"{delta:>19.3f}%"
    print(row)

print()
print("=" * 70)
print("WINDOW 2 REPORTED SEPARATELY (per instruction -- not folded into an aggregate)")
print("=" * 70)
w2_case = next(c for c in CASES if c[0] == 'window2_seed42')
base = next(x for x in results if x['case'] == 'window2_seed42' and x['arm'] == 'baseline_polish')
plain = next(x for x in results if x['case'] == 'window2_seed42' and x['arm'] == 'alns_plain')
print(f"window2_seed42: baseline_polish={base['final_fitness']:.2f}  alns_plain={plain['final_fitness']:.2f}")
for combo_name, weights in WEIGHT_COMBOS:
    r = next(x for x in results if x['case'] == 'window2_seed42' and x['arm'] == combo_name)
    dvb = 100.0 * (base['final_fitness'] - r['final_fitness']) / base['final_fitness']
    dvp = 100.0 * (plain['final_fitness'] - r['final_fitness']) / plain['final_fitness']
    print(f"  {combo_name} {weights}: final={r['final_fitness']:.2f}  "
          f"vs baseline={dvb:+.3f}%  vs plain-ALNS={dvp:+.3f}%")

print()
print("=" * 70)
print("WINDOWS 1 AND 3 (per instruction -- reported separately from window 2)")
print("=" * 70)
for case_name in ['window1_seed42', 'window1_seed123', 'window3_seed42']:
    base = next(x for x in results if x['case'] == case_name and x['arm'] == 'baseline_polish')
    plain = next(x for x in results if x['case'] == case_name and x['arm'] == 'alns_plain')
    print(f"{case_name}: baseline_polish={base['final_fitness']:.2f}  alns_plain={plain['final_fitness']:.2f}")
    for combo_name, weights in WEIGHT_COMBOS:
        r = next(x for x in results if x['case'] == case_name and x['arm'] == combo_name)
        dvb = 100.0 * (base['final_fitness'] - r['final_fitness']) / base['final_fitness']
        dvp = 100.0 * (plain['final_fitness'] - r['final_fitness']) / plain['final_fitness']
        print(f"  {combo_name} {weights}: final={r['final_fitness']:.2f}  "
              f"vs baseline={dvb:+.3f}%  vs plain-ALNS={dvp:+.3f}%")

print("\nDONE")
