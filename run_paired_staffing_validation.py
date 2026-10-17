"""
Experiment 1 -- paired staffing validation, GA vs EDF.

WHY. run_authoritative_comparison.py runs the GA arm with export=True (real
worker assignment) and the baseline arms with run_rolling_horizon_baseline(),
which performed no worker assignment at all. Its 3,043 understaffed
operations therefore establish that the GA's exported schedule is not
execution-feasible and establish NOTHING about any baseline. A delivery
comparison between arms is only operationally meaningful if both arms are
staffable to a comparable degree -- which was never measured.

This runs GA and EDF through the IDENTICAL export path, from IDENTICAL
initial committed reservations, with SEPARATE reservation state per arm (a
shared mutable one would let whichever arm ran second inherit the first's
bookings and invent a shortage that is an artefact of ordering).

Symmetry is NOT assumed. The question is whether shortages hit similar
orders and similar amounts of work, or whether one arm degrades worse --
i.e. whether the GA's apparent delivery advantage survives staffing.

No reseed, no retraining, no GA/heuristic changes. Reads the same frozen
snapshot the authoritative comparison uses.
"""
import copy
import datetime as _dt
import glob
import json
import os
import sys
import time
from datetime import datetime

import pandas as pd
import torch

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

SNAPSHOT_NAME = "authoritative_20260911"

import database.ga_snapshot as ga_snapshot
ga_snapshot.activate_snapshot(SNAPSHOT_NAME)
ORIGIN = ga_snapshot.get_snapshot_origin(SNAPSHOT_NAME)
print(f"Frozen origin timestamp: {ORIGIN}")

from database.db import get_engine
from database.queries import (fetch_concurrent_operator_capacity,
                              fetch_assignable_operators, fetch_line_factory_map,
                              fetch_committed_operator_reservations)
from graph.graph_builder import FactoryGraphBuilder
from graph.feature_schema import load_preprocessing
from models.gnn import MultiTaskFactoryGNN, prior_corrected_delay_probabilities
from ga.rolling_horizon import run_rolling_horizon, run_rolling_horizon_baseline
from agent.decision_engine import AIDecisionEngine
from utils.config import GNN_HIDDEN_CHANNELS, OUTPUT_DIR, PLANNING_HORIZON_ORDERS

SEED = 42
POP, GENS, POLISH_EVALS = 50, 50, 400
_T0 = time.time()


def banner(t):
    print("\n" + "=" * 78 + "\n" + t + "\n" + "=" * 78, flush=True)


banner("GRAPH BUILD + GNN INFERENCE (same checkpoint, no retraining)")
builder = FactoryGraphBuilder()
ckpt = os.path.join(OUTPUT_DIR, "checkpoints", "gnn_multitask_latest.pth")
preprocessing = load_preprocessing(ckpt)
graph, _ = builder.build_graph(preprocessing=preprocessing)
df_lines = pd.read_sql(
    "SELECT line_id, department_id, line_number, target_efficiency_percentage, is_active "
    "FROM Production_Lines WHERE is_active = 1", get_engine())
df_machines = builder.df_machines

model = MultiTaskFactoryGNN(metadata=graph.metadata(), hidden_channels=GNN_HIDDEN_CHANNELS,
                            operation_in_dim=preprocessing['operation_schema']['count'])
model.load_state_dict(torch.load(ckpt))
model.eval()
with torch.no_grad():
    out = model(graph.x_dict, graph.edge_index_dict, graph['operation'].type_idx)
delay = prior_corrected_delay_probabilities(
    out['delay'], preprocessing['delay_pos_weight']
).squeeze(-1)
preds_all = {k: {'delay': delay[i].item(), 'priority': out['priority'][i].item()}
             for i, k in enumerate(builder.op_map.keys())}

capacity = fetch_concurrent_operator_capacity()
commit_size = PLANNING_HORIZON_ORDERS or builder.df_routings['production_order_id'].nunique()
print(f"  commit_size={commit_size}  concurrent_operators={capacity}")

engine_ga = AIDecisionEngine()
engine_edf = AIDecisionEngine()
assignable, line_factory = fetch_assignable_operators(), fetch_line_factory_map()
op_index_ga = engine_ga._build_operator_index(assignable, df_lines, line_factory)
op_index_edf = engine_edf._build_operator_index(assignable, df_lines, line_factory)

# ONE fetch, deep-copied per arm: identical starting reservations, independent
# evolution. Sharing the object would make the second arm inherit the first's
# bookings, manufacturing an asymmetry that is purely an artefact of order.
base_reservations = fetch_committed_operator_reservations(ORIGIN)
print(f"  initial committed reservations: {len(base_reservations)} operators with bookings")

banner("ARM A -- GA (optimize + polish), export=True")
t0 = time.time()
ga = run_rolling_horizon(
    builder.df_routings, df_machines, df_lines, preds_all, capacity, ORIGIN,
    commit_size=commit_size, pop_size=POP, generations=GENS, polish_evals=POLISH_EVALS,
    seed=SEED, export=True, decision_engine=engine_ga, operator_index=op_index_ga,
    initial_operator_reservations=copy.deepcopy(base_reservations),
    log_fn=print, ownership_snapshot=builder.ownership_snapshot,
    export_label_prefix='paired_ga')
print(f"GA arm done in {(time.time()-t0)/60:.1f} min")

banner("ARM B -- EDF (pure dispatching rule), export=True, SAME export path")
t0 = time.time()
edf = run_rolling_horizon_baseline(
    'EDF', builder.df_routings, df_machines, df_lines, preds_all, capacity, ORIGIN,
    commit_size=commit_size, export=True, decision_engine=engine_edf,
    operator_index=op_index_edf,
    initial_operator_reservations=copy.deepcopy(base_reservations),
    log_fn=print, ownership_snapshot=builder.ownership_snapshot,
    export_label_prefix='paired_edf')
print(f"EDF arm done in {(time.time()-t0)/60:.1f} min")


# The denominator, named. Every staffing fraction in this report is
# unfilled / REQUIRED, where required = sum(duration * machines_used) over the
# exported operations and assigned = sum(duration * operators_used). These
# satisfy required == assigned + unfilled exactly, which is precisely why the
# previously-reported 37.7% was wrong: it divided unfilled by
# (required + unfilled), double-counting the unfilled demand because the
# required total already contains it. Correct value: 60.45% (GA).
#
# ga/fitness.py's labor_used_minutes is the SAME required quantity (one
# operator assumed per engaged machine, existence never checked), so any
# utilisation figure derived from it describes labour DEMAND, not supply.
UNFILLED_QUALIFIER = (
    "unfilled ASSIGNMENT-minutes under the current greedy, "
    "single-worker-per-lot assignment policy -- NOT a proven minimum physical "
    "shortage. A different assignment order, or allowing worker handover "
    "mid-operation, could staff some of the same demand."
)


def export_labour_accounting(patterns):
    """required / assigned / unfilled from the exported operation intervals."""
    rows = []
    for pat in patterns:
        for f in sorted(glob.glob(os.path.join(OUTPUT_DIR, pat))):
            if f.endswith('.run.json'):
                continue
            with open(f, encoding='utf-8') as fh:
                rows += json.load(fh)
    required = assigned = 0.0
    long_unfilled = 0.0
    long_ops = 0
    for r in rows:
        try:
            d = (_dt.datetime.fromisoformat(r['base_end_time'])
                 - _dt.datetime.fromisoformat(r['proposed_start_time'])).total_seconds() / 60.0
        except (KeyError, ValueError):
            continue
        m, o = r.get('machines_used', 0), r.get('operators_used', 0)
        required += d * m
        assigned += d * o
        gap = d * (m - o)
        if gap > 0 and d > 480:
            long_unfilled += gap
            long_ops += 1
    unfilled = required - assigned
    return {
        'operations': len(rows),
        'required_worker_minutes': required,
        'assigned_worker_minutes': assigned,
        'unfilled_worker_minutes': unfilled,
        'unfilled_over_required_pct': (100.0 * unfilled / required) if required else None,
        'unfilled_in_operations_over_8h': long_unfilled,
        'operations_over_8h_with_gap': long_ops,
        'share_of_unfilled_in_operations_over_8h_pct': (
            100.0 * long_unfilled / unfilled) if unfilled else None,
    }


def staffing_profile(result):
    """Counts, worker-minutes and affected orders -- counts alone conflate a
    3-operator shortfall over 8 hours with one over 5 minutes."""
    per_window, orders, ops = [], {}, {}
    total_ops = total_short_wm = 0
    total_shortfall_heads = 0
    for i, s in enumerate(result.get('export_summaries', []), start=1):
        detail = s.get('understaffed_detail', []) or []
        # STRICT, not .get(..., 0). A zero default is how the Part 1 defect
        # hid: one of two record-construction paths lacked these fields and
        # silently contributed nothing to every sum. If a future path omits
        # them again, this must fail loudly rather than under-report.
        missing = [k for k in ('shortfall', 'shortfall_worker_minutes')
                   for d in detail if k not in d]
        if missing:
            raise KeyError(
                f"understaffed records missing impact fields {sorted(set(missing))} -- "
                f"a construction path is not populating them; summing would "
                f"silently under-report the shortage")
        wm = sum(d['shortfall_worker_minutes'] for d in detail)
        heads = sum(d['shortfall'] for d in detail)
        per_window.append({
            'window': i,
            'understaffed_operations': s.get('understaffed_operations', 0),
            'execution_feasible': s.get('execution_feasible'),
            'shortfall_worker_minutes': wm,
            'shortfall_headcount_sum': heads,
            'affected_orders': len({d['production_order_id'] for d in detail}),
        })
        total_ops += s.get('understaffed_operations', 0)
        total_short_wm += wm
        total_shortfall_heads += heads
        for d in detail:
            oid = str(d['production_order_id'])
            orders[oid] = orders.get(oid, 0.0) + d['shortfall_worker_minutes']
            ops[d['operation_key']] = ops.get(d['operation_key'], 0.0) + d['shortfall_worker_minutes']
    return {'per_window': per_window, 'total_understaffed_operations': total_ops,
            'total_shortfall_worker_minutes': total_short_wm,
            'total_shortfall_headcount': total_shortfall_heads,
            'orders': orders, 'operations': ops,
            'any_infeasible': any(w['execution_feasible'] is False for w in per_window)}


prof = {'GA': staffing_profile(ga), 'EDF': staffing_profile(edf)}
# Exports are written per window; the GA arm's first window carries no label.
acct = {
    'GA': export_labour_accounting(['proposed_schedule_paired_ga_w*.json']),
    'EDF': export_labour_accounting(['proposed_schedule_paired_edf_w*.json']),
}

banner("STAFFING FEASIBILITY -- PAIRED, SAME EXPORT PATH, SAME INITIAL RESERVATIONS")
for arm in ('GA', 'EDF'):
    p = prof[arm]
    print(f"\n  --- {arm} ---")
    print(f"  {'window':>8}{'understaffed_ops':>19}{'shortfall_heads':>18}"
          f"{'shortfall_wkr_min':>20}{'orders_hit':>12}{'feasible':>11}")
    for wdw in p['per_window']:
        print(f"  {wdw['window']:>8}{wdw['understaffed_operations']:>19}"
              f"{wdw['shortfall_headcount_sum']:>18}{wdw['shortfall_worker_minutes']:>20,.0f}"
              f"{wdw['affected_orders']:>12}{str(wdw['execution_feasible']):>11}")
    print(f"  {'TOTAL':>8}{p['total_understaffed_operations']:>19}"
          f"{p['total_shortfall_headcount']:>18}{p['total_shortfall_worker_minutes']:>20,.0f}"
          f"{len(p['orders']):>12}")

banner("CORRECTED DENOMINATOR -- unfilled / REQUIRED")
print("  required = sum(duration x machines_used) over exported operations")
print("  assigned = sum(duration x operators_used)")
print("  These satisfy required == assigned + unfilled exactly.")
print(f"  {UNFILLED_QUALIFIER}")
print()
print(f"  {'arm':<6}{'operations':>12}{'required':>16}{'assigned':>16}"
      f"{'unfilled':>16}{'unfilled/required':>19}")
for arm in ('GA', 'EDF'):
    a = acct[arm]
    print(f"  {arm:<6}{a['operations']:>12}{a['required_worker_minutes']:>16,.0f}"
          f"{a['assigned_worker_minutes']:>16,.0f}{a['unfilled_worker_minutes']:>16,.0f}"
          f"{a['unfilled_over_required_pct']:>18.2f}%")
print()
for arm in ('GA', 'EDF'):
    a = acct[arm]
    share = a['share_of_unfilled_in_operations_over_8h_pct']
    if share is None:
        # Zero unfilled minutes -> no share to report. Formatting None with
        # :.1f raised and killed the run after the numbers were computed but
        # before they were saved.
        print(f"  {arm}: no unfilled minutes at all, so no >8h share to report")
    else:
        print(f"  {arm}: of the unfilled total, {a['unfilled_in_operations_over_8h']:,.0f} "
              f"({share:.1f}%) sits in "
              f"{a['operations_over_8h_with_gap']} operations longer than 8h")
print()
print("  NOT COMPARABLE to the previously reported 37.7%: that divided unfilled")
print("  by (required + unfilled), double-counting the unfilled demand.")

banner("DO THE TWO ARMS DEGRADE THE SAME WAY?")
ga_o, edf_o = set(prof['GA']['orders']), set(prof['EDF']['orders'])
inter, union = ga_o & edf_o, ga_o | edf_o
print(f"  orders short in GA only            {len(ga_o - edf_o)}")
print(f"  orders short in EDF only           {len(edf_o - ga_o)}")
print(f"  orders short in BOTH               {len(inter)}")
print(f"  Jaccard overlap of affected orders {len(inter)/max(1,len(union)):.4f}")
gw, ew = prof['GA']['total_shortfall_worker_minutes'], prof['EDF']['total_shortfall_worker_minutes']
print(f"\n  GA  total shortfall worker-minutes {gw:,.0f}")
print(f"  EDF total shortfall worker-minutes {ew:,.0f}")
print(f"  GA / EDF ratio                     {(gw/ew if ew else float('nan')):.4f}")
ga_ops, edf_ops = set(prof['GA']['operations']), set(prof['EDF']['operations'])
op_inter, op_union = ga_ops & edf_ops, ga_ops | edf_ops
op_jac = len(op_inter) / max(1, len(op_union))
print(f"  operations short in GA only        {len(ga_ops - edf_ops)}")
print(f"  operations short in EDF only       {len(edf_ops - ga_ops)}")
print(f"  operations short in BOTH           {len(op_inter)} "
      f"(GA {len(ga_ops)}, EDF {len(edf_ops)})")
print(f"  OPERATION-level Jaccard            {op_jac:.4f}")
print(f"  order-level Jaccard                {len(inter)/max(1,len(union)):.4f}")
print()
print("  The ORDER-level figure is the weaker claim: an order counts as shared")
print("  if ANY of its operations is short in both arms, and orders hold ~8.6")
print("  operations each. Operation-level is the one that speaks to whether")
print("  the arms fail on the same WORK. A high order-level overlap alongside")
print("  a much lower operation-level one means the arms hit the same orders")
print("  in different places -- it does NOT establish identical degradation.")

banner("DELIVERY KPIs FOR THE SAME TWO RUNS (for reference, still staffing-relaxed)")
print(f"  {'arm':<6}{'on-time%':>10}{'orders':>8}{'late':>7}{'makespan(d)':>13}"
      f"{'mean_tard_late(h)':>19}{'max_tard(h)':>13}")
for arm, res in (('GA', ga), ('EDF', edf)):
    a = res['aggregate']
    print(f"  {arm:<6}{a['on_time_pct']:>9.1f}%{a['orders']:>8}{a['orders_late']:>7}"
          f"{a['makespan_days']:>13.2f}{a['mean_tardiness_h']:>19.2f}{a['max_tardiness_h']:>13.2f}")

out = {
    'snapshot': SNAPSHOT_NAME, 'origin': ORIGIN.isoformat(), 'seed': SEED,
    'commit_size': commit_size, 'concurrent_operators': capacity,
    'initial_reserved_operators': len(base_reservations),
    'staffing': {arm: {k: v for k, v in prof[arm].items() if k != 'operations'}
                 for arm in prof},
    'labour_accounting': acct,
    'unfilled_qualifier': UNFILLED_QUALIFIER,
    'order_overlap': {'ga_only': sorted(ga_o - edf_o), 'edf_only': sorted(edf_o - ga_o),
                      'both': sorted(inter),
                      'jaccard_order_level': len(inter) / max(1, len(union))},
    'operation_overlap': {'ga_only': len(ga_ops - edf_ops),
                          'edf_only': len(edf_ops - ga_ops),
                          'both': len(op_inter),
                          'jaccard_operation_level': op_jac},
    'delivery': {'GA': ga['aggregate'], 'EDF': edf['aggregate']},
    'total_seconds': time.time() - _T0,
}
path = os.path.join(OUTPUT_DIR, "paired_staffing_validation.json")
with open(path, "w", encoding="utf-8") as f:
    json.dump(out, f, indent=2, default=str)
from utils.run_artifacts import stamp_artifact
stamp_artifact(path)
banner("DONE")
print(f"total runtime: {out['total_seconds']/60:.1f} min")
print(f"written: {path}")
