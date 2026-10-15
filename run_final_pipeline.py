"""
Final end-to-end pipeline run -- ONE process, ONE origin timestamp, ONE seed.

Sequence: fresh seed -> graph -> train GNN -> evaluate GNN -> GNN inference ->
rolling-horizon scheduling across the entire order book with local-search
polishing -> final schedule outputs -> a machine-breakdown simulation through
the fuzzy impact gate.

WHY A SINGLE SCRIPT. Stitching this together from separate invocations lets
small confounds in: the earlier SERVICE_LEVEL_WEIGHT sweep had to be run in
two halves after being killed, and the two halves used origin timestamps about
an hour apart, shifting every deadline by ~0.14%. Negligible there, but this
project has been bitten repeatedly by numbers that turned out to be measuring
something other than what they claimed. ORIGIN is fixed once, below, and every
window, every deadline and every carried machine state is expressed against
it, so the run is internally consistent by construction rather than by
remembering to be careful.

Back up the database before running this. It reseeds, which truncates all 26
tables, and TRUNCATE auto-commits.

    python run_final_pipeline.py
"""

import json
import os
import sys
import time
from datetime import datetime

import pandas as pd
import torch

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from utils.config import (
    GNN_HIDDEN_CHANNELS, OUTPUT_DIR, PLANNING_HORIZON_ORDERS,
    GA_POPULATION_SIZE, GA_GENERATIONS,
)
from utils.logger import get_logger

logger = get_logger("final_run", "agent.log")

SEED = 42
# BUG FIX (PIPELINE_METHODOLOGY_TRACE.md Stage 10 / FULL_CODEBASE_AUDIT.md):
# was a locally hardcoded POP, GENS = 50, 50, disconnected from
# utils/config.py's GA_POPULATION_SIZE/GA_GENERATIONS (which also disagreed
# with each other -- GA_GENERATIONS defaulted to 100, this script actually
# ran 50). Now imports the single source of truth instead of a second,
# independent copy of the same two numbers.
POP, GENS = GA_POPULATION_SIZE, GA_GENERATIONS
POLISH_EVALS = 400
BREAKDOWN_ASSET = 121

# Overlapping lookahead (receding horizon): the GA sees LOOKAHEAD_ORDERS while
# deciding but only PLANNING_HORIZON_ORDERS (utils/config.py) get committed
# and carried forward each round. None means lookahead == commit, i.e. the
# original non-overlapping behaviour -- see ga/rolling_horizon.py. Set to a
# concrete multiple of PLANNING_HORIZON_ORDERS once the lookahead sweep
# (item 1) has picked a default.
LOOKAHEAD_ORDERS = None

RESULTS = {'started_at': datetime.now().isoformat(), 'seed': SEED}
# Large intermediate objects (the rolling-horizon result) that later stages
# need but that don't belong in the JSON-serialisable RESULTS dict directly.
PIPELINE_STATE = {}


def banner(text):
    print("\n" + "=" * 78)
    print(text)
    print("=" * 78, flush=True)


def stage(name, fn):
    """Runs a stage, timing it, and records failure without losing prior work."""
    banner(name)
    t0 = time.time()
    try:
        out = fn()
        RESULTS.setdefault('stages', {})[name] = {'ok': True, 'seconds': time.time() - t0}
        return out
    except Exception as exc:
        RESULTS.setdefault('stages', {})[name] = {
            'ok': False, 'seconds': time.time() - t0, 'error': repr(exc)}
        _save()
        raise


def _save():
    with open(os.path.join(OUTPUT_DIR, "final_run_report.json"), "w") as f:
        json.dump(RESULTS, f, indent=2, default=str)
    from utils.run_artifacts import stamp_artifact
    stamp_artifact(os.path.join(OUTPUT_DIR, "final_run_report.json"))


def _metric_line(name, value):
    """Format scalar and structured evaluation outputs without assuming float."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        rendered = f"{value:.4f}"
    elif value is None:
        rendered = "null"
    else:
        rendered = json.dumps(value, sort_keys=True, default=str)
    return f"  {name:<26} {rendered}"


# ---------------------------------------------------------------- 1. reseed
def do_seed():
    from data_generation.seed_database_fixed import execute_complete_seed
    execute_complete_seed()
    from database.db import get_engine
    counts = {}
    for t in ('production_orders', 'job_shop_schedules', 'employees',
              'employee_batches', 'machinery_assets'):
        counts[t] = int(pd.read_sql(f"SELECT COUNT(*) AS n FROM {t}", get_engine()).iloc[0]['n'])
    print("row counts:", counts)
    RESULTS['seed_row_counts'] = counts


# ------------------------------------------------------------- 2. graph/GNN
def do_train():
    from models.train import train_gnn
    train_gnn()


def do_evaluate():
    from models.evaluate import evaluate_gnn
    metrics = evaluate_gnn()
    RESULTS['gnn_metrics'] = metrics
    PIPELINE_STATE['gnn_metrics'] = metrics
    for k, v in (metrics or {}).items():
        print(_metric_line(k, v))
    return metrics


# -------------------------------------------------------------- 3. schedule
def do_schedule():
    """
    Built on ga.rolling_horizon.run_rolling_horizon() -- the same module the
    lookahead sweep uses, so the production pipeline and the experiment that
    chose its settings can never quietly diverge into two implementations of
    the same loop.
    """
    from database.db import get_engine
    from database.queries import (
        fetch_concurrent_operator_capacity, fetch_assignable_operators,
        fetch_line_factory_map,
    )
    from graph.graph_builder import FactoryGraphBuilder
    from models.gnn import MultiTaskFactoryGNN, prior_corrected_delay_probabilities
    from agent.decision_engine import AIDecisionEngine
    from ga.rolling_horizon import run_rolling_horizon

    builder = FactoryGraphBuilder()
    from graph.feature_schema import load_preprocessing
    preprocessing = load_preprocessing(os.path.join(OUTPUT_DIR, "checkpoints", "gnn_multitask_latest.pth"))
    graph, _ = builder.build_graph(preprocessing=preprocessing)
    df_lines = pd.read_sql(
        "SELECT line_id, department_id, line_number, target_efficiency_percentage, is_active "
        "FROM Production_Lines WHERE is_active = 1", get_engine())

    model = MultiTaskFactoryGNN(metadata=graph.metadata(), hidden_channels=GNN_HIDDEN_CHANNELS, operation_in_dim=preprocessing['operation_schema']['count'])
    model.load_state_dict(torch.load(os.path.join(OUTPUT_DIR, "checkpoints",
                                                  "gnn_multitask_latest.pth")))
    model.eval()
    with torch.no_grad():
        out = model(graph.x_dict, graph.edge_index_dict, graph['operation'].type_idx)
    delay = prior_corrected_delay_probabilities(
        out['delay'], preprocessing['delay_pos_weight']
    ).squeeze(-1)
    preds_all = {k: {'delay': delay[i].item(), 'priority': out['priority'][i].item()}
                 for i, k in enumerate(builder.op_map.keys())}
    dvals = [p['delay'] for p in preds_all.values()]
    RESULTS['gnn_delay_range'] = [min(dvals), max(dvals)]
    print(f"  GNN delay probability range: {min(dvals):.4f} - {max(dvals):.4f}")

    capacity = fetch_concurrent_operator_capacity()

    # THE fixed origin. Every deadline, every carried machine free-time and
    # every window in this run is measured from here.
    ORIGIN = datetime.now()
    RESULTS['origin'] = ORIGIN.isoformat()
    print(f"  fixed origin timestamp: {ORIGIN.isoformat()}")

    eng = AIDecisionEngine()
    op_index = eng._build_operator_index(
        fetch_assignable_operators(), df_lines, fetch_line_factory_map())

    commit_size = PLANNING_HORIZON_ORDERS or builder.df_routings['production_order_id'].nunique()
    print(f"  commit={commit_size}  lookahead={LOOKAHEAD_ORDERS or commit_size}")

    rolling_result = run_rolling_horizon(
        builder.df_routings, builder.df_machines, df_lines, preds_all, capacity, ORIGIN,
        commit_size=commit_size, lookahead_size=LOOKAHEAD_ORDERS,
        pop_size=POP, generations=GENS, polish_evals=POLISH_EVALS, seed=SEED,
        export=True, decision_engine=eng, operator_index=op_index,
        log_fn=print, ownership_snapshot=builder.ownership_snapshot,
    )

    PIPELINE_STATE['rolling_result'] = rolling_result
    RESULTS['schedule'] = rolling_result['aggregate']
    if rolling_result['export_summaries']:
        RESULTS['exported_window_summary'] = rolling_result['export_summaries'][0]
    _save()


# ---------------------------------------------------------- 3b. eval report
def do_evaluation_report():
    """
    The standing report: PR/ROC curves for the delay classifier, GA
    convergence, local-search gain per window, labour by shift and by
    sourcing tier. Generated every full pipeline run from here on, not
    produced by hand -- see reporting/evaluation_report.py.
    """
    from reporting.evaluation_report import generate_evaluation_report
    from models.evaluate import compute_delay_curves

    rolling_result = PIPELINE_STATE.get('rolling_result')
    if rolling_result is None:
        print("  no schedule stage result available — skipping.")
        return

    curves = compute_delay_curves()
    report = generate_evaluation_report(
        rolling_result, delay_curves=curves, gnn_metrics=PIPELINE_STATE.get('gnn_metrics'))
    RESULTS['evaluation_report'] = {
        'json': os.path.join(OUTPUT_DIR, "evaluation_report.json"),
        'txt': os.path.join(OUTPUT_DIR, "evaluation_report.txt"),
        'plots': report.get('plots', {}),
    }
    for k, v in report.get('plots', {}).items():
        print(f"  plot: {k} -> {v}")


# ------------------------------------------------------------ 4. disruption
def do_breakdown():
    """Secondary check: the fuzzy impact gate, end to end against real data."""
    from agent.impact_assessor import assess_machine_breakdown
    from collections import Counter
    t0 = time.time()
    assessments = assess_machine_breakdown(BREAKDOWN_ASSET)
    tiers = dict(Counter(a['tier'] for a in assessments))
    reserve = sum(1 for a in assessments if a.get('from_standby_reserve'))
    donors = sum(1 for a in assessments if a.get('donor_plan'))
    impacts = [a['impact_hours'] for a in assessments if a.get('impact_hours') is not None]
    RESULTS['breakdown'] = {
        'asset': BREAKDOWN_ASSET, 'affected_operations': len(assessments),
        'tiers': tiers, 'covered_from_reserve': reserve, 'donor_preemptions': donors,
        'max_impact_hours': max(impacts) if impacts else 0.0,
        'seconds': time.time() - t0,
    }
    print(f"  machine {BREAKDOWN_ASSET}: {len(assessments)} affected operations")
    print(f"  tier decisions: {tiers}")
    print(f"  covered from standby reserve: {reserve}   donor preemptions: {donors}")
    print(f"  max impact: {max(impacts) if impacts else 0:.1f} h   ({time.time()-t0:.0f}s)")


# ------------------------------------------------------------------- report
def main():
    t0 = time.time()
    banner("FINAL PIPELINE RUN -- single process, fixed origin and seed")
    print(f"seed={SEED}  population={POP}  generations={GENS}  "
          f"horizon={PLANNING_HORIZON_ORDERS} orders  polish budget={POLISH_EVALS}")

    stage("1. RESEED DATABASE", do_seed)
    stage("2. TRAIN GNN", do_train)
    stage("3. EVALUATE GNN", do_evaluate)
    stage("4. SCHEDULE (rolling horizon + local search)", do_schedule)
    stage("5. BREAKDOWN SIMULATION (fuzzy impact gate)", do_breakdown)
    stage("6. STANDING EVALUATION REPORT", do_evaluation_report)

    RESULTS['total_seconds'] = time.time() - t0
    _save()

    s = RESULTS['schedule']
    banner("FINAL RESULTS")
    print(f"  orders scheduled           {s['orders']}")
    print(f"  on-time rate               {s['on_time_pct']:.1f}%")
    print(f"  orders late                {s['orders_late']}")
    print(f"  mean added tardiness       {s['mean_tardiness_h']:.1f} h  (late orders; effective deadline)")
    print(f"  max added tardiness        {s['max_tardiness_h']:.1f} h  (effective deadline)")
    print(f"  makespan                   {s['makespan_days']:.1f} days")
    print(f"  throughput                 {s['throughput_orders_per_day']:.1f} orders/day")
    print(f"  labour utilisation         {s['labour_utilisation_pct']:.1f}%  (whole book)")
    margin = s.get('mean_margin_over_baseline_pct')
    print(f"  GA margin over baseline    {margin:.1f}%" if margin is not None else
          "  GA margin over baseline    n/a")
    print(f"  local search contribution  {s['mean_local_search_gain_pct']:+.2f}%")
    print(f"  commit / lookahead         {s['commit_size']} / {s['lookahead_size']} orders")
    print(f"\n  total runtime              {RESULTS['total_seconds']/60:.1f} min")
    print(f"  report written to          {os.path.join(OUTPUT_DIR, 'final_run_report.json')}")
    if 'evaluation_report' in RESULTS:
        print(f"  evaluation report          {RESULTS['evaluation_report']['txt']}")


if __name__ == "__main__":
    main()
