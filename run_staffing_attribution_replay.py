"""
Part 4 -- replay the SAVED plans under four reservation/segmentation models.

A TRUE REPLAY, not a re-optimisation. The exported operation intervals are
held fixed -- same line, same machine count, same start, same duration -- and
only the worker-assignment model varies. Re-running the GA would change the
schedule itself (machine state now starts with committed fixed work), which
would confound "the shortage was mismeasured" with "the plan changed".

Four conditions, so each fix's contribution is separable rather than only
their combination:

    A  keep=None   + continuous  -- the ORIGINAL measurement
    B  keep=fixed  + continuous  -- Part 1 (reservation identity) alone
    C  keep=None   + segmented   -- Part 2 (execution segmentation) alone
    D  keep=fixed  + segmented   -- both, the corrected model

A->B isolates the stale-reservation artefact; A->C isolates the
continuity-requirement artefact; D is what the corrected measurement says.
B and C together also reveal whether the two causes interact or are additive.

Writes nothing to the database. Re-runs no GA.
"""
import copy
import datetime as dt
import glob
import json
import os
import sys
import time

import pandas as pd
import torch

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

SNAPSHOT_NAME = "authoritative_20260911"

import database.ga_snapshot as ga_snapshot
ga_snapshot.activate_snapshot(SNAPSHOT_NAME)
ORIGIN = ga_snapshot.get_snapshot_origin(SNAPSHOT_NAME)
print(f"Frozen origin timestamp: {ORIGIN}", flush=True)

from database.db import get_engine
from utils.run_artifacts import RunIdentityError, require_same_run
from database.queries import (RESERVATION_FIXED, fetch_assignable_operators,
                              fetch_committed_operator_reservations,
                              fetch_concurrent_operator_capacity,
                              fetch_line_factory_map,
                              fetch_operator_reservation_records,
                              reservation_composition)
from graph.graph_builder import FactoryGraphBuilder
from graph.feature_schema import load_preprocessing
from models.gnn import MultiTaskFactoryGNN
from agent.decision_engine import AIDecisionEngine
from utils.config import GNN_HIDDEN_CHANNELS, OUTPUT_DIR

_T0 = time.time()
EIGHT_HOURS = 480.0


def banner(t):
    print("\n" + "=" * 78 + "\n" + t + "\n" + "=" * 78, flush=True)


EXPECTED_OPERATIONS = 7809       # the graph's operation-node count


def load_export(prefix, expected_rows=EXPECTED_OPERATIONS):
    """
    Load one arm's exported windows, validating provenance before use.

    THREE GUARDS, each for a defect that already bit this replay once:

      1. A GLOB matched proposed_schedule_w5.json -- a 2,579-row leftover
         from an unrelated five-window run -- inflating one arm to 10,388
         operations. Files are now enumerated from an arm-specific prefix
         with a contiguous window sequence, and anything outside it is
         reported rather than silently swept in.

      2. ROW PARITY. The windows must sum to the graph's operation count.
         A stale or missing window changes this total, so it is asserted
         rather than assumed.

      3. RUN IDENTITY, via utils/run_artifacts.require_same_run, which
         checks each file's recorded run_id AND its sha256. Both arms of a
         paired run share one run_id, so a GA export from one run paired
         against an EDF export from another fails here. mtime was what let
         that through last time -- the GA files were timestamped 05:02-05:17
         from the authoritative comparison while EDF's were 00:25 from the
         paired run, and nothing checked.
    """
    pattern = os.path.join(OUTPUT_DIR, f'proposed_schedule_{prefix}_w*.json')
    files = sorted(f for f in glob.glob(pattern) if not f.endswith('.run.json'))
    if not files:
        raise FileNotFoundError(f'No exports for arm prefix {prefix!r} ({pattern})')
    # Contiguous window sequence, so a gap or an extra is visible.
    def window_of(path):
        stem = os.path.basename(path).rsplit('_w', 1)[-1]
        return int(stem.split('.')[0])
    windows = sorted(window_of(f) for f in files)
    if windows != list(range(1, len(windows) + 1)):
        raise ValueError(f'{prefix}: window files are not a contiguous 1..N set: {windows}')
    run_id = require_same_run(files)          # raises RunIdentityError on mismatch
    rows = []
    for f in sorted(files, key=window_of):
        with open(f, encoding='utf-8') as fh:
            rows += json.load(fh)
    if len(rows) != expected_rows:
        raise ValueError(
            f'{prefix}: exported operations {len(rows)} != expected {expected_rows} '
            f'across windows {windows} -- a stale or missing window')
    print(f"  {prefix}: {len(rows)} operations across windows {windows}, run_id={run_id}")
    return rows


banner("CONTEXT (graph + GNN only, for routing types and priorities)")
builder = FactoryGraphBuilder()
ckpt = os.path.join(OUTPUT_DIR, "checkpoints", "gnn_multitask_latest.pth")
preprocessing = load_preprocessing(ckpt)
graph, _ = builder.build_graph(preprocessing=preprocessing)
df_lines = pd.read_sql(
    "SELECT line_id, department_id, line_number, target_efficiency_percentage, is_active "
    "FROM Production_Lines WHERE is_active = 1", get_engine())
model = MultiTaskFactoryGNN(metadata=graph.metadata(), hidden_channels=GNN_HIDDEN_CHANNELS,
                            operation_in_dim=preprocessing['operation_schema']['count'])
model.load_state_dict(torch.load(ckpt))
model.eval()
with torch.no_grad():
    out = model(graph.x_dict, graph.edge_index_dict, graph['operation'].type_idx)
priority = {k: out['priority'][i].item() for i, k in enumerate(builder.op_map.keys())}

# required_machine_type is NOT in the export; recover it from the routings the
# schedule was built from, keyed the same way the exporter keys operations.
mtype = {}
for _, r in builder.df_routings.iterrows():
    mtype[(str(r['production_order_id']), str(r['operation_id']))] = r.get('required_machine_type', '')

capacity = fetch_concurrent_operator_capacity()
engine = AIDecisionEngine()
op_index = engine._build_operator_index(
    fetch_assignable_operators(), df_lines, fetch_line_factory_map())

records = fetch_operator_reservation_records(ORIGIN)
comp = reservation_composition(records)
print(f"  reservations: {comp['total']['rows']} rows -> "
      f"{comp[RESERVATION_FIXED]['rows']} fixed / "
      f"{comp['replaceable']['rows']} replaceable")
RES = {
    'all': fetch_committed_operator_reservations(ORIGIN, keep=None),
    'fixed': fetch_committed_operator_reservations(ORIGIN, keep=RESERVATION_FIXED),
}
print(f"  keep=None  -> {sum(len(v) for v in RES['all'].values())} intervals")
print(f"  keep=fixed -> {sum(len(v) for v in RES['fixed'].values())} intervals")

ARM_PREFIX = {'GA': 'paired_ga', 'EDF': 'paired_edf'}
EXPORTS = {a: load_export(p) for a, p in ARM_PREFIX.items()}
# Both arms must come from the SAME paired run, not merely be internally
# consistent -- that is the pairing the whole experiment rests on.
_all_files = []
for pfx in ARM_PREFIX.values():
    _all_files += [f for f in sorted(glob.glob(os.path.join(
        OUTPUT_DIR, f'proposed_schedule_{pfx}_w*.json'))) if not f.endswith('.run.json')]
PAIRED_RUN_ID = require_same_run(_all_files)
print(f"  both arms share run_id={PAIRED_RUN_ID} ({len(_all_files)} files)")

CONDITIONS = [
    ('A', 'all', 'continuous', 'ORIGINAL measurement'),
    ('B', 'fixed', 'continuous', 'Part 1 only (reservation identity)'),
    ('C', 'all', 'segmented', 'Part 2 only (execution segmentation)'),
    ('D', 'fixed', 'segmented', 'BOTH (corrected model)'),
]


def replay(rows, reservations_key, policy):
    """Re-run ONLY worker assignment over the fixed exported intervals."""
    AIDecisionEngine.WORKER_CONTINUITY_POLICY = policy
    reservations = copy.deepcopy(RES[reservations_key])
    busy = {}
    understaffed = []
    required = assigned = 0.0
    for r in rows:
        try:
            start = dt.datetime.fromisoformat(r['proposed_start_time'])
            end = dt.datetime.fromisoformat(r['base_end_time'])
        except (KeyError, ValueError):
            continue
        duration = (end - start).total_seconds() / 60.0
        if duration <= 0:
            continue
        head = int(r.get('machines_used', 0) or 0)
        if head <= 0:
            continue
        key = (str(r['production_order_id']), str(r['operation_id']))
        chosen, _tier, under = AIDecisionEngine._assign_operators(
            op_index, mtype.get(key, ''), int(r['assigned_line_id']), head,
            busy, duration, start_dt=start, operator_reservations=reservations,
            order_priority=priority.get(f"{key[0]}_{key[1]}", 0.5))
        required += duration * head
        assigned += duration * len(chosen)
        if under is not None:
            # STRICT: a record without impact fields must fail loudly. A zero
            # default is precisely how an under-reported shortage hid before.
            understaffed.append({
                'order': key[0], 'op_key': f"{key[0]}_{key[1]}",
                'duration': duration,
                'shortfall': under['shortfall'],
                'shortfall_worker_minutes': under['shortfall_worker_minutes'],
            })
        elif len(chosen) < head:
            raise AssertionError(
                f'{key}: {len(chosen)} of {head} operators assigned with NO '
                f'understaffed record -- a silent-zero path has reappeared')
    unfilled = required - assigned
    long_min = sum(u['shortfall_worker_minutes'] for u in understaffed if u['duration'] > EIGHT_HOURS)
    return {
        'understaffed_operations': len(understaffed),
        'shortfall_headcount': sum(u['shortfall'] for u in understaffed),
        'shortfall_worker_minutes': sum(u['shortfall_worker_minutes'] for u in understaffed),
        'required_worker_minutes': required,
        'assigned_worker_minutes': assigned,
        'unfilled_worker_minutes': unfilled,
        'unfilled_over_required_pct': (100.0 * unfilled / required) if required else None,
        'orders_affected': len({u['order'] for u in understaffed}),
        'op_keys': sorted({u['op_key'] for u in understaffed}),
        'unfilled_in_ops_over_8h': long_min,
    }


results = {}
for code, res_key, policy, label in CONDITIONS:
    banner(f"CONDITION {code} -- {label}   (reservations={res_key}, continuity={policy})")
    results[code] = {'label': label, 'reservations': res_key, 'continuity': policy, 'arms': {}}
    for arm, rows in EXPORTS.items():
        t0 = time.time()
        r = replay(rows, res_key, policy)
        results[code]['arms'][arm] = r
        print(f"  {arm}: understaffed_ops={r['understaffed_operations']}  "
              f"heads={r['shortfall_headcount']}  "
              f"unfilled={r['shortfall_worker_minutes']:,.0f} wkr-min  "
              f"unfilled/required={r['unfilled_over_required_pct']:.2f}%  "
              f"orders={r['orders_affected']}  ({time.time()-t0:.0f}s)")
AIDecisionEngine.WORKER_CONTINUITY_POLICY = 'segmented'

banner("ATTRIBUTION -- how much of the original shortage was artefact?")
for arm in EXPORTS:
    a = results['A']['arms'][arm]
    print(f"\n  --- {arm} ---")
    print(f"  {'condition':<38}{'ops':>7}{'unfilled wkr-min':>19}{'unf/req':>10}{'vs A':>10}")
    for code, _rk, _p, label in CONDITIONS:
        r = results[code]['arms'][arm]
        delta = (100.0 * (r['shortfall_worker_minutes'] - a['shortfall_worker_minutes'])
                 / a['shortfall_worker_minutes']) if a['shortfall_worker_minutes'] else 0.0
        print(f"  {code} {label:<36}"[:40] + f"{r['understaffed_operations']:>7}"
              f"{r['shortfall_worker_minutes']:>19,.0f}"
              f"{(r['unfilled_over_required_pct'] or 0):>9.2f}%{delta:>9.1f}%")
    b, c, d = (results[k]['arms'][arm] for k in ('B', 'C', 'D'))
    tot = a['shortfall_worker_minutes'] - d['shortfall_worker_minutes']
    print(f"  reservation fix alone removed   "
          f"{a['shortfall_worker_minutes']-b['shortfall_worker_minutes']:>15,.0f} wkr-min")
    print(f"  segmentation fix alone removed  "
          f"{a['shortfall_worker_minutes']-c['shortfall_worker_minutes']:>15,.0f} wkr-min")
    print(f"  both together removed           {tot:>15,.0f} wkr-min "
          f"({100.0*tot/a['shortfall_worker_minutes']:.1f}% of the original)")
    single = ((a['shortfall_worker_minutes'] - b['shortfall_worker_minutes'])
              + (a['shortfall_worker_minutes'] - c['shortfall_worker_minutes']))
    print(f"  sum of the two alone            {single:>15,.0f} wkr-min "
          f"-> {'sub-additive (they overlap)' if single > tot else 'super-additive (they interact)' if single < tot else 'exactly additive'}")
    print(f"  REMAINING shortage (condition D){d['shortfall_worker_minutes']:>15,.0f} wkr-min "
          f"= {d['unfilled_over_required_pct']:.2f}% of required")

banner("OPERATION-LEVEL vs ORDER-LEVEL OVERLAP, corrected model (D)")
ga_ops = set(results['D']['arms']['GA']['op_keys'])
edf_ops = set(results['D']['arms']['EDF']['op_keys'])
ga_ord = {k.split('_')[0] for k in ga_ops}
edf_ord = {k.split('_')[0] for k in edf_ops}
print(f"  operations: GA {len(ga_ops)}  EDF {len(edf_ops)}  both {len(ga_ops & edf_ops)}")
print(f"  OPERATION-level Jaccard {len(ga_ops & edf_ops)/max(1,len(ga_ops | edf_ops)):.4f}")
print(f"  order-level Jaccard     {len(ga_ord & edf_ord)/max(1,len(ga_ord | edf_ord)):.4f}")
print("  Order-level counts an order as shared if ANY of its ~8.6 operations is")
print("  short in both arms, so it overstates agreement. Operation-level is the")
print("  figure that speaks to whether the arms fail on the same WORK.")

out = {
    'snapshot': SNAPSHOT_NAME, 'origin': ORIGIN.isoformat(),
    'reservation_composition': comp,
    'replay_of_saved_plans': True, 're_optimised': False,
    'paired_run_id': PAIRED_RUN_ID,
    'conditions': {c: {'label': l, 'reservations': rk, 'continuity': p}
                   for c, rk, p, l in CONDITIONS},
    'results': {c: {a: {k: v for k, v in r.items() if k != 'op_keys'}
                    for a, r in results[c]['arms'].items()} for c in results},
    'operation_overlap_corrected': {
        'ga_only': len(ga_ops - edf_ops), 'edf_only': len(edf_ops - ga_ops),
        'both': len(ga_ops & edf_ops),
        'jaccard_operation_level': len(ga_ops & edf_ops) / max(1, len(ga_ops | edf_ops)),
        'jaccard_order_level': len(ga_ord & edf_ord) / max(1, len(ga_ord | edf_ord))},
    'qualifier': ("unfilled ASSIGNMENT-minutes under a greedy single-worker-per-lot "
                  "policy over FIXED exported intervals -- not a proven minimum "
                  "physical shortage, and not a re-optimised schedule"),
    'total_seconds': time.time() - _T0,
}
path = os.path.join(OUTPUT_DIR, "staffing_attribution_replay.json")
with open(path, "w", encoding="utf-8") as f:
    json.dump(out, f, indent=2, default=str)
banner("DONE")
print(f"total runtime: {out['total_seconds']/60:.1f} min")
print(f"written: {path}")
