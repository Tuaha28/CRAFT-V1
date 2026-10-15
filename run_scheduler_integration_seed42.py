"""GNN-disabled, seed-42 authoritative worker-aware scheduler comparison."""
from __future__ import annotations
import copy
import hashlib
import json
import os
from pathlib import Path
import random
import sys
import time
from datetime import datetime

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
def _arg_value(name, default):
    if name not in sys.argv:
        return default
    index = sys.argv.index(name)
    if index + 1 >= len(sys.argv):
        raise ValueError(f'missing value for {name}')
    return sys.argv[index + 1]


SNAPSHOT = _arg_value('--snapshot', 'scheduler_integration_20260916_seed42')
OUT = ROOT / 'outputs' / _arg_value('--output-dir', 'scheduler_integration_seed42')
EXPORTS = OUT / 'exports'
SEED, POPULATION, GENERATIONS, POLISH_EVALS = 42, 50, 50, 400
STRATEGIES = ("FIFO", "EDF", "CriticalRatio", "UrgencyTiered", "RandomRepaired")
NEUTRAL_SIGNALS = {}  # evaluate_schedule defaults: delay=0.0, priority=0.5.


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _hashable(value):
    if isinstance(value, dict):
        return [(str(key), _hashable(item)) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))]
    if isinstance(value, (list, tuple)):
        return [_hashable(item) for item in value]
    if isinstance(value, set):
        return sorted((_hashable(item) for item in value), key=str)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, 'item'):
        return _hashable(value.item())
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def fingerprint(value):
    payload = json.dumps(_hashable(value), separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def as_json(value):
    if isinstance(value, Path): return str(value)
    if isinstance(value, datetime): return value.isoformat()
    if hasattr(value, "item"): return value.item()
    raise TypeError(type(value).__name__)


def main():
    if (OUT / "final_result.json").exists():
        raise RuntimeError("final result already exists; refusing to overwrite frozen experiment")
    OUT.mkdir(parents=True, exist_ok=True)
    EXPORTS.mkdir(exist_ok=True)
    os.environ["DB_NAME"] = "garment_fyp_db_ga_snapshot_" + SNAPSHOT

    import psutil
    import pandas as pd
    import agent.decision_engine as decision_module
    from agent.decision_engine import AIDecisionEngine
    from database.ga_snapshot import activate_snapshot, get_snapshot_origin
    activate_snapshot(SNAPSHOT)
    from database.db import get_engine
    from database.queries import (
        RESERVATION_FIXED, fetch_active_orders_and_routings, fetch_assignable_operators,
        fetch_committed_machine_state, fetch_committed_operator_reservations,
        fetch_concurrent_operator_capacity, fetch_line_factory_map, fetch_machine_reservation_records,
        fetch_machine_state, fetch_operator_reservation_records,
    )
    from ga.fitness import ALPHA, DELTA, EPSILON, GAMMA, SERVICE_LEVEL_WEIGHT, ZETA, evaluate_schedule
    from ga.heuristics import baseline_chromosomes
    from ga.rolling_horizon import run_rolling_horizon, run_rolling_horizon_baseline
    from ga.scheduler import GAScheduler
    import ga.schedule_utils as schedule_utils
    from ga.worker_allocation import operator_available_at
    from utils.factory_ownership import resolve_ownership
    from utils.machine_taxonomy import machine_type_to_operation_type
    from validation.schedule_validator import ScheduleValidationContext, validate_exported_schedule
    from utils.config import PLANNING_HORIZON_ORDERS

    # The generic exporter writes through this module-global destination; isolate this run.
    decision_module.OUTPUT_DIR = str(EXPORTS)
    start = time.perf_counter()
    process = psutil.Process()
    origin = get_snapshot_origin(SNAPSHOT)
    engine = get_engine()
    routings = fetch_active_orders_and_routings()
    machines = fetch_machine_state()
    lines = pd.read_sql("SELECT line_id, department_id, line_number, target_efficiency_percentage, is_active FROM Production_Lines", engine)
    active_lines = lines[lines.is_active == 1]
    dataset = pd.read_sql('SELECT * FROM _dataset_activation LIMIT 1', engine).iloc[0].to_dict()
    calibration = json.loads(pd.read_sql(
        'SELECT payload FROM Dataset_Calibration WHERE singleton_id=1', engine
    ).iloc[0, 0])
    uuid = str(calibration.get('dataset_version', dataset.get('dataset_version', dataset.get('dataset_uuid', 'unknown'))))
    capacity = fetch_concurrent_operator_capacity()
    ownership = resolve_ownership(routings.production_order_id)
    ownership_map = ownership.for_orders(routings.production_order_id)
    line_factory = fetch_line_factory_map()
    operator_frame = fetch_assignable_operators()
    decision_engine = AIDecisionEngine()
    operator_index = decision_engine._build_operator_index(operator_frame, lines, line_factory)
    base_machine_free, base_machine_last_order = fetch_committed_machine_state(origin)
    base_worker_reservations = fetch_committed_operator_reservations(origin)
    commit_size = int(PLANNING_HORIZON_ORDERS or routings.production_order_id.nunique())
    repair_gate_path = ROOT / 'outputs' / 'scheduler_integration_seed42_repaired_metrics' / 'WORKER_OVERLAP_REPAIR_GATE.json'
    repair_gate_sha = sha(repair_gate_path)
    expected_repair_gate_sha = '150ae3bb98be48477fe494de9d3f63b78d55ab666a252c56ded7873aff328ef8'
    if repair_gate_sha != expected_repair_gate_sha:
        raise RuntimeError('worker-overlap repair gate identity mismatch')
    parity_gate_path = ROOT / 'outputs' / 'scheduler_parity_fix_preflight_20260916_s42' / 'SCORE_EXPORT_PARITY_GATE.json'
    parity_gate_sha = sha(parity_gate_path)
    if parity_gate_sha != '540f968b81bda3ca759cfa1820e3bd1e292c916e02172d92904a76085e0a4b01':
        raise RuntimeError('score/export parity gate identity mismatch')
    if json.loads(parity_gate_path.read_text(encoding='utf-8')).get('status') != 'SCORE_EXPORT_PARITY_PASS':
        raise RuntimeError('score/export parity gate has not passed')
    initial_state_fingerprints = {
        'orders': fingerprint(sorted(map(str, routings.production_order_id.unique()))),
        'machine_free': fingerprint(base_machine_free),
        'machine_last_order': fingerprint(base_machine_last_order),
        'worker_reservations': fingerprint(base_worker_reservations),
        'operator_index': fingerprint(operator_index),
        'ownership': fingerprint(ownership_map),
        'calendar_source': sha(ROOT / 'utils' / 'work_calendar.py'),
    }
    if NEUTRAL_SIGNALS:
        raise RuntimeError("GNN-disabled driver requires an empty neutral signal map")
    if routings.empty or machines.empty or active_lines.empty or capacity <= 0:
        raise RuntimeError("scheduler input gate failed: empty routings/machines/lines or no concurrent operators")

    # Validator material comes from source resource/route data, never the decoder.
    machine_ctx = {}
    for asset, group in machines.dropna(subset=["asset_id"]).groupby("asset_id"):
        first = group.iloc[0]
        machine_ctx[int(asset)] = {
            "line_id": int(first.line_id), "factory_id": int(first.factory_id),
            "machine_type": str(first.operation_type).strip().lower(),
            "capabilities": sorted({str(v).strip().lower() for v in group.operation_type.dropna()}),
        }
    worker_ctx = {}
    for employee, group in operator_frame.groupby("employee_id"):
        first = group.iloc[0]
        worker_ctx[int(employee)] = {
            "factory_id": int(first.factory_id),
            "skills": sorted({str(v).strip().lower() for v in group.operation_type.dropna()}),
        }
    route_by_key = {f"{r.production_order_id}_{r.operation_id}": r for r in routings.itertuples(index=False)}

    fixed_machine_records = [r for r in fetch_machine_reservation_records(origin) if r.classification == RESERVATION_FIXED]
    fixed_worker_records = [r for r in fetch_operator_reservation_records(origin) if r.classification == RESERVATION_FIXED]
    committed_machine = [{"asset_id": r.asset_id, "start": r.start, "end": r.end, "operation_key": f"fixed_{r.schedule_item_id}"} for r in fixed_machine_records]
    committed_worker = [{"employee_id": r.employee_id, "start": a, "end": b, "operation_key": f"fixed_{r.schedule_item_id}"} for r in fixed_worker_records for a, b in r.segments]

    def validate_arm(name, result):
        rows = []
        for summary in result.get("export_summaries", []):
            rows.extend(json.loads(Path(summary["schedule_file"]).read_text(encoding="utf-8")))
        expected = []
        for row in rows:
            key = f"{row['production_order_id']}_{row['operation_id']}"
            source = route_by_key.get(key)
            if source is None:
                raise RuntimeError(f"export references unknown operation {key}")
            optype = machine_type_to_operation_type(source.required_machine_type)
            expected.append({
                "production_order_id": str(source.production_order_id), "operation_id": str(source.operation_id),
                "operation_sequence_number": source.operation_sequence_number,
                "release_time": source.release_date, "deadline": source.order_deadline,
                "eligible_line_ids": result["eligibility"][key], "required_machine_type": optype,
                "required_operation_type": optype, "required_machine_count": int(row["machines_used"]),
                "required_workers": int(row["operators_used"]), "productive_minutes": float(row["productive_minutes"]),
                "setup_minutes": float(row["setup_minutes"]),
            })
        context = ScheduleValidationContext(
            expected_operations=expected, ownership_by_order=ownership_map, line_factory=line_factory,
            machines=machine_ctx, workers=worker_ctx, committed_machine_intervals=committed_machine,
            committed_worker_intervals=committed_worker,
            worker_is_available=lambda eid, when: operator_available_at(operator_index, int(eid), when),
        )
        report = validate_exported_schedule(rows, context).as_dict()
        report["arm"] = name
        report["rows"] = len(rows)
        return report

    # Pre-GA deterministic EDF decode/export/validation with fixed commitments.
    order_release = routings.groupby("production_order_id").release_date.min().reset_index().sort_values(["release_date", "production_order_id"])
    first_ids = set(order_release.production_order_id.head(commit_size))
    pre_sub = routings[routings.production_order_id.isin(first_ids)].copy()
    pre_sched = GAScheduler(pre_sub, machines, lines, NEUTRAL_SIGNALS, current_time=origin,
                            present_operator_count=capacity, ownership_snapshot=ownership,
                            initial_machine_free=base_machine_free, initial_machine_last_order=base_machine_last_order,
                            operator_index=operator_index, initial_operator_reservations=base_worker_reservations,
                            scored_order_ids={str(v) for v in first_ids})
    missing_eligibility = [k for k, v in pre_sched.factory.eligibility_map.items() if not v]
    if missing_eligibility: raise RuntimeError(f"pre-GA eligibility failure: {missing_eligibility[:5]}")
    pre_chromo = baseline_chromosomes(pre_sched.factory, pre_sched.op_details, pre_sched.order_deadlines_minutes, pre_sched.seq_lookup)["EDF"]
    pre_fit, pre_details = evaluate_schedule(pre_chromo, pre_sched.num_ops, pre_sched.op_details, NEUTRAL_SIGNALS,
        all_line_ids=pre_sched.active_line_ids, order_deadlines=pre_sched.order_deadlines_minutes,
        order_release_minutes=pre_sched.order_release_minutes, machine_pools=pre_sched.factory.machine_ids_per_line_map,
        machine_line_map=pre_sched.machine_line_map, usable_machine_count=pre_sched.usable_machine_count,
        present_operator_count=capacity, op_sequence_map=pre_sched.seq_lookup, initial_machine_free=base_machine_free,
        initial_machine_last_order=base_machine_last_order, op_optype=pre_sched.factory.optype_for_op,
        protected_machine_type=pre_sched.protected_machine_type, base_time=origin, operator_index=operator_index,
        initial_operator_reservations=base_worker_reservations, return_details=True)
    if pre_details.get("invalid") or not pre_details.get("execution_feasible"):
        raise RuntimeError(f"pre-GA authoritative phenotype infeasible: {pre_details.get('infeasibility')}")
    pre_export = decision_engine._export_proposed_schedule(pre_chromo, pre_sched.num_ops, pre_sched.op_details, NEUTRAL_SIGNALS,
        lines, pre_sched.factory.eligibility_map, pre_sched.order_deadline_abs, pre_sub, machines,
        label="preflight_edf", machine_pools=pre_sched.factory.machine_ids_per_line_map, operator_index=operator_index,
        present_operator_count=capacity, usable_machine_count=pre_sched.usable_machine_count,
        op_optype=pre_sched.factory.optype_for_op, protected_machine_type=pre_sched.protected_machine_type,
        initial_machine_free=base_machine_free, initial_machine_last_order=base_machine_last_order,
        initial_operator_reservations=base_worker_reservations, base_time=origin,
        phenotype_details=pre_details, scored_fitness=pre_fit[0])
    if pre_export["phenotype_hash"] != pre_details["phenotype_hash"] or abs(pre_export["fitness"] - pre_fit[0]) > 1e-8:
        raise RuntimeError("pre-GA score/export parity failure")
    pre_result = {"export_summaries": [pre_export], "eligibility": pre_sched.factory.eligibility_map}
    pre_validation = validate_arm("preflight_edf", pre_result)
    if not pre_validation["valid"]:
        raise RuntimeError(f"pre-GA independent validator failure: {pre_validation['errors'][:3]}")

    preflight_runtime_seconds = time.perf_counter() - start
    preflight_rss_mb = process.memory_info().rss / (1024 * 1024)
    provenance = {
        "snapshot": SNAPSHOT, "snapshot_database": os.environ["DB_NAME"], "origin": origin,
        "source_database": "garment_fyp_db", "dataset_activation": dataset, "dataset_uuid": uuid,
        "git_head": os.popen("git rev-parse HEAD").read().strip(), "worktree_dirty": True,
        "seed": SEED, "orders": int(routings.production_order_id.nunique()), "operations": int(len(routings)),
        "machines": int(machines.asset_id.nunique()), "workers": int(operator_frame.employee_id.nunique()),
        "commit_size": commit_size, "lookahead_size": commit_size, "population": POPULATION,
        "generations": GENERATIONS, "polish_evals": POLISH_EVALS, "neutral_signal_map": "empty; default delay=0.0 priority=0.5",
        "gnn_operational_status": "EVALUATED_NOT_PROMOTED_FOR_OPERATIONAL_SCHEDULING_DECISIONS",
        "reservation_policy": "policy_a_keep_fixed_replace_movable_future", "worker_policy": "same_worker_pause_resume_segmented",
        "shifts": ["06:00-14:00", "14:00-22:00", "22:00-06:00"],
        "line_efficiency_mean_pct": float(active_lines.target_efficiency_percentage.mean()),
        "risk_buffer_at_certainty": float(schedule_utils.RISK_BUFFER_AT_CERTAINTY),
        "validator_sha256": sha(ROOT / "validation" / "schedule_validator.py"),
        "source_sha256": {str(p.relative_to(ROOT)): sha(p) for p in [ROOT/"ga"/"scheduler.py", ROOT/"ga"/"fitness.py", ROOT/"ga"/"rolling_horizon.py", ROOT/"ga"/"worker_allocation.py", ROOT/"agent"/"decision_engine.py"]},
        "known_hardening_issues_not_addressed": ["breakdown_no_action_future_machine", "local_patch_bypasses_authoritative_segments_validator", "persistent_manager_priority_incomplete", "maintenance_technicians_not_constrained", "frontend_api_productionization"],
        "pre_ga": {"input_eligibility_missing": 0, "phenotype_hash": pre_details["phenotype_hash"], "fitness": float(pre_fit[0]), "export_fitness": pre_export["fitness"], "validator": pre_validation, "runtime_seconds": preflight_runtime_seconds, "rss_mb_end": preflight_rss_mb},
    }
    (OUT / "scheduler_snapshot_manifest.json").write_text(json.dumps(provenance, indent=2, default=as_json), encoding="utf-8")
    if '--preflight-only' in sys.argv:
        print(json.dumps({'preflight': provenance['pre_ga'], 'out': str(OUT)}, default=as_json))
        return

    comparison_manifest = {
        'manifest_version': 1,
        'snapshot': SNAPSHOT,
        'snapshot_database': os.environ['DB_NAME'],
        'snapshot_origin': origin,
        'source_database': 'garment_fyp_db',
        'dataset_version': uuid,
        'dataset_activation': dataset,
        'dataset_calibration': calibration,
        'git_head': os.popen('git rev-parse HEAD').read().strip(),
        'worktree_dirty': True,
        'seed': SEED,
        'gnn_operational_status': 'EVALUATED_NOT_PROMOTED_FOR_OPERATIONAL_SCHEDULING_DECISIONS',
        'gnn_signal_map': 'empty; evaluator defaults only: delay=0.0 priority=0.5',
        'repair_gate': {'path': str(repair_gate_path), 'sha256': repair_gate_sha},
        'scheduler_config': {
            'commit_horizon_orders': commit_size,
            'lookahead_orders': commit_size,
            'worker_policy': 'same_worker_pause_resume_segmented',
            'worker_reservation_policy': 'policy_a_keep_fixed_replace_movable_future',
            'machine_commitments': 'fixed/in_flight retained',
            'shifts': ['06:00-14:00', '14:00-22:00', '22:00-06:00'],
            'line_efficiency_mean_pct': float(active_lines.target_efficiency_percentage.mean()),
            'calendar_source_sha256': sha(ROOT / 'utils' / 'work_calendar.py'),
            'validator_sha256': sha(ROOT / 'validation' / 'schedule_validator.py'),
        },
        'ga_config': {
            'population': POPULATION, 'generations': GENERATIONS, 'seed': SEED,
            'polish_max_evals_per_window': POLISH_EVALS,
            'selection_metric': 'minimum authoritative worker-aware scalar fitness',
        },
        'fitness_definition': {
            'makespan_weight': ALPHA, 'delay_penalty_weight': GAMMA,
            'priority_reward_weight': DELTA, 'load_balance_weight': EPSILON,
            'lateness_weight': ZETA, 'service_level_weight': SERVICE_LEVEL_WEIGHT,
            'deadline_semantics': 'effective deadline=max(customer deadline, scheduling origin)',
        },
        'baselines': {
            'FIFO': 'deterministic order-release sequence',
            'EDF': 'deterministic earliest effective due date sequence',
            'CriticalRatio': 'deterministic critical-ratio sequence',
            'UrgencyTiered': 'deterministic urgency-tiered sequence',
            'RandomRepaired': 'fixed-seed deterministic repaired random sequence',
        },
        'apples_to_apples': {
            'same_order_book': True, 'same_initial_reservations': True,
            'same_worker_machine_calendar_eligibility_policy': True,
            'independent_mutable_state_per_arm': True,
            'initial_state_fingerprints': initial_state_fingerprints,
        },
        'source_sha256': {str(x.relative_to(ROOT)): sha(x) for x in [
            ROOT / 'ga' / 'scheduler.py', ROOT / 'ga' / 'fitness.py',
            ROOT / 'ga' / 'rolling_horizon.py', ROOT / 'ga' / 'worker_allocation.py',
            ROOT / 'utils' / 'work_calendar.py', ROOT / 'agent' / 'decision_engine.py',
            ROOT / 'validation' / 'schedule_validator.py', ROOT / 'run_scheduler_integration_seed42.py',
        ]},
    }
    comparison_path = OUT / 'comparison_manifest.json'
    comparison_path.write_text(json.dumps(comparison_manifest, indent=2, default=as_json), encoding='utf-8')
    (OUT / 'comparison_manifest.sha256').write_text(sha(comparison_path) + '\n', encoding='ascii')

    def run_arm(name):
        arm_start = time.perf_counter()
        kwargs = dict(commit_size=commit_size, export=True, decision_engine=decision_engine, operator_index=operator_index,
                      ownership_snapshot=ownership, committed_machine_state=dict(base_machine_free),
                      committed_machine_last_order=dict(base_machine_last_order), initial_operator_reservations=copy.deepcopy(base_worker_reservations),
                      export_label_prefix=f"scheduler_seed42_{name.lower()}", log_fn=print)
        if name == "GA":
            result = run_rolling_horizon(routings, machines, lines, NEUTRAL_SIGNALS, capacity, origin,
                pop_size=POPULATION, generations=GENERATIONS, polish_evals=POLISH_EVALS, seed=SEED, **kwargs)
        else:
            result = run_rolling_horizon_baseline(name, routings, machines, lines, NEUTRAL_SIGNALS, capacity, origin, **kwargs)
        result["eligibility"] = {}
        # Existing rolling functions have already asserted score/export phenotype and reservation parity per window.
        # Reconstruct eligibility independently for validator expectation from one fresh, non-optimizing factory per export order-set.
        for summary in result.get("export_summaries", []):
            rows = json.loads(Path(summary["schedule_file"]).read_text(encoding="utf-8"))
            ids = {int(r["production_order_id"]) for r in rows}
            sub = routings[routings.production_order_id.isin(ids)]
            probe = GAScheduler(sub, machines, lines, NEUTRAL_SIGNALS, current_time=origin, present_operator_count=capacity,
                                ownership_snapshot=ownership, operator_index=operator_index)
            result["eligibility"].update(probe.factory.eligibility_map)
        validation = validate_arm(name, result)
        if not validation["valid"]:
            raise RuntimeError(f"{name} independent validator failure: {validation['errors'][:3]}")
        aggregate = result["aggregate"]
        summaries = result.get("export_summaries", [])
        return {"aggregate": aggregate, "windows": result["windows"], "logbooks": result.get("logbooks", []),
                "export_summaries": summaries, "validator": validation,
                "objective_fitness_sum": float(sum(w.get("fitness", 0.0) for w in result["windows"])),
                "understaffed_operations": int(sum(s.get("understaffed_operations", 0) for s in summaries)),
                "unfilled_worker_minutes": float(sum(sum(d.get("shortfall_worker_minutes", 0) for d in s.get("understaffed_detail", [])) for s in summaries)),
                "runtime_seconds": time.perf_counter() - arm_start,
                "machine_utilisation": "not instrumented; do not infer from labour utilisation"}

    arms = {"GA": run_arm("GA")}
    for name in STRATEGIES:
        arms[name] = run_arm(name)
    final = {"provenance": provenance, "arms": arms, "runtime_seconds": time.perf_counter()-start,
             "peak_observed_rss_bytes": process.memory_info().rss,
             "decision": "pending interpretation from frozen metrics; no post-run tuning performed"}
    (OUT / "final_result.json").write_text(json.dumps(final, indent=2, default=as_json), encoding="utf-8")
    (OUT / "final_result.sha256").write_text(sha(OUT / "final_result.json") + "\n", encoding="ascii")
    print(json.dumps({"out": str(OUT), "sha256": sha(OUT / "final_result.json"), "arms": {k: v["aggregate"] for k,v in arms.items()}}, default=as_json))

if __name__ == "__main__":
    main()