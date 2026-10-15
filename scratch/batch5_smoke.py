"""
Pre-reseed lifecycle smoke test.

Proves the whole chain works end to end against an ISOLATED staging database
before committing to the long reseed+retrain. Deliberately small on the one
axis that costs hours (epochs) and full-scale everywhere else, because the
things most likely to break -- artifact identity, feature schema, ownership
scoping, split manifests -- are scale-independent.

TOUCHES NO LIVE STATE. Every step runs against the staging database named on
the command line. Nothing is promoted.

Usage:  python scratch/batch5_smoke.py <staging_db_name>
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def banner(n, title):
    print(f"\n{'=' * 78}\nSTEP {n}: {title}\n{'=' * 78}", flush=True)


def main(staging_db):
    # Point every consumer at the staging database BEFORE importing anything
    # that builds an engine at import time.
    os.environ['DB_NAME'] = staging_db
    from utils import config as cfg
    cfg.DATABASE_URL = cfg.DATABASE_URL.rsplit('/', 1)[0] + '/' + staging_db

    import database.db as dbmod
    from sqlalchemy import create_engine, text
    dbmod.engine = create_engine(cfg.DATABASE_URL, pool_pre_ping=True)

    with dbmod.engine.connect() as c:
        actual = c.execute(text('SELECT DATABASE()')).scalar()
    assert actual == staging_db, f"engine points at {actual}, not {staging_db}"
    print(f"All steps run against: {actual}")

    results = {'staging_db': staging_db}

    # ---------------------------------------------------------------- gate
    banner(1, "Extended validation gate on a GOOD dataset")
    import mysql.connector
    from data_generation.validation_records import validate_and_record
    from data_generation import seed_database_fixed as seed
    conn_cfg = dict(seed.db_config)
    conn_cfg['database'] = staging_db
    conn = mysql.connector.connect(**conn_cfg)
    try:
        conn.start_transaction(readonly=True)
        gate = validate_and_record(conn)['result']
    finally:
        conn.close()
    print(f"valid: {gate['valid']}")
    print(f"errors: {json.dumps(gate['errors'], indent=2)}")
    results['gate_valid'] = gate['valid']
    results['gate_errors'] = gate['errors']
    if not gate['valid']:
        print("*** GATE FAILED on the freshly seeded dataset -- stopping ***")
        return results

    # ------------------------------------------------------- graph + edges
    banner(2, "Graph build with factory-scoped disjunctive edges")
    from graph.graph_builder import FactoryGraphBuilder
    builder = FactoryGraphBuilder()
    graph, labels = builder.build_graph()
    n_edges = graph['operation', 'contends_with', 'operation'].edge_index.shape[1]
    n_ops = graph['operation'].x.shape[0]
    print(f"operation nodes      : {n_ops}")
    print(f"operation features   : {graph['operation'].x.shape[1]}")
    print(f"DISJUNCTIVE EDGES    : {n_edges:,}")
    print(f"train/val split      : {int(graph['operation'].train_mask.sum())}"
          f" / {int(graph['operation'].val_mask.sum())}")
    results.update(operation_nodes=n_ops, feature_width=graph['operation'].x.shape[1],
                   disjunctive_edges=n_edges)

    # Was scoping actually ACTIVE, or did it silently fall back company-wide?
    from utils.factory_ownership import load_factory_ownership
    ownership = load_factory_ownership(dbmod.engine)
    print(f"ownership rows       : {len(ownership)}  "
          f"(scoping {'ACTIVE' if ownership else 'INACTIVE -- company-wide'})")
    results['ownership_rows'] = len(ownership)
    if ownership:
        from collections import Counter
        print(f"factory distribution : {dict(sorted(Counter(ownership.values()).items()))}")
        results['factory_distribution'] = dict(sorted(Counter(ownership.values()).items()))

    # ------------------------------------------------------------- training
    banner(3, "Train a handful of epochs (not 150)")
    import models.train as train_mod
    epochs = int(os.getenv('SMOKE_EPOCHS', '3'))
    model = train_mod.train_gnn(epochs=epochs)
    ckpt = ROOT / 'outputs' / 'checkpoints' / 'gnn_multitask_latest.pth'
    print(f"checkpoint            : {ckpt.exists()}")
    print(f"preprocessing artifact: {Path(str(ckpt) + '.preprocessing.json').exists()}")
    print(f"split manifest        : {(ROOT / 'outputs' / 'split_manifest.json').exists()}")
    results['epochs_run'] = epochs

    # ----------------------------------------------------------- evaluation
    banner(4, "evaluate.py against that checkpoint")
    import importlib
    import models.evaluate as ev
    importlib.reload(ev)
    metrics = ev.evaluate_gnn()
    print(json.dumps({k: (round(v, 4) if isinstance(v, float) else v)
                      for k, v in metrics.items()}, indent=2))
    results['metrics'] = {k: (round(v, 4) if isinstance(v, float) else v)
                          for k, v in metrics.items()}

    # ------------------------------------------------------------ schedule
    banner(5, "One scheduling pass")
    from ga.scheduler import GAScheduler
    from database.queries import (fetch_active_orders_and_routings, fetch_machine_state,
                                  apply_planning_horizon)
    import pandas as pd
    from datetime import datetime
    routings = apply_planning_horizon(fetch_active_orders_and_routings(), 40)
    machines = fetch_machine_state()
    with dbmod.engine.connect() as c:
        lines = pd.read_sql(text(
            "SELECT line_id, department_id, line_number, target_efficiency_percentage, "
            "is_active FROM Production_Lines WHERE is_active=1"), c)
    sched = GAScheduler(routings, machines, lines, {}, current_time=datetime.now(),
                        present_operator_count=500)
    sols, _log = sched.optimize(pop_size=8, generations=3, n_solutions=1)
    print(f"solutions: {len(sols)}   best fitness: {sols[0][1]:.2f}")
    results['schedule_fitness'] = round(float(sols[0][1]), 2)
    results['schedule_operations'] = sched.num_ops

    banner('OK', "chain complete")
    out = ROOT / 'outputs' / 'batch5_smoke_result.json'
    out.write_text(json.dumps(results, indent=2), encoding='utf-8')
    print(f"written: {out}")
    return results


if __name__ == '__main__':
    if len(sys.argv) < 2:
        sys.exit("usage: python scratch/batch5_smoke.py <staging_db_name>")
    main(sys.argv[1])
