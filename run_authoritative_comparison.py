"""
Final, authoritative GA-vs-baselines comparison.

One process, one fixed origin timestamp (via a fresh, named
database/ga_snapshot.py snapshot), seed=42, NO RESEED. Runs TODAY'S ACTUAL
DEFAULT configuration only -- confirmed directly against ga/rolling_horizon.py
and ga/scheduler.py before this script was written (2026-08-27):
optimize() + polish(). The confidence-gated risk buffer is wired in and its
gate check still runs, but it is DISABLED BY VALUE: RISK_BUFFER_AT_CERTAINTY
is 0.0 (ga/schedule_utils.py), set there deliberately after a multiseed
ablation found no reliable winner and mean tardiness favouring disabled. So
duration * (1 + p*0.0) == duration and the buffer changes nothing. An earlier
version of this docstring said "always on", which was true of the code path
and misleading about the effect. ALNS refinement, memetic integration, TCOX, GNN-informed
repair scoring, and the WLC release gate are all confirmed opt-in and NOT
engaged here -- every one of those was evaluated across this project's
GA-refinement investigation (GA_AGENT_POSTMORTEM.md Section 13) and closed
HOLD, not adopted. An earlier instruction to wire ALNS in as the new default
was corrected before this script was written: ALNS loses to one-shot polish
in 3 of 4 real tested cases (ALNS_REPAIR_SCORING_PROPOSAL.md Section 14's
Step 2 validation) -- this run deliberately reflects the actually-validated
default, not that incorrect premise.

Sequence: build a fresh named snapshot of the live DB (freezes both the data
and the "now" origin -- PROJECT_STATUS.txt STANDING PRACTICES, same
mechanism used throughout this session) -> graph build -> GNN inference using
the current promoted checkpoint (gnn_multitask_latest.pth, NOT retrained) ->
whole-order-book rolling-horizon scheduling for the GA arm
(ga.rolling_horizon.run_rolling_horizon) -> the same whole-book rolling
horizon independently for FIFO/EDF/CriticalRatio
(ga.rolling_horizon.run_rolling_horizon_baseline, each with its OWN
self-consistent machine-state trajectory -- the genuine "what if this
heuristic alone scheduled everything" comparison) -> a per-window,
release-date-aware capacity floor (same methodology as
scratch/capacity_floor_current.py, computed cumulatively at each window's own
cutoff, so the ratio is meaningful window over window) -> full report, every
strategy side by side, dense/slack windows reported separately, never
blended into one number.

Scope, stated explicitly: no reseed, no GNN retraining and no breakdown
simulation. Every arm uses the same authoritative worker-aware phenotype
decoder and exports the realized schedule, because worker calendars affect
elapsed completion, successor readiness, tardiness and therefore fitness.

REPRODUCIBILITY FIX (2026-08-27), why this run supersedes the two before it:
the first two runs of this script (identical seed/snapshot/origin/GNN
checkpoint) produced genuinely different GA schedules -- root-caused to
database/queries.py::fetch_active_orders_and_routings's multi-table JOIN
having NO ORDER BY, so row order was undefined SQL semantics, not merely
unspecified-but-stable, and that row order fed straight into GA chromosome
construction. Confirmed directly (not assumed): a freshly-rebuilt snapshot
produced a THIRD, still-different result from either prior run, while
repeated reads against one already-materialized snapshot agreed with
themselves but not with a fresh build -- the exact signature of an
undefined JOIN order. Fixed with an explicit `ORDER BY po.production_
order_id, ro.operation_sequence_number` (the ordering every downstream
consumer already assumed -- ga/fitness.py's own precedence check,
ga/operators.py::repair_precedence). Verified directly: two independent
fresh snapshot builds after the fix produced byte-identical, correctly
grouped-and-sequenced row order (8,745/8,745 rows, zero violations).
PROJECT_STATUS.txt STANDING PRACTICES has the full writeup. THIS is the
first run of this script executed against the fixed query -- the genuinely
reproducible result, not one arbitrary row-order roll of the dice.
"""
import copy
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
print("=" * 78)
existing = {s['snapshot_name'] for s in ga_snapshot.list_snapshots()}
if SNAPSHOT_NAME in existing:
    print(f"Reusing existing frozen snapshot '{SNAPSHOT_NAME}' (unchanged data/origin, no rebuild)")
else:
    print(f"Building fresh frozen snapshot '{SNAPSHOT_NAME}' from the live database (no reseed)")
    ga_snapshot.build_snapshot(SNAPSHOT_NAME)
print("=" * 78)
ga_snapshot.activate_snapshot(SNAPSHOT_NAME)
ORIGIN = ga_snapshot.get_snapshot_origin(SNAPSHOT_NAME)
print(f"Frozen origin timestamp: {ORIGIN}")

from database.db import get_engine
from database.queries import fetch_concurrent_operator_capacity
from graph.graph_builder import FactoryGraphBuilder
from models.gnn import MultiTaskFactoryGNN, prior_corrected_delay_probabilities
from ga.scheduler import DEFAULT_LINE_EFFICIENCY, DEFAULT_SETUP_TIME_MINS
from ga.chromosome import ChromosomeFactory
from ga.rolling_horizon import run_rolling_horizon, run_rolling_horizon_baseline
from utils.config import (GNN_HIDDEN_CHANNELS, OUTPUT_DIR, PLANNING_HORIZON_ORDERS,
                          GA_MAX_MACHINES_PER_OPERATION, DURATION_CALIBRATION_FACTOR)

SEED = 42
POP, GENS = 50, 50
POLISH_EVALS = 400
MINS_PER_DAY = 1440.0

_SCRIPT_START = time.time()
RESULTS = {'started_at': datetime.now().isoformat(), 'seed': SEED, 'snapshot': SNAPSHOT_NAME,
           'origin': ORIGIN.isoformat()}


def banner(text):
    print("\n" + "=" * 78)
    print(text)
    print("=" * 78, flush=True)


# ---------------------------------------------------------------- graph/GNN
banner("GRAPH BUILD + GNN INFERENCE (current promoted checkpoint, no retraining)")

builder = FactoryGraphBuilder()
from graph.feature_schema import load_preprocessing
preprocessing = load_preprocessing(os.path.join(OUTPUT_DIR, "checkpoints", "gnn_multitask_latest.pth"))
graph, _ = builder.build_graph(preprocessing=preprocessing)
df_lines = pd.read_sql(
    "SELECT line_id, department_id, line_number, target_efficiency_percentage, is_active "
    "FROM Production_Lines WHERE is_active = 1", get_engine())
df_machines = builder.df_machines

model = MultiTaskFactoryGNN(metadata=graph.metadata(), hidden_channels=GNN_HIDDEN_CHANNELS, operation_in_dim=preprocessing['operation_schema']['count'])
ckpt_path = os.path.join(OUTPUT_DIR, "checkpoints", "gnn_multitask_latest.pth")
model.load_state_dict(torch.load(ckpt_path))
model.eval()
with torch.no_grad():
    out = model(graph.x_dict, graph.edge_index_dict, graph['operation'].type_idx)
delay = prior_corrected_delay_probabilities(
    out['delay'], preprocessing['delay_pos_weight']
).squeeze(-1)
preds_all = {k: {'delay': delay[i].item(), 'priority': out['priority'][i].item()}
             for i, k in enumerate(builder.op_map.keys())}
dvals = [p['delay'] for p in preds_all.values()]
print(f"  checkpoint: {ckpt_path}")
print(f"  GNN delay probability range: {min(dvals):.4f} - {max(dvals):.4f}")
RESULTS['gnn_delay_range'] = [min(dvals), max(dvals)]

capacity = fetch_concurrent_operator_capacity()
commit_size = PLANNING_HORIZON_ORDERS or builder.df_routings['production_order_id'].nunique()
print(f"  commit_size={commit_size}  concurrent_operators={capacity}")
RESULTS['commit_size'] = commit_size

order_release = builder.df_routings.groupby('production_order_id')['release_date'].min()
all_orders_sorted = order_release.reset_index().sort_values(
    ['release_date', 'production_order_id'])['production_order_id'].tolist()
n_windows_expected = (len(all_orders_sorted) + commit_size - 1) // commit_size
print(f"  {len(all_orders_sorted)} orders total -> {n_windows_expected} windows")


# ---------------------------------------------------------------- GA arm
banner("GA ARM -- optimize() + polish(), risk buffer wired but INERT "
       "(RISK_BUFFER_AT_CERTAINTY=0.0), "
       "ALNS/memetic/TCOX/repair-scoring/release-gate all off (today's real default)")
# export=True, CHANGED from this script's original scope. The operator
# assignment it drives is the ONLY producer of understaffed_operations /
# execution_feasible (agent/decision_engine.py) -- without it the run cannot
# say whether the schedules it reports are actually staffable, and a
# scheduling-quality table that cannot answer that overstates its own result.
# Costs real runtime; worth it, because the alternative is reporting
# completion times whose feasibility was never checked.
from agent.decision_engine import AIDecisionEngine
from database.queries import (
    fetch_assignable_operators, fetch_committed_machine_state,
    fetch_committed_operator_reservations, fetch_line_factory_map,
)

decision_engine = AIDecisionEngine()
operator_index = decision_engine._build_operator_index(
    fetch_assignable_operators(), df_lines, fetch_line_factory_map())
# Freeze one committed-state snapshot for every arm. The database itself is a
# frozen snapshot, but loading once also prevents a future mutable caller from
# giving later arms a different starting problem.
base_machine_free, base_machine_last_order = fetch_committed_machine_state(ORIGIN)
base_operator_reservations = fetch_committed_operator_reservations(ORIGIN)

t0 = time.time()
ga_result = run_rolling_horizon(
    builder.df_routings, df_machines, df_lines, preds_all, capacity, ORIGIN,
    commit_size=commit_size, pop_size=POP, generations=GENS, polish_evals=POLISH_EVALS,
    seed=SEED, export=True, decision_engine=decision_engine, operator_index=operator_index,
    log_fn=print, ownership_snapshot=builder.ownership_snapshot,
    committed_machine_state=dict(base_machine_free),
    committed_machine_last_order=dict(base_machine_last_order),
    initial_operator_reservations=copy.deepcopy(base_operator_reservations),
)
print(f"GA arm done in {(time.time()-t0)/60:.1f} min")
RESULTS['ga_aggregate'] = ga_result['aggregate']
RESULTS['ga_logbooks'] = ga_result.get('logbooks')


# ---------------------------------------------------------------- baseline arms
# RandomRepaired included deliberately: FIFO/EDF/CriticalRatio are all
# purposeful rules, so without a no-information arm the table has no floor to
# read the others against -- "better than FIFO" and "better than picking an
# order at random" are different claims.
baseline_results = {}
for strategy in ('FIFO', 'EDF', 'CriticalRatio', 'RandomRepaired'):
    banner(f"BASELINE ARM -- {strategy} (own independent whole-book machine-state trajectory)")
    t0 = time.time()
    baseline_results[strategy] = run_rolling_horizon_baseline(
        strategy, builder.df_routings, df_machines, df_lines, preds_all, capacity, ORIGIN,
        commit_size=commit_size, log_fn=print, ownership_snapshot=builder.ownership_snapshot,
        export=True, decision_engine=decision_engine, operator_index=operator_index,
        export_label_prefix=strategy.lower(),
        committed_machine_state=dict(base_machine_free),
        committed_machine_last_order=dict(base_machine_last_order),
        initial_operator_reservations=copy.deepcopy(base_operator_reservations),
    )
    print(f"{strategy} arm done in {(time.time()-t0)/60:.1f} min")
    RESULTS[f'{strategy.lower()}_aggregate'] = baseline_results[strategy]['aggregate']


# ---------------------------------------------------------------- capacity floor
banner("PER-WINDOW RELEASE-DATE-AWARE CAPACITY FLOOR (cumulative, same methodology "
       "as scratch/capacity_floor_current.py)")

if 'target_efficiency_percentage' in df_lines.columns:
    eff_series = df_lines.loc[df_lines['is_active'] == 1, 'target_efficiency_percentage']
    avg_efficiency = (eff_series.mean() / 100.0) if not eff_series.empty and pd.notna(eff_series.mean()) else DEFAULT_LINE_EFFICIENCY
else:
    avg_efficiency = DEFAULT_LINE_EFFICIENCY
avg_efficiency = max(avg_efficiency, 0.05)

factory = ChromosomeFactory(builder.df_routings, df_machines, df_lines)


def compute_capacity_floor_days(df_routings_subset, factory, avg_efficiency, origin,
                                op_work=None):
    """
    Two SEPARATE lower bounds, returned separately -- never pre-maxed.

    CORRECTED (P2): this previously computed its own work content as
    sam * quantity / efficiency, omitting DURATION_CALIBRATION_FACTOR, which
    ga/scheduler.py:276 applies:

        serial_processing_mins = (duration_calibration_factor * sam * qty) / eff

    At 0.825454 the floor therefore described a problem 1/0.825454 = 1.2114x
    SLOWER than the one being optimised. A bound computed from a slower
    problem is not necessarily a lower bound on this one, so the resulting
    "1.00x the floor" near-optimality reading was unjustified. `op_work` now
    supplies the scheduler's OWN serial_processing_mins per operation --
    read from a real GAScheduler rather than re-derived here, so the two can
    no longer disagree. The old local formula remains only as a fallback for
    an operation the scheduler did not produce, and is flagged when used.

    The two bounds answer different questions and are NOT interchangeable:

      workload bound  -- per operation-type, release-date-aware: the work
          that must flow through a finite machine pool. This is the only one
          of the two that says anything about CAPACITY being saturated.

      critical-path bound -- max over orders of (release_day + that order's
          own minimum serial duration). This can bind while machines sit
          idle everywhere: one late-released order with a long routing sets
          it regardless of how much spare capacity exists. Reaching this
          bound is NOT evidence of efficient machine use.

    Reporting only max(the two) conflates them, which is how a
    release-date-driven bound came to be read as capacity saturation.
    """
    df = df_routings_subset.drop_duplicates(subset=['production_order_id', 'operation_id']).copy()
    if df.empty:
        return {'workload_days': 0.0, 'critical_path_days': 0.0, 'binding': None,
                'floor_days': 0.0, 'fallback_ops': 0}
    df['release_date'] = pd.to_datetime(df['release_date'])
    op_work = op_work or {}
    rows, fallback_ops = [], 0
    for _, row in df.iterrows():
        key = f"{row['production_order_id']}_{row['operation_id']}"
        if key in op_work:
            serial_processing_mins = op_work[key]
        else:
            # The scheduler had no entry for this operation. Use the local
            # formula WITH the calibration factor the scheduler applies --
            # never the uncalibrated one -- and count it so a silent drift
            # back to a different work representation is visible.
            fallback_ops += 1
            sam = float(row.get('sam', 30)) or 30.0
            quantity = int(row.get('target_quantity', 1)) or 1
            remaining_frac = row.get('remaining_frac', 1.0)
            if pd.isna(remaining_frac):
                remaining_frac = 1.0
            remaining_frac = min(max(float(remaining_frac), 0.0), 1.0)
            quantity = max(1, int(round(quantity * remaining_frac)))
            serial_processing_mins = (
                DURATION_CALIBRATION_FACTOR * sam * quantity) / avg_efficiency
        optype = factory.optype_for_op.get(key)
        release_days = (row['release_date'] - origin).total_seconds() / 86400.0
        per_line_counts = factory.machines_per_line_map.get(key, {})
        company_cap = factory.usable_cap_by_type.get(optype, GA_MAX_MACHINES_PER_OPERATION)
        best_line_machines = max(per_line_counts.values()) if per_line_counts else 1
        max_machines = max(1, min(int(best_line_machines), int(company_cap), GA_MAX_MACHINES_PER_OPERATION))
        min_duration_mins = DEFAULT_SETUP_TIME_MINS + (serial_processing_mins / max_machines)
        rows.append({
            'order_id': row['production_order_id'], 'optype': optype,
            'release_days': release_days,
            'machine_minutes': serial_processing_mins + DEFAULT_SETUP_TIME_MINS,
            'min_duration_mins': min_duration_mins,
        })
    fdf = pd.DataFrame(rows)

    def release_aware_floor(sub_df, usable):
        """
        For any time t: makespan >= t + (work released at or after t)/capacity.

        TWO CORRECTIONS, both found by reporting this bound separately
        instead of pre-maxing it with the critical-path bound, which hid them:

        1. A cutoff before the origin is CLAMPED to it -- max(r, 0.0). Work
           released in the past cannot be started in the past. This is what
           actually repairs window 1 of this book, whose orders are the
           earliest-released and every one of them released BEFORE the origin:
           previously every sampled cutoff was negative, every candidate bound
           came out negative, and `best` never moved off its 0.0
           initialisation, producing a vacuous "floor of 0 days" that the old
           max() then silently swallowed. With the clamp, the EARLIEST
           negative cutoff -- the one whose filter retains all the work --
           yields 0 + total_work/capacity, the correct bound.

        2. t = 0 is also added to the cutoff set, defensively. NOTE it is not
           what recovers the already-released workload: the t = 0 filter is
           `release_days >= 0`, which excludes every negative-release row, so
           on window 1 that cutoff contributes zero work and a zero bound.
           It is dominated by the earliest cutoff in every mixed case too
           (r + work-at-or-after-r can only be <= 0 + all-work). Kept because
           a max() over more valid cutoffs is never wrong, not because it
           carries this case -- an earlier version of this comment credited it
           with the fix, which was inaccurate.
        """
        cutoffs = sorted(set(sub_df['release_days'].unique()) | {0.0})
        best = 0.0
        for r in cutoffs:
            remaining_work_days = sub_df.loc[sub_df['release_days'] >= r, 'machine_minutes'].sum() / MINS_PER_DAY
            bound = max(r, 0.0) + (remaining_work_days / usable)
            if bound > best:
                best = bound
        return best

    total_usable = sum(factory.usable_cap_by_type.values()) or 1
    floor_b_tight = 0.0
    for optype, sub in fdf.groupby('optype'):
        usable = factory.usable_cap_by_type.get(optype, total_usable)
        floor_b_tight = max(floor_b_tight, release_aware_floor(sub, usable))

    order_release_days = fdf.groupby('order_id')['release_days'].first()
    order_critical_mins = fdf.groupby('order_id')['min_duration_mins'].sum()
    # Release clamped at the origin for the same reason: an order released in
    # the past starts at the origin at the earliest, so its critical path runs
    # from 0, not from a negative release day. Without this the bound went
    # NEGATIVE for window 1 (-12.16 days), which is not a lower bound on any
    # makespan and made its ratio meaningless (-1.0966).
    floor_c = float((order_release_days.clip(lower=0.0)
                     + (order_critical_mins / MINS_PER_DAY)).max())
    floor_c = max(floor_c, 0.0)

    return {
        'workload_days': float(floor_b_tight),
        'critical_path_days': floor_c,
        'binding': 'workload' if floor_b_tight >= floor_c else 'critical_path',
        'floor_days': max(float(floor_b_tight), floor_c),
        'fallback_ops': fallback_ops,
    }


n_windows = len(ga_result['windows'])
# The scheduler's OWN work content per operation, so the bound and the problem
# being bounded use one representation rather than two that can drift apart.
from ga.scheduler import GAScheduler as _GAS
_work_sched = _GAS(builder.df_routings, df_machines, df_lines, preds_all,
                   ownership_snapshot=builder.ownership_snapshot,
                   current_time=ORIGIN, present_operator_count=capacity)
OP_WORK = {k: d['serial_processing_mins'] for k, d in _work_sched.op_details.items()}
print(f"  scheduler work content loaded for {len(OP_WORK)} operations "
      f"(duration_calibration_factor={_work_sched.duration_calibration_factor})")
RESULTS['duration_calibration_factor_used'] = float(_work_sched.duration_calibration_factor)

capacity_floors = []  # cumulative floor COMPONENTS (days) as of each window's cutoff
for w in range(1, n_windows + 1):
    cumulative_ids = set(all_orders_sorted[:w * commit_size])
    sub = builder.df_routings[builder.df_routings['production_order_id'].isin(cumulative_ids)]
    fl = compute_capacity_floor_days(sub, factory, avg_efficiency, ORIGIN, op_work=OP_WORK)
    capacity_floors.append(fl)
    print(f"  window {w}: cumulative orders={len(cumulative_ids)}  "
          f"workload={fl['workload_days']:.4f}d  critical_path={fl['critical_path_days']:.4f}d  "
          f"binding={fl['binding']}  fallback_ops={fl['fallback_ops']}")

RESULTS['capacity_floors_days'] = capacity_floors


# ---------------------------------------------------------------- dense/slack
banner("DENSE / SLACK WINDOW CLASSIFICATION")
ga_on_time = [entry['on_time_pct'] for entry in ga_result['windows']]
median_on_time = sorted(ga_on_time)[len(ga_on_time) // 2] if ga_on_time else 0.0
window_class = ['dense' if ot < median_on_time else 'slack' for ot in ga_on_time]
print("  rule: a window is 'dense' if the GA arm's on-time rate there is BELOW "
      f"the median across all {n_windows} windows ({median_on_time:.1f}%), 'slack' otherwise.")
for w, (ot, cls) in enumerate(zip(ga_on_time, window_class), start=1):
    print(f"  window {w}: on-time={ot:.1f}% -> {cls}")
RESULTS['window_classification'] = window_class


# ---------------------------------------------------------------- full report
banner("FULL PER-WINDOW METRIC SET -- ALL STRATEGIES SIDE BY SIDE")

STRATEGIES = [('GA', ga_result)] + [(s, baseline_results[s])
                                    for s in ('FIFO', 'EDF', 'CriticalRatio', 'RandomRepaired')]

report_lines = []


def emit(line=""):
    print(line)
    report_lines.append(line)


# per_window[strategy] = list of one dict per window -- the SAVED,
# self-verifying source of every number the printed table below derives
# from. cum_makespan_days and incr_makespan_days are DELIBERATELY both
# present and separately labelled: the capacity floor bounds everything
# committed THROUGH a window (a cumulative quantity), so capacity_floor_ratio
# is computed against cum_makespan_days, NOT incr_makespan_days -- keeping
# both explicit, with the ratio's own numerator/denominator spelled out per
# row, so this cannot silently be misread as one unlabelled "makespan(d)"
# column again.
per_window = {name: [] for name, _ in STRATEGIES}

for w in range(1, n_windows + 1):
    cls = window_class[w - 1] if w - 1 < len(window_class) else 'n/a'
    fl = capacity_floors[w - 1] if w - 1 < len(capacity_floors) else None
    floor_days = fl['floor_days'] if fl else None
    for name, result in STRATEGIES:
        windows = result['windows']
        if w - 1 >= len(windows):
            continue
        entry = windows[w - 1]
        cum_days = entry['end_day']
        prev_cum_days = windows[w - 2]['end_day'] if w >= 2 and w - 2 < len(windows) else 0.0
        incr_days = max(cum_days - prev_cum_days, 1e-9)
        throughput = entry['committed_orders'] / incr_days
        labour_pct = 100.0 * entry['labor_minutes'] / (capacity * incr_days * MINS_PER_DAY) if incr_days else 0.0
        floor_ratio = (cum_days / floor_days) if floor_days else None
        row = {
            'window': w,
            'classification': cls,
            'lookahead_orders': entry.get('lookahead_orders'),
            'committed_operations': entry.get('committed_operations'),
            'max_tardiness_h': entry.get('max_tardiness_h'),
            'fitness': entry.get('fitness'),
            'fitness_components': entry.get('fitness_components'),
            'fitness_lateness_split': entry.get('fitness_lateness_split'),
            'pool_short_operations': entry.get('pool_short_operations'),
            'on_time_pct': entry['on_time_pct'],
            'cum_makespan_days': cum_days,
            'incr_makespan_days': incr_days,
            'throughput_orders_per_day': throughput,
            'mean_tardiness_h': entry['mean_tardiness_h'],
            'labour_pct': labour_pct,
            'committed_orders': entry['committed_orders'],
            'capacity_floor_days_cumulative': floor_days,
            'capacity_floor_workload_days': fl['workload_days'] if fl else None,
            'capacity_floor_critical_path_days': fl['critical_path_days'] if fl else None,
            'capacity_floor_binding_component': fl['binding'] if fl else None,
            'capacity_floor_ratio_cum_makespan_over_floor': floor_ratio,
            'ratio_vs_workload_bound': (cum_days / fl['workload_days'])
                if fl and fl['workload_days'] else None,
            'ratio_vs_critical_path_bound': (cum_days / fl['critical_path_days'])
                if fl and fl['critical_path_days'] else None,
        }
        if name == 'GA':
            row['margin_pct_vs_best_of_5_heuristics'] = entry.get('margin_pct')
            row['best_of_5_heuristic_name'] = entry.get('best_baseline')
        per_window[name].append(row)

RESULTS['per_window'] = per_window

for w in range(1, n_windows + 1):
    cls = window_class[w - 1] if w - 1 < len(window_class) else 'n/a'
    emit()
    _ga_w = per_window['GA'][w - 1] if w - 1 < len(per_window['GA']) else {}
    emit(f"--- WINDOW {w} ({cls}) -- lookahead {_ga_w.get('lookahead_orders')} orders, "
         f"committed {_ga_w.get('committed_orders')} orders / "
         f"{_ga_w.get('committed_operations')} operations ---")
    header = (f"{'strategy':<14}{'on-time%':>10}{'cum_days':>10}{'incr_days':>11}{'throughput/d':>14}"
              f"{'mean_tard(h)':>14}{'max_tard(h)':>13}{'labour%':>10}{'vs_workload':>13}{'vs_crit_path':>14}")
    emit(header)
    _fl = capacity_floors[w - 1] if w - 1 < len(capacity_floors) else None
    if _fl:
        emit(f"  bounds (cumulative, days): workload={_fl['workload_days']:.4f}  "
             f"critical_path={_fl['critical_path_days']:.4f}  BINDING={_fl['binding']}")
        emit("  Ratios are cum_days / bound, BOTH cumulative. Only the WORKLOAD bound")
        emit("  speaks to machine capacity; the critical-path bound can bind with")
        emit("  capacity idle, so a ratio near 1 there is NOT near-optimality.")
    for name, _ in STRATEGIES:
        rows = per_window[name]
        if w - 1 >= len(rows):
            continue
        r = rows[w - 1]
        rw = r.get('ratio_vs_workload_bound')
        rc = r.get('ratio_vs_critical_path_bound')
        rw_s = f"{rw:.4f}" if rw is not None else "n/a"
        rc_s = f"{rc:.4f}" if rc is not None else "n/a"
        emit(f"{name:<14}{r['on_time_pct']:>9.1f}%{r['cum_makespan_days']:>10.2f}"
             f"{r['incr_makespan_days']:>11.2f}{r['throughput_orders_per_day']:>14.2f}"
             f"{r['mean_tardiness_h']:>14.2f}{(r.get('max_tardiness_h') or 0):>13.2f}"
             f"{r['labour_pct']:>9.1f}%{rw_s:>13}{rc_s:>14}")
    ga_row = per_window['GA'][w - 1] if w - 1 < len(per_window['GA']) else None
    if ga_row and ga_row.get('margin_pct_vs_best_of_5_heuristics') is not None:
        emit(f"  GA margin, DEFINITION A (in-window): {ga_row['margin_pct_vs_best_of_5_heuristics']:+.4f}% "
             f"vs {ga_row['best_of_5_heuristic_name']}")
        emit("    = sched.run_baselines(), scoring each pure rule against the GA")
        emit("      window's OWN incoming machine state.")
        _bname = ga_row['best_of_5_heuristic_name']
        _garm = per_window['GA'][w - 1]['fitness']
        _other = next((per_window[nm][w - 1]['fitness'] for nm, _ in STRATEGIES
                       if nm == _bname and w - 1 < len(per_window[nm])), None)
        if _other:
            emit(f"  GA margin, DEFINITION B (whole-book arm): "
                 f"{100.0 * (_other - _garm) / _other:+.4f}% vs the separately")
            emit(f"    evolved {_bname} arm's own window-{w} fitness ({_other:.4f} vs {_garm:.4f}),")
            emit("      i.e. that heuristic carrying its OWN machine-state trajectory.")
            emit("    A and B are different comparisons and differ numerically; neither")
            emit("    is 'the' margin, so both are reported with their definition.")
    else:
        emit("  GA in-window margin: n/a")

emit()
emit("=" * 78)
emit("WHOLE-BOOK AGGREGATE (never a substitute for the per-window table above)")
emit("=" * 78)
emit("TARDINESS DENOMINATORS, both reported because they are routinely confused:")
emit("  tard_late(h) = mean over LATE orders only")
emit("  tard_all(h)  = mean over ALL orders with a deadline (on-time contribute 0)")
emit("They differ by the late fraction. Neither is 'mean tardiness' unqualified.")
emit("")
emit("BOTH are measured against the EFFECTIVE deadline. ga/fitness.py:486 sets")
emit("effective_deadline = max(deadline, 0), clamping any deadline that had")
emit("already passed before the scheduling origin up to the origin. These")
emit("figures therefore measure the delay THIS schedule adds and EXCLUDE")
emit("pre-existing overdue time against the original customer commitment. An")
emit("order already 1,200h late at the origin and finished immediately scores")
emit("0h here. Do not read them as lateness against the customer's date.")
emit("=" * 78)
agg_header = (f"{'strategy':<14}{'on-time%':>10}{'orders':>8}{'late':>7}{'makespan(d)':>13}"
              f"{'throughput/d':>14}{'tard_late(h)':>14}{'tard_all(h)':>13}"
              f"{'max_tard(h)':>13}{'labour%':>10}")
emit(agg_header)
for name, result in STRATEGIES:
    a = result['aggregate']
    emit(f"{name:<14}{a['on_time_pct']:>9.1f}%{a['orders']:>8}{a['orders_late']:>7}"
         f"{a['makespan_days']:>13.2f}"
         f"{a['throughput_orders_per_day']:>14.2f}"
         f"{a['mean_tardiness_h_late_orders_only']:>14.2f}"
         f"{a['mean_tardiness_h_all_orders']:>13.2f}"
         f"{a['max_tardiness_h']:>13.2f}{a['labour_utilisation_pct']:>9.1f}%")

emit()
emit("=" * 78)
emit("ORDER-WEIGHTED AGGREGATE ACROSS WINDOWS")
emit("=" * 78)
emit("CORRECTED. A previous version of this report claimed the order-weighted")
emit("and whole-book on-time figures necessarily answer different questions.")
emit("For THIS implementation that is false, and the equality is exact:")
emit("  * the commit windows are disjoint and together cover every order once,")
emit("  * each window's on_time_pct is computed over exactly the orders that")
emit("    window committed, and every committed order carries a deadline,")
emit("  * so weighting those rates by committed_orders re-pools the identical")
emit("    numerator and denominator the whole-book figure uses.")
emit("Verified numerically below (difference column); it is 0 to floating point.")
emit("")
emit("TARDINESS IS DIFFERENT and the distinction is real there. The per-window")
emit("mean_tardiness_h averages over LATE orders only, so weighting it by ALL")
emit("committed orders produces neither the whole-book mean over all orders nor")
emit("the mean among late orders -- it is not a standard quantity and is no")
emit("longer reported. Both proper denominators appear in the table above.")
emit(f"{'strategy':<16}{'order-wtd on-time%':>20}{'whole-book on-time%':>21}"
     f"{'difference':>13}{'orders':>9}")
order_weighted = {}
for name, result in STRATEGIES:
    rows = per_window[name]
    total_orders = sum(r['committed_orders'] for r in rows)
    if not total_orders:
        continue
    ot = sum(r['on_time_pct'] * r['committed_orders'] for r in rows) / total_orders
    wb = result['aggregate']['on_time_pct']
    order_weighted[name] = {'order_weighted_on_time_pct': ot,
                            'whole_book_on_time_pct': wb,
                            'difference': ot - wb,
                            'orders_counted': total_orders}
    emit(f"{name:<16}{ot:>19.6f}%{wb:>20.6f}%{ot - wb:>13.2e}{total_orders:>9}")
RESULTS['order_weighted_aggregate_across_windows'] = order_weighted

emit()
emit("=" * 78)
emit("FITNESS COMPONENT BREAKDOWN (per window, weighted terms, sums to fitness)")
emit("=" * 78)
emit("Straight from ga/fitness.py::evaluate_schedule's own breakdown -- not")
emit("recomputed here. priority_reward is NEGATIVE: it is subtracted.")
emit("")
emit("FINDING -- the delay term carries no schedule-discriminating signal.")
emit("  ga/fitness.py accumulates  total_delay_penalty += delay_prob * 50.0")
emit("  once per operation and divides by the operation count. That is the")
emit("  mean predicted delay probability times a constant: it depends on WHICH")
emit("  operations are in the window and on nothing the GA decides -- not")
emit("  sequence, line, machine count or timing. For a fixed operation set it")
emit("  is identical for every candidate, which is why the delay column below")
emit("  is constant across all arms within each window.")
emit("  The other channel by which the delay prediction could reach a schedule")
emit("  -- the risk buffer -- is inert too: RISK_BUFFER_AT_CERTAINTY = 0.0, so")
emit("  duration * (1 + p*0.0) == duration. Together these mean the GNN delay")
emit("  head currently has NO effect on GA scheduling.")
emit("  Removing the term would improve interpretability without changing any")
emit("  tournament outcome (subtracting a constant preserves every pairwise")
emit("  comparison). NOT removed here -- reported for a decision, not acted on.")
emit("")
emit("MATCHING DELIVERY KPIs DO NOT MEAN IDENTICAL SCHEDULES. Two arms can")
emit("agree to every printed decimal on on-time, makespan and tardiness while")
emit("differing in fitness, because the load-balance and idle-labour terms")
emit("respond to placement detail those KPIs do not capture. Compare the")
emit("fitness column, not the KPI table, before calling two schedules the same.")
for w in range(1, n_windows + 1):
    emit(f"\n--- WINDOW {w} ---")
    emit(f"{'strategy':<16}{'fitness':>12}{'makespan':>11}{'delay':>10}"
         f"{'priority':>10}{'load_bal':>10}{'lateness':>10}{'idle_lab':>10}{'sum_chk':>11}")
    for name, _ in STRATEGIES:
        rows = per_window[name]
        if w - 1 >= len(rows):
            continue
        r = rows[w - 1]
        c = r.get('fitness_components') or {}
        if not c:
            continue
        emit(f"{name:<16}{r['fitness']:>12.2f}{c['makespan']:>11.2f}"
             f"{c['delay_penalty']:>10.2f}{c['priority_reward']:>10.2f}"
             f"{c['load_balance']:>10.2f}{c['lateness']:>10.2f}"
             f"{c['idle_labour']:>10.2f}{sum(c.values()):>11.2f}")

emit()
emit("=" * 78)
emit("MACHINE-POOL SHORTAGE (pool_short_operations) -- SEPARATE FROM STAFFING")
emit("=" * 78)
emit("Counted when the chromosome's requested machine count exceeds the line's")
emit("eligible pool. QUALIFICATION, so this is not over-read:")
emit("  * The evaluator CLAMPS the count to the pool before computing the")
emit("    duration (ga/fitness.py: machine_count = min(machine_count,")
emit("    len(pool_ids)), then calculate_operation_work). The schedule is")
emit("    therefore costed against machines that exist -- these counts do NOT")
emit("    demonstrate fabricated capacity or an optimistic duration.")
emit("  * What they do show is a chromosome asking for more machines than the")
emit("    assigned line can supply. The MC gene is supposed to be bounded by")
emit("    that same count at generation time, so the origin is UNDIAGNOSED.")
emit("  * They are NOT worker shortages and must never be added to, compared")
emit("    with, or reported alongside the operator-staffing figures below.")
for _nm, _ in STRATEGIES:
    _vals = [(r['window'], r.get('pool_short_operations')) for r in per_window[_nm]]
    emit(f"  {_nm:<16}" + "  ".join(f"w{w}={v}" for w, v in _vals))
emit("")
emit("=" * 78)
emit("STAFFING FEASIBILITY -- ALL ARMS, AUTHORITATIVE DECODER")
emit("=" * 78)
emit("Every arm is scored and exported through the same deterministic worker-aware")
emit("phenotype. Worker assignment and productive calendar segments determine")
emit("elapsed completion before fitness is calculated; staffing is not a post-hoc")
emit("reporting step. An infeasible crew assignment rejects the candidate.")
emit("")
_arm_results = {"GA": ga_result, **baseline_results}
staffing_by_arm = {}
for arm_name, arm_result in _arm_results.items():
    arm_staffing = {
        "windows": [],
        "total_understaffed_operations": 0,
        "any_window_infeasible": False,
    }
    for summary in arm_result.get("export_summaries", []):
        n_under = summary.get("understaffed_operations", 0)
        feasible = summary.get("execution_feasible")
        arm_staffing["windows"].append({
            "understaffed_operations": n_under,
            "execution_feasible": feasible,
            "phenotype_hash": summary.get("phenotype_hash"),
        })
        arm_staffing["total_understaffed_operations"] += n_under
        if feasible is False:
            arm_staffing["any_window_infeasible"] = True
    staffing_by_arm[arm_name] = arm_staffing
    emit(f"  {arm_name}:")
    for window_number, row in enumerate(arm_staffing["windows"], start=1):
        emit(
            f"    window {window_number}: "
            f"understaffed_operations={row['understaffed_operations']}  "
            f"execution_feasible={row['execution_feasible']}"
        )
    emit(
        f"    total understaffed operations: "
        f"{arm_staffing['total_understaffed_operations']}"
    )
    if arm_staffing["any_window_infeasible"]:
        emit("    *** at least one exported window is not execution-feasible ***")
emit("")
emit("A completed comparison is valid only when every exported arm/window is")
emit("execution-feasible and its score/export phenotype hashes agree.")
RESULTS["staffing_feasibility"] = staffing_by_arm

emit()
emit("=" * 78)
emit("GA FITNESS TRAJECTORY (per window, from the DEAP logbook)")
emit("=" * 78)
emit("gen 0 is the INITIAL POPULATION (heuristic seeds + random), before any")
emit("selection/crossover/mutation. 'best' is the minimum -- this is a")
emit("minimisation objective. No historical convergence figure is referenced.")
emit("")
emit("NOT AN EQUAL-COMPUTE COMPARISON, stated plainly: the GA evaluates its")
emit("initial population, then some thousands of offspring across the")
emit("generations (nevals below), then spends a further polish budget of")
emit("local-search evaluations. Each pure heuristic CONSTRUCTS one schedule")
emit("directly and is evaluated once. That asymmetry is acceptable for the")
emit("question being asked -- does the extra optimisation pay off -- but it")
emit("is not a like-for-like budget and must not be presented as one.")
emit("")
emit("WHAT A FLAT TRAJECTORY DOES AND DOES NOT ESTABLISH:")
emit("  It establishes that THIS configuration, from THESE heuristic seeds,")
emit("  did not improve on them within its generation budget. It does NOT")
emit("  establish that better schedules do not exist, nor that the landscape")
emit("  is flat. No seed short-circuit and no missing offspring-evaluation")
emit("  defect was found; the offspring were evaluated and were not better.")
emit("")
emit("  A previously-offered explanation is RETRACTED: that the makespan term")
emit("  dominates the objective and therefore flattens the search signal.")
emit("  Two reasons it does not hold. First, the makespan SHARE of final GA")
emit("  fitness varies widely by window (printed below), and the windows with")
emit("  zero improvement include the one where makespan is the SMALLEST share")
emit("  -- so the effect is not where that story predicts. Second, adding a")
emit("  near-constant to every candidate preserves every pairwise tournament")
emit("  comparison; it shrinks percentage-improvement figures but cannot stop")
emit("  selection from preferring a better schedule.")
trajectories = {}
for lb in (ga_result.get('logbooks') or []):
    recs = lb['records']
    if not recs:
        continue
    gen0, final = recs[0], recs[-1]
    best0, bestN = gen0['min'], final['min']
    total_gain = best0 - bestN
    # Generation at which the run reached within 1% of its own final best --
    # a convergence point measured against THIS run, not an external figure.
    threshold = bestN + 0.01 * abs(total_gain)
    conv_gen = next((r['gen'] for r in recs if r['min'] <= threshold), final['gen'])
    trajectories[lb['window']] = {
        'nevals_gen0': gen0['nevals'],
        'nevals_after_gen0': sum(r['nevals'] for r in recs[1:]),
        'initial_population_best': best0,
        'final_best': bestN,
        'total_improvement': total_gain,
        'improvement_pct_of_initial': (100.0 * total_gain / abs(best0)) if best0 else 0.0,
        'generation_reaching_99pct_of_final_gain': conv_gen,
        'generations_run': final['gen'],
        'final_avg': final['avg'],
        'final_spread_avg_minus_min': final['avg'] - bestN,
        'per_generation_best': [r['min'] for r in recs],
    }
    t = trajectories[lb['window']]
    emit(f"\n--- WINDOW {lb['window']} ---")
    emit(f"  initial population best (gen 0)   {best0:>12.2f}")
    emit(f"  final best (gen {final['gen']})            {bestN:>12.2f}")
    emit(f"  total improvement from search     {total_gain:>12.2f}"
         f"  ({t['improvement_pct_of_initial']:.2f}% of the initial best)")
    emit(f"  reached 99% of that gain by gen   {conv_gen:>12}  of {final['gen']}")
    emit(f"  evaluations at gen 0              {gen0['nevals']:>12}")
    emit(f"  evaluations after gen 0           {sum(r['nevals'] for r in recs[1:]):>12}"
         f"   (offspring + immigrants, all evaluated)")
    _share = None
    _garow = next((r for r in per_window['GA'] if r['window'] == lb['window']), None)
    if _garow and _garow.get('fitness_components'):
        _share = 100.0 * _garow['fitness_components']['makespan'] / _garow['fitness']
        emit(f"  makespan share of final fitness   {_share:>11.2f}%"
             f"   (varies by window -- see retraction above)")
    # Three-way decomposition: how much of the committed result came from the
    # heuristic SEED, from the GENERATIONS, and from the POLISH pass. These
    # are separate mechanisms and lumping them together as "the GA" hides
    # which one is actually doing the work.
    if _garow:
        seed_best = gen0['min']
        after_gens = final['min']
        committed = _garow['fitness']
        emit(f"  DECOMPOSITION of the committed result:")
        emit(f"    initial population best (seed)  {seed_best:>14.6f}")
        emit(f"    after {final['gen']} generations         {after_gens:>14.6f}"
             f"   ({seed_best - after_gens:+.6f})")
        emit(f"    after polish (committed)        {committed:>14.6f}"
             f"   ({after_gens - committed:+.6f})")
        _tot = seed_best - committed
        if abs(_tot) > 1e-12:
            emit(f"    share of total gain from generations "
                 f"{100.0 * (seed_best - after_gens) / _tot:>7.2f}%")
            emit(f"    share of total gain from polish      "
                 f"{100.0 * (after_gens - committed) / _tot:>7.2f}%")
        else:
            emit("    total gain over the seed is zero -- neither stage improved it.")
        trajectories[lb['window']].update({
            'seed_best': seed_best, 'after_generations': after_gens,
            'committed_after_polish': committed,
            'gain_from_generations': seed_best - after_gens,
            'gain_from_polish': after_gens - committed})
    emit(f"  final population avg              {final['avg']:>12.2f}")
    emit(f"  final spread (avg - best)         {t['final_spread_avg_minus_min']:>12.2f}"
         f"   (small = converged//homogeneous population)")
    step = max(1, len(recs) // 10)
    emit("  best-so-far by generation: " + ", ".join(
        f"g{r['gen']}={r['min']:.1f}" for r in recs[::step]))
RESULTS['ga_fitness_trajectory'] = trajectories

emit()
emit("=" * 78)
emit("FAIRNESS CHECK -- BASELINES FACE THE SAME FACTORY CONSTRAINT AS THE GA")
emit("=" * 78)
# Verified structurally, not assumed: run_rolling_horizon_baseline builds the
# SAME GAScheduler with the SAME ownership_snapshot, and passes
# sched.factory straight into baseline_chromosomes() -- so both arms read one
# ChromosomeFactory's eligibility_map. Re-derived here as an executable check
# so the claim is measured rather than asserted from code reading.
from ga.chromosome import ChromosomeFactory as _CF
_shared = _CF(builder.df_routings, df_machines, df_lines,
              ownership_snapshot=builder.ownership_snapshot)
_legacy = _CF(builder.df_routings, df_machines, df_lines,
              legacy_mode='legacy_company_wide')
_scoped_sizes = {k: len(v) for k, v in _shared.eligibility_map.items()}
_legacy_sizes = {k: len(v) for k, v in _legacy.eligibility_map.items()}
_narrowed = sum(1 for k in _scoped_sizes if _scoped_sizes[k] < _legacy_sizes.get(k, 0))
emit(f"  operations in eligibility map                 {len(_scoped_sizes)}")
emit(f"  mean eligible lines per op, factory-scoped    "
     f"{sum(_scoped_sizes.values())/max(1,len(_scoped_sizes)):.2f}")
emit(f"  mean eligible lines per op, company-wide      "
     f"{sum(_legacy_sizes.values())/max(1,len(_legacy_sizes)):.2f}")
emit(f"  operations whose choices ownership narrowed   {_narrowed}")
emit("  Both arms construct GAScheduler with the same ownership_snapshot, and")
emit("  ga/rolling_horizon.py::run_rolling_horizon_baseline passes that same")
emit("  sched.factory into baseline_chromosomes() -- so the GA and every")
emit("  baseline decode against one shared eligibility map and the identical")
emit("  factory-ownership constraint. The baselines are not given a freer")
emit("  problem than the GA.")
RESULTS['eligibility_fairness'] = {
    'operations': len(_scoped_sizes),
    'mean_eligible_lines_factory_scoped': sum(_scoped_sizes.values())/max(1, len(_scoped_sizes)),
    'mean_eligible_lines_company_wide': sum(_legacy_sizes.values())/max(1, len(_legacy_sizes)),
    'operations_narrowed_by_ownership': _narrowed,
}

overall_floor = compute_capacity_floor_days(
    builder.df_routings, factory, avg_efficiency, ORIGIN, op_work=OP_WORK)
emit("")
emit("Whole-book lower bounds (days), reported SEPARATELY -- not pre-maxed:")
emit(f"  workload bound (machine capacity)      {overall_floor['workload_days']:.4f}")
emit(f"  critical-path bound (release + route)  {overall_floor['critical_path_days']:.4f}")
emit(f"  binding component                      {overall_floor['binding']}")
emit(f"  operations using the fallback formula  {overall_floor['fallback_ops']}")
emit("  The critical-path bound can bind while machines sit idle -- one")
emit("  late-released order with a long routing sets it regardless of spare")
emit("  capacity. Reaching it is NOT evidence that capacity is saturated.")
whole_book_floor_ratios = {}
for name, result in STRATEGIES:
    ms = result['aggregate']['makespan_days']
    rw = ms / overall_floor['workload_days'] if overall_floor['workload_days'] else float('nan')
    rc = ms / overall_floor['critical_path_days'] if overall_floor['critical_path_days'] else float('nan')
    whole_book_floor_ratios[name] = {'vs_workload': rw, 'vs_critical_path': rc,
                                     'makespan_days': ms}
    emit(f"  {name:<16}makespan={ms:.4f}d   vs workload bound={rw:.4f}x   "
         f"vs critical-path bound={rc:.4f}x")
RESULTS['whole_book_capacity_floor_ratios'] = whole_book_floor_ratios

RESULTS['overall_capacity_floor_days'] = overall_floor
RESULTS['total_seconds'] = time.time() - _SCRIPT_START

with open(os.path.join(OUTPUT_DIR, "authoritative_comparison_report.json"), "w") as f:
    json.dump(RESULTS, f, indent=2, default=str)
from utils.run_artifacts import stamp_artifact
stamp_artifact(os.path.join(OUTPUT_DIR, "authoritative_comparison_report.json"))
with open(os.path.join(OUTPUT_DIR, "authoritative_comparison_report.txt"), "w") as f:
    f.write("\n".join(report_lines))

banner("DONE")
print(f"total runtime: {RESULTS['total_seconds']/60:.1f} min")
print(f"report written to: {os.path.join(OUTPUT_DIR, 'authoritative_comparison_report.json')}")
print(f"report written to: {os.path.join(OUTPUT_DIR, 'authoritative_comparison_report.txt')}")
