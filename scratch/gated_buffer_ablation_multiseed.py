"""
Multi-seed re-run of scratch/gated_buffer_ablation.py, on the CURRENT,
fully-fixed pipeline.

WHY THIS EXISTS, NOT JUST A RE-RUN OF THE ORIGINAL. The original 3-arm
ablation (scratch/gated_buffer_ablation.py, log at
scratch_gated_buffer_ablation.log, GA_AUDIT.md Section 6) ran 2026-08-23,
seed=42 only, and predates BOTH:
  - the release-date enforcement fix (PROJECT_STATUS.txt Section 1.3 /
    GA_AUDIT.md Section 8, 2026-08-24) -- that run's makespan was ~32 days;
    the corrected pipeline's whole-book makespan is ~324 days, a completely
    different operating regime, not just a reproducibility risk.
  - the fetch_active_orders_and_routings ORDER BY fix (database/queries.py,
    2026-08-27) -- ga/chromosome.py builds its gene list directly from
    df_routings.iterrows() with no explicit re-sort by
    operation_sequence_number, so row order feeds straight into chromosome
    construction. Pre-fix, that order was undefined SQL semantics.
GA_AUDIT.md Section 6 already flagged this as "a single-seed ablation ...
should be treated as provisional until re-confirmed under a second seed" --
this run closes that caveat AND regenerates the numbers on the corrected
pipeline in one pass, since re-running under the old regime would produce
numbers that are reproducible but no longer representative of anything real.

SAME PROBLEM INSTANCE ACROSS EVERYTHING BUT THE SEED: one ORIGIN, one graph,
one set of real GNN delay predictions (current checkpoint, not retrained),
one capacity figure -- built ONCE, shared across all 3 seeds x 3 arms (9
runs total). Only ga.rolling_horizon's internal GA seed varies.

Arms (identical definitions to the original script):
  A_disabled   -- buffer_mins = duration_mins always
  B_broad_0.5  -- buffer_mins = duration * (1 + p*0.5) for EVERYONE
  C_gated_0.5  -- buffer_mins = duration * (1 + p*0.5) IF p >= 0.14, else unchanged

Writes incrementally to outputs/gated_buffer_ablation_multiseed.json after
EVERY arm x seed completes (this is a multi-hour run; a partial file must
survive an interruption) and a final range-summary printed/written at the
end covering on-time%, makespan, throughput, mean tardiness, labour%, and
margin per policy across all 3 seeds.
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
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

GATE_THRESHOLD = 0.14

COMMIT_SIZE = 300
LOOKAHEAD_SIZE = None
POP_SIZE = 50
GENERATIONS = 50
POLISH_EVALS = 400
SEEDS = [42, 101, 202]

OUT_PATH = os.path.join(OUTPUT_DIR, "gated_buffer_ablation_multiseed.json")


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


def _load_results():
    if os.path.exists(OUT_PATH):
        with open(OUT_PATH, encoding='utf-8') as f:
            return json.load(f)
    return {'meta': {}, 'runs': {}}


def _save_results(results):
    tmp = OUT_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, default=str)
    os.replace(tmp, OUT_PATH)


print("=" * 70)
print("SETUP (shared across all seeds x arms -- same problem instance)")
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
print(f"fixed origin timestamp (shared by ALL {len(SEEDS)} seeds x {len(ARMS)} arms): {ORIGIN.isoformat()}")
print(f"concurrent operator capacity: {capacity}")

eng = AIDecisionEngine()
op_index = eng._build_operator_index(
    fetch_assignable_operators(), df_lines, fetch_line_factory_map())

print(f"commit={COMMIT_SIZE}  lookahead={LOOKAHEAD_SIZE or COMMIT_SIZE}  "
      f"pop={POP_SIZE}  gen={GENERATIONS}  polish={POLISH_EVALS}  seeds={SEEDS}")

results = _load_results()
results['meta'] = {
    'origin': ORIGIN.isoformat(), 'capacity': capacity, 'commit_size': COMMIT_SIZE,
    'pop_size': POP_SIZE, 'generations': GENERATIONS, 'polish_evals': POLISH_EVALS,
    'seeds': SEEDS, 'gate_threshold': GATE_THRESHOLD,
    'why': 'post release-date-fix, post ORDER-BY-fix re-run; supersedes '
           'scratch/gated_buffer_ablation.py (2026-08-23, single seed, pre-both-fixes)',
}
_save_results(results)

total_runs = len(SEEDS) * len(ARMS)
done_count = 0
t_start = time.time()

for seed in SEEDS:
    for arm_name, buffer_fn in ARMS.items():
        run_key = f"seed{seed}_{arm_name}"
        if run_key in results['runs'] and 'windows' in results['runs'][run_key]:
            print(f"[skip] {run_key} already in {OUT_PATH} (with per-window data)")
            done_count += 1
            continue

        print()
        print("=" * 70)
        print(f"RUN {done_count + 1}/{total_runs}: seed={seed}  arm={arm_name}")
        print("=" * 70)
        patch_buffer(buffer_fn)

        t0 = time.time()
        rolling_result = run_rolling_horizon(
            builder.df_routings, builder.df_machines, df_lines, preds_all, capacity, ORIGIN,
            commit_size=COMMIT_SIZE, lookahead_size=LOOKAHEAD_SIZE,
            pop_size=POP_SIZE, generations=GENERATIONS, polish_evals=POLISH_EVALS, seed=seed,
            export=False, decision_engine=eng, operator_index=op_index,
            log_fn=print,
        )
        elapsed = time.time() - t0
        agg = rolling_result['aggregate']

        # Per-window breakdown, same derivation as run_authoritative_comparison.py:
        # incr_days = this window's OWN added span (end_day is cumulative, carried
        # machine state), labour_pct uses the SAME capacity-aware denominator as
        # the whole-book figure, scoped to just this window's span and labour.
        # Dense/slack labels reuse the classification already established against
        # this same live DB/window structure (windows 1-2 dense, 3-4 slack) rather
        # than re-deriving a fresh median split per arm/seed, which could jitter.
        DENSE_SLACK = {1: 'dense', 2: 'dense', 3: 'slack', 4: 'slack'}
        raw_windows = rolling_result.get('windows') or []
        per_window = []
        prev_end_day = 0.0
        for entry in raw_windows:
            end_day = entry['end_day']
            incr_days = max(end_day - prev_end_day, 1e-9)
            labour_pct = 100.0 * entry['labor_minutes'] / (capacity * incr_days * 1440.0)
            per_window.append({
                'window': entry['window'],
                'classification': DENSE_SLACK.get(entry['window'], 'n/a'),
                'on_time_pct': entry['on_time_pct'],
                'cum_makespan_days': end_day,
                'incr_makespan_days': incr_days,
                'committed_orders': entry['committed_orders'],
                'mean_tardiness_h': entry['mean_tardiness_h'],
                'labour_pct': labour_pct,
                'margin_pct': entry.get('margin_pct'),
                'best_baseline': entry.get('best_baseline'),
            })
            prev_end_day = end_day

        results['runs'][run_key] = {
            'seed': seed, 'arm': arm_name, 'elapsed_s': elapsed, 'windows': per_window, **agg,
        }
        _save_results(results)
        done_count += 1

        total_elapsed = time.time() - t_start
        avg_per_run = total_elapsed / done_count
        remaining = (total_runs - done_count) * avg_per_run
        print(f"[{run_key}] done in {elapsed:.0f}s. "
              f"{done_count}/{total_runs} complete, ~{remaining/60:.0f} min remaining (rough estimate).")

print()
print("=" * 70)
print("ALL RUNS COMPLETE -- RANGE SUMMARY ACROSS SEEDS, PER POLICY")
print("=" * 70)

METRICS = [
    ('on_time_pct', '%', '.1f'), ('makespan_days', 'd', '.1f'),
    ('throughput_orders_per_day', '/d', '.2f'), ('mean_tardiness_h', 'h', '.0f'),
    ('labour_utilisation_pct', '%', '.2f'), ('mean_margin_over_baseline_pct', '%', '+.1f'),
]

summary = {}
for arm_name in ARMS:
    arm_runs = [results['runs'][f"seed{s}_{arm_name}"] for s in SEEDS
                if f"seed{s}_{arm_name}" in results['runs']]
    summary[arm_name] = {}
    print(f"\n{arm_name}  (n={len(arm_runs)} seeds)")
    for metric, unit, fmt in METRICS:
        vals = [r.get(metric) for r in arm_runs if r.get(metric) is not None]
        if not vals:
            continue
        lo, hi = min(vals), max(vals)
        spread = hi - lo
        summary[arm_name][metric] = {'min': lo, 'max': hi, 'values': vals}
        print(f"  {metric:<32} range [{lo:{fmt}}{unit}, {hi:{fmt}}{unit}]  "
              f"spread={spread:{fmt}}{unit}  values={[f'{v:{fmt}}' for v in vals]}")

results['range_summary'] = summary
_save_results(results)

print()
print("=" * 70)
print("PER-WINDOW / DENSE-SLACK BREAKDOWN, PER POLICY, ACROSS SEEDS")
print("=" * 70)

WINDOW_METRICS = [
    ('on_time_pct', '%', '.1f'), ('incr_makespan_days', 'd', '.1f'),
    ('labour_pct', '%', '.2f'), ('margin_pct', '%', '+.1f'),
]

window_summary = {}
for arm_name in ARMS:
    arm_runs = [results['runs'][f"seed{s}_{arm_name}"] for s in SEEDS
                if f"seed{s}_{arm_name}" in results['runs']]
    print(f"\n{arm_name}")
    for w in (1, 2, 3, 4):
        cls = {1: 'dense', 2: 'dense', 3: 'slack', 4: 'slack'}.get(w, 'n/a')
        entries = [next((pw for pw in r['windows'] if pw['window'] == w), None) for r in arm_runs]
        entries = [e for e in entries if e is not None]
        if not entries:
            continue
        print(f"  window {w} ({cls}):")
        for metric, unit, fmt in WINDOW_METRICS:
            vals = [e.get(metric) for e in entries if e.get(metric) is not None]
            if not vals:
                continue
            lo, hi = min(vals), max(vals)
            window_summary.setdefault(arm_name, {}).setdefault(w, {})[metric] = {
                'min': lo, 'max': hi, 'values': vals,
            }
            print(f"    {metric:<20} range [{lo:{fmt}}{unit}, {hi:{fmt}}{unit}]  "
                  f"values={[f'{v:{fmt}}' for v in vals]}")

    # Dense (w1+2) vs slack (w3+4) combined, time-weighted by incr_makespan_days
    # -- same weighting the whole-book figure itself reduces to (capacity cancels
    # out of the ratio), so this reconciles exactly with the aggregate reported
    # above when summed across all 4 windows.
    for label, wnums in (('dense (w1-2)', (1, 2)), ('slack (w3-4)', (3, 4))):
        per_seed_pct = []
        for r in arm_runs:
            wins = [pw for pw in r['windows'] if pw['window'] in wnums]
            total_days = sum(w['incr_makespan_days'] for w in wins)
            if total_days <= 0:
                continue
            weighted_pct = sum(w['labour_pct'] * w['incr_makespan_days'] for w in wins) / total_days
            per_seed_pct.append(weighted_pct)
        if per_seed_pct:
            print(f"  {label} labour util., time-weighted per seed: "
                  f"range [{min(per_seed_pct):.2f}%, {max(per_seed_pct):.2f}%]  "
                  f"values={[f'{v:.2f}' for v in per_seed_pct]}")

results['window_range_summary'] = window_summary
_save_results(results)
print(f"\nFull results: {OUT_PATH}")
print("DONE")
