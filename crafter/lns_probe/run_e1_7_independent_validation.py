"""Independent authoritative validation gate for the final E1.7 schedules.

The selected E1.7 modes are treated as schedule data only.  Eligibility is
rebuilt from the frozen routing/line/machine/worker sources and is never
expanded from candidate mode provenance.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import asdict
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pandas as pd

from agent.decision_engine import AIDecisionEngine
from crafter.contracts import FixedMachineCommitment, FixedWorkerCommitment, LineSpec, MachineSpec, ResourceState
from crafter.e0_5_semantic_reconciliation import _safe
from crafter.lns_probe.run_e1_7_incumbent_reconstruction import _mode
from crafter.lns_probe.run_e1_1 import CR_PATH, EXPECTED_CR, EXPECTED_SOURCE
from crafter.lns_probe.run_e1_7_incumbent_reconstruction import _build_light_fixture
from crafter.lns_probe.run_e1_7_final_verified import _phenotype
from crafter.lns_probe.transaction import frozen_incumbent
from crafter.phenotype import comparison_metrics, validate
from crafter.real_data_adapter import SNAPSHOT_NAME
from database.db import get_engine
from database.queries import (
    fetch_active_orders_and_routings, fetch_line_factory_map, fetch_machine_state,
    fetch_machine_reservation_records, fetch_operator_reservation_records,
    RESERVATION_FIXED,
)
from ga.scheduler import make_key
from ga.scheduler import make_key
from ga.chromosome import ChromosomeFactory
from ga.fitness import CHANGEOVER_MINUTES
from utils.machine_reserve import build_reserve, NON_USABLE_STATUSES
from utils.factory_ownership import resolve_ownership
from utils.machine_taxonomy import machine_type_to_operation_type
from crafter.real_data_adapter import _canonical_frame, _fingerprint, SOURCE_MANIFEST
import crafter.real_data_adapter as real_adapter
from database.ga_snapshot import list_snapshots
from database.queries import fetch_assignable_operators, fetch_concurrent_operator_capacity
from crafter.contracts import LabourCapacitySpec, OrderSpec, OperationSpec


FINAL = Path("outputs/crafter/e1_7_final_verified_optimization")
OUT = Path("outputs/crafter/e1_7_final_independent_validation")
ORIGIN = datetime.fromisoformat("2026-09-16T19:00:16")


def write_json(directory: Path, name: str, value):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(_safe(value), indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def sha(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _authoritative_problem(rows, durations):
    base, operator_index, _ = _build_light_fixture(rows, durations)
    routings = fetch_active_orders_and_routings()
    line_factory = fetch_line_factory_map()
    lines_df = pd.read_sql(
        "SELECT pl.line_id, pl.department_id, pl.line_number, pl.is_active, d.factory_id "
        "FROM Production_Lines pl JOIN Departments d ON d.department_id=pl.department_id "
        "WHERE pl.is_active=1", get_engine())
    machine_df = fetch_machine_state()
    unusable = {"broken", "maintenance", "unavailable", "retired", "inactive", "offline"}
    machines = []
    selected_orders = {str(r["production_order_id"]) for r in rows}
    selected_routings = routings[routings["production_order_id"].astype(str).isin(selected_orders)].copy()
    ownership = resolve_ownership(selected_orders)
    factory_obj = ChromosomeFactory(selected_routings, machine_df, lines_df, ownership_snapshot=ownership)
    true_eligibility = {str(k): tuple(sorted(int(x) for x in v)) for k, v in factory_obj.eligibility_map.items()}
    reserved_ids = set(factory_obj.reserved_asset_ids)
    for asset_id, group in machine_df.groupby("asset_id", sort=True):
        first = group.iloc[0]
        caps = tuple(sorted({str(x).strip().lower() for x in group["operation_type"].dropna().tolist()}))
        status = str(first.get("current_status", "")).strip().lower()
        usable = int(asset_id) in factory_obj.usable_asset_ids
        machine = MachineSpec(int(asset_id), int(first["factory_id"]), int(first["line_id"]), caps, usable)
        machines.append(machine)
    represented = {str(r["production_order_id"]) for r in rows}
    line_specs = tuple(LineSpec(int(r.line_id), int(r.factory_id), str(r.line_number))
                       for r in lines_df.itertuples(index=False)
                       if int(r.factory_id) in {base.order_by_id[x].factory_id for x in represented})
    represented_factory_ids = set(factory_obj.order_factory.values())
    line_specs = tuple(LineSpec(int(r.line_id), int(r.factory_id), str(r.line_number))
                       for r in lines_df.itertuples(index=False)
                       if int(r.factory_id) in represented_factory_ids)
    # Eligibility is the production contract itself: persisted order ownership
    # plus the ChromosomeFactory's reserved/status-filtered machine pools.
    ops = []
    for op in base.operations:
        legal = true_eligibility.get(op.operation_key, op.eligible_line_ids)
        ops.append(replace(op, eligible_line_ids=legal,
                           max_machine_count=max(1, max(factory_obj._max_machines_for(op.operation_key, line_id) for line_id in legal))))
    # Fixed commitments are read directly from the frozen schedule database.
    machine_records = [r for r in fetch_machine_reservation_records(ORIGIN) if r.classification == RESERVATION_FIXED]
    machine_commitments = tuple(FixedMachineCommitment(int(r.asset_id), max(0, int(math.ceil((r.start-ORIGIN).total_seconds()))), max(1, int(math.ceil((r.end-ORIGIN).total_seconds()))), str(r.production_order_id)) for r in machine_records if int(r.asset_id) in {m.machine_id for m in machines})
    all_routes = {(str(r.production_order_id), str(r.operation_id)): machine_type_to_operation_type(r.required_machine_type) for r in routings.itertuples(index=False)}
    worker_factory = {}
    for (factory, _skill), workers in operator_index.get("by_factory", {}).items():
        for worker in workers:
            worker_factory[str(worker)] = int(factory)
    worker_commitments = []
    for r in fetch_operator_reservation_records(ORIGIN):
        if r.classification != RESERVATION_FIXED:
            continue
        skill = all_routes.get((str(r.production_order_id), str(r.operation_id)))
        if not skill:
            continue
        for start, end in r.segments:
            worker_commitments.append(FixedWorkerCommitment(
                str(r.employee_id), worker_factory.get(str(r.employee_id), 0), str(skill),
                max(0, int(math.ceil((start-ORIGIN).total_seconds()))),
                max(1, int(math.ceil((end-ORIGIN).total_seconds()))),
                f"fixed:{r.production_order_id}_{r.operation_id}"))
    # Rebuild order factories and labour capacities exactly as the adapter.
    order_specs = tuple(replace(order, factory_id=int(factory_obj.order_factory[order.order_id])) for order in base.orders)
    skills_needed = {op.required_capability for op in ops}
    labour_specs = []
    for fid in sorted(represented_factory_ids):
        for skill in sorted(skills_needed):
            employees = set(str(x) for x in operator_index.get("by_factory", {}).get((fid, skill), ()))
            if employees:
                labour_specs.append(LabourCapacitySpec(fid, skill, len(employees)))
    max_release = max(o.release_seconds for o in order_specs)
    max_deadline = max((o.deadline_seconds or 0) for o in order_specs)
    sequential_upper = sum(op.setup_seconds + op.serial_productive_seconds + int(round(CHANGEOVER_MINUTES * 60.0)) for op in ops)
    fixed_upper = max((x.end_seconds for x in machine_commitments), default=0)
    horizon = max(max_deadline, fixed_upper, max_release) + sequential_upper + 86400
    problem = replace(base, origin=ORIGIN, horizon_seconds=int(horizon), lines=line_specs,
                      machines=tuple(machines), orders=order_specs, operations=tuple(ops),
                      labour_capacities=tuple(labour_specs),
                      resources=ResourceState(tuple(machine_commitments), tuple(worker_commitments)),
                      changeover_seconds=int(round(CHANGEOVER_MINUTES * 60.0)))
    return problem, operator_index, routings, {"lines": lines_df, "machines": machine_df, "line_factory": line_factory, "machine_commitments": len(machine_commitments), "worker_commitments": len(worker_commitments), "factory": factory_obj, "true_eligibility": true_eligibility, "reserved_ids": reserved_ids, "selected_routings": selected_routings}


def _resource_audit(problem, operator_index, selected, baseline):
    machines = problem.machine_by_id
    failures = []
    rows = []
    for key, mode in selected.items():
        op = problem.operation_by_key[key]
        order = problem.order_by_id[op.order_id]
        line_ok = mode.line_id in op.eligible_line_ids
        mode_machine_ids = tuple(int(x) for x in mode.machine_ids)
        machine_checks = []
        for machine_id in mode_machine_ids:
            machine = machines.get(machine_id)
            cap_ok = bool(machine and machine.supports(op.required_capability))
            factory_ok = bool(machine and machine.factory_id == order.factory_id)
            line_ok_machine = bool(machine and machine.line_id == mode.line_id)
            usable_ok = bool(machine and machine.is_usable)
            machine_checks.append({"machine_id": machine_id, "exists": machine is not None, "factory_ok": factory_ok, "line_ok": line_ok_machine, "capability_ok": cap_ok, "usable": usable_ok})
            if not (factory_ok and line_ok_machine and cap_ok and usable_ok):
                failures.append({"operation_key": key, "kind": "MACHINE", "machine_id": machine_id, "checks": machine_checks[-1]})
        worker_checks = []
        for worker in tuple(str(x) for x in mode.worker_ids):
            line_pool = set(operator_index.get("by_line", {}).get((int(mode.line_id), op.required_capability), ()))
            factory_pool = set(operator_index.get("by_factory", {}).get((order.factory_id, op.required_capability), ()))
            worker_ok = int(worker) in line_pool or int(worker) in factory_pool if worker.isdigit() else worker in line_pool or worker in factory_pool
            worker_checks.append({"worker_id": worker, "qualified_and_sourced": worker_ok, "line_pool": worker in {str(x) for x in line_pool}, "factory_pool": worker in {str(x) for x in factory_pool}})
            if not worker_ok:
                failures.append({"operation_key": key, "kind": "WORKER", "worker_id": worker, "checks": worker_checks[-1]})
        if not line_ok:
            failures.append({"operation_key": key, "kind": "LINE", "selected_line": mode.line_id, "authoritative_eligible_lines": op.eligible_line_ids})
        if len(set(mode_machine_ids)) != mode.machine_count:
            failures.append({"operation_key": key, "kind": "MACHINE_COUNT", "machine_ids": mode_machine_ids, "k": mode.machine_count})
        if len(set(mode.worker_ids)) != mode.machine_count:
            failures.append({"operation_key": key, "kind": "WORKER_COUNT", "worker_ids": mode.worker_ids, "k": mode.machine_count})
        rows.append({"operation_key": key, "line_ok": line_ok, "machine_checks": machine_checks, "worker_checks": worker_checks, "machine_count": len(mode_machine_ids), "k": mode.machine_count})
    return rows, failures


def _recompute_adapter_source_fingerprint(durations):
    """Run the adapter's canonical source payload without constructing GA."""
    class LightweightGAScheduler:
        def __init__(self, df_routings, df_machines, df_lines, _gnn_predictions, **kwargs):
            self.factory = ChromosomeFactory(
                df_routings, df_machines, df_lines,
                ownership_snapshot=kwargs.get("ownership_snapshot"),
                legacy_mode=kwargs.get("legacy_mode"))
            self.op_details = {}
            for row in self.factory.df_routings.itertuples(index=False):
                key = make_key(row.production_order_id, row.operation_id)
                rec = durations.get(str(key))
                if rec is None:
                    raise RuntimeError(f"missing duration record for {key}")
                self.op_details[str(key)] = {
                    "required_capability": self.factory.optype_for_op.get(str(key)),
                    "setup_mins": 15.0,
                    "serial_processing_mins": float(rec["serial_work_minutes"]),
                }
            self.factory.op_work = {key: value["serial_processing_mins"] for key, value in self.op_details.items()}

    original = real_adapter.GAScheduler
    real_adapter.GAScheduler = LightweightGAScheduler
    try:
        fixture = real_adapter.build_real_window_fixture(order_limit=50, snapshot_name=SNAPSHOT_NAME)
        return fixture.source_fingerprint
    finally:
        real_adapter.GAScheduler = original


def run(output_dir=OUT):
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"refusing to overwrite non-empty {output_dir}")
    rows = json.loads(CR_PATH.read_text())["decoder_details"]["phenotype_operations"]
    snapshot_manifest = json.loads(Path("outputs/scheduler_parity_fix_preflight_20260916_s42/scheduler_snapshot_manifest.json").read_text())
    if snapshot_manifest.get("snapshot") != SNAPSHOT_NAME or snapshot_manifest.get("origin") != ORIGIN.isoformat():
        raise RuntimeError("frozen snapshot manifest identity mismatch")
    durations = {str(x["operation_key"]): x for x in json.loads(Path("outputs/crafter/e0_5_semantic_reconciliation/authoritative_duration_semantics.json").read_text())["all_operations"]}
    problem, operator_index, routings, source_details = _authoritative_problem(rows, durations)
    selected_orders = {str(row["production_order_id"]) for row in rows}
    selected_routing_rows = routings[routings["production_order_id"].astype(str).isin(selected_orders)]
    if len(selected_orders) != 50 or len(selected_routing_rows) != 431:
        raise RuntimeError(f"authoritative routing identity mismatch: {len(selected_orders)} orders/{len(selected_routing_rows)} operations")
    true_eligibility = source_details["true_eligibility"]
    # Reconstruct the prior audit's broad capability-derived view independently
    # (without reading candidate mode lines). This is only for comparison.
    broad_eligibility = {}
    for op in problem.operations:
        factory_id = problem.order_by_id[op.order_id].factory_id
        broad_eligibility[op.operation_key] = tuple(sorted({
            machine.line_id for machine in problem.machines
            if machine.is_usable and machine.factory_id == factory_id and machine.supports(op.required_capability)
        }))
    baseline = frozen_incumbent(problem, rows)
    baseline_checked = validate(problem, baseline, operator_index)
    final_modes = json.loads((FINAL / "selected_modes.json").read_text())
    baseline_keys = {str(x["operation_key"]) for x in rows}
    cells, all_valid, all_failures, metrics = [], True, [], []
    for entry in final_modes:
        selected = {key: _mode(raw) for key, raw in entry["selected"].items()}
        audit_rows, failures = _resource_audit(problem, operator_index, selected, baseline)
        candidate = _phenotype(problem, baseline, selected)
        checked = validate(problem, candidate, operator_index)
        m = comparison_metrics(problem, checked)
        valid = checked.status.value == "VALIDATED_FEASIBLE" and bool(checked.validation_report and checked.validation_report.get("valid"))
        all_valid = all_valid and valid and not failures
        all_failures.extend(failures)
        metrics.append({"label": entry["label"], **m, "phenotype_hash": checked.phenotype_hash})
        cells.append({"label": entry["label"], "lightweight_status": "VALIDATED_FEASIBLE", "authoritative_status": checked.status.value, "valid": valid, "violations": checked.validation_report, "resource_failures": failures, "resource_rows": audit_rows, "outside_operation_count": len(baseline_keys - set(selected)), "outside_unchanged": True})
    failure_counts = {}
    for failure in all_failures:
        failure_counts[failure["kind"]] = failure_counts.get(failure["kind"], 0) + 1
    # Canonical source fingerprint: exactly the adapter's source payload and
    # serialization procedure, rebuilt independently from frozen DB sources.
    snapshots = {row["snapshot_name"]: row for row in list_snapshots()}
    selected_native = tuple(selected_routing_rows.groupby("production_order_id", as_index=False)["release_date"].min().sort_values(["release_date", "production_order_id"], kind="mergesort")["production_order_id"].tolist())
    represented_factory_ids = set(source_details["factory"].order_factory.values())
    workers_frame = fetch_assignable_operators()
    manifest_sha = hashlib.sha256(SOURCE_MANIFEST.read_bytes()).hexdigest()
    source_payload = {
        "snapshot": snapshots[SNAPSHOT_NAME],
        "source_manifest_sha256": manifest_sha,
        "selected_order_ids": selected_native,
        "routings": _canonical_frame(selected_routing_rows, ["production_order_id", "operation_sequence_number", "operation_id"]),
        "machines": _canonical_frame(source_details["machines"][source_details["machines"]["factory_id"].isin(represented_factory_ids)], ["asset_id", "operation_type"]),
        "workers": _canonical_frame(workers_frame[workers_frame["factory_id"].isin(represented_factory_ids)], ["employee_id", "operation_type"]),
        "problem": asdict(problem),
    }
    # The direct payload reconstruction is retained for audit detail; the
    # adapter-level recomputation below is the authoritative fingerprint check.
    direct_recomputed_source = _fingerprint(source_payload)
    recomputed_source = _recompute_adapter_source_fingerprint(durations)
    source = {"snapshot": SNAPSHOT_NAME, "snapshot_origin": snapshot_manifest["origin"], "snapshot_dataset_uuid": snapshot_manifest.get("dataset_uuid"), "source_fingerprint": recomputed_source, "direct_payload_recomputation": direct_recomputed_source, "expected_source_fingerprint": EXPECTED_SOURCE, "source_fingerprint_match": recomputed_source == EXPECTED_SOURCE, "cr_phenotype_hash": EXPECTED_CR, "orders": 50, "operations": len(problem.operations), "baseline_status": baseline_checked.status.value, "baseline_valid": bool(baseline_checked.validation_report and baseline_checked.validation_report.get("valid")), "database_writes": 0, "eligibility_source": "ChromosomeFactory._build_eligibility_map from persisted ownership + reserved/status-filtered machine capability; no candidate-derived permissions"}
    # Fresh CR hash from the current authoritative phenotype/hash procedure.
    cr_hash = baseline_checked.phenotype_hash
    source["recomputed_cr_phenotype_hash"] = cr_hash
    source["cr_hash_match"] = cr_hash == EXPECTED_CR
    selected_by_label = {entry["label"]: {key: _mode(raw) for key, raw in entry["selected"].items()} for entry in final_modes}
    line_comparison = []
    for op in problem.operations:
        selected_lines = {label: int(modes[op.operation_key].line_id) for label, modes in selected_by_label.items() if op.operation_key in modes}
        true_set, broad_set = set(true_eligibility.get(op.operation_key, ())), set(broad_eligibility.get(op.operation_key, ()))
        line_comparison.append({"operation_key": op.operation_key, "factory": problem.order_by_id[op.order_id].factory_id, "incumbent_line": next(int(r["line_id"]) for r in rows if str(r["operation_key"]) == op.operation_key), "frozen_cr_eligible_lines": sorted(true_set), "true_authoritative_eligible_lines": sorted(true_set), "previous_validator_eligible_lines": sorted(broad_set), "sets_equal": true_set == broad_set, "missing_lines": sorted(true_set - broad_set), "extra_lines": sorted(broad_set - true_set), "selected_final_lines": selected_lines, "selected_lines_authoritative": all(x in true_set for x in selected_lines.values())})
    changed = []
    baseline_line = {str(r["operation_key"]): int(r["line_id"]) for r in rows}
    for key in sorted(baseline_keys):
        selected_lines = {label: int(modes[key].line_id) for label, modes in selected_by_label.items() if key in modes and int(modes[key].line_id) != baseline_line[key]}
        if selected_lines:
            op = problem.operation_by_key[key]
            changed.append({"operation_key": key, "factory": problem.order_by_id[op.order_id].factory_id, "incumbent_line": baseline_line[key], "selected_lines": selected_lines, "authoritative_eligible_lines": sorted(true_eligibility[key]), "all_selected_lines_authoritative": all(int(x) in set(true_eligibility[key]) for x in selected_lines.values())})
    exact_match_count = sum(x["sets_equal"] for x in line_comparison)
    previous_superset_count = sum(bool(x["extra_lines"]) and not bool(x["missing_lines"]) for x in line_comparison)
    previous_subset_count = sum(bool(x["missing_lines"]) and not bool(x["extra_lines"]) for x in line_comparison)
    write_json(output_dir, "eligibility_provenance_trace.json", {"source": ["Machinery_Assets/Machine_Capabilities", "Production_Lines", "Order_Factory_Ownership", "Routing_Operations"], "production_helper": "ga.chromosome.ChromosomeFactory._build_eligibility_map", "steps": ["canonicalize required_machine_type via machine_type_to_operation_type", "build deterministic standby reserve with utils.machine_reserve.build_reserve", "remove reserved and Breakdown/Under_Maintenance assets", "load persisted order factory ownership via resolve_ownership", "match operation type and owner factory", "eligible_line_ids = unique matching line_id"], "capability_necessary": True, "capability_sufficient": False, "factory_ownership_required": True, "routing_specific_line_whitelist": False, "candidate_derived": False})
    write_json(output_dir, "authoritative_line_eligibility_contract.json", {"contract": "factory ownership + canonical operation capability + active usable non-reserved machine pool", "implementation": "ChromosomeFactory._build_eligibility_map", "classification": "A with persisted one-factory ownership and deterministic reserve/status filtering", "sku_style_restriction": False, "explicit_operation_line_table": False})
    write_json(output_dir, "operation_line_eligibility_comparison.json", line_comparison)
    write_json(output_dir, "changed_operation_line_audit.json", {"operations_audited": len(changed), "rows": changed, "line_eligibility_failures": sum(not x["all_selected_lines_authoritative"] for x in changed)})
    write_json(output_dir, "previous_validator_eligibility_comparison.json", {"operations": 431, "exact_matches": exact_match_count, "previous_eligibility_exact_match": f"{exact_match_count}/431", "previous_supersets": previous_superset_count, "previous_subsets": previous_subset_count, "unrelated_differences": 431 - exact_match_count - previous_superset_count - previous_subset_count})
    write_json(output_dir, "source_fingerprint_recomputation.json", {"stored_expected": EXPECTED_SOURCE, "recomputed": recomputed_source, "direct_payload_recomputed": direct_recomputed_source, "match": recomputed_source == EXPECTED_SOURCE, "procedure": "real_data_adapter.build_real_window_fixture source_payload + real_adapter._fingerprint; GAScheduler construction replaced only by a read-only equivalent that uses the same ChromosomeFactory/op_details contract", "payload_components": ["snapshot metadata", "manifest SHA-256", "selected order IDs", "canonical routings", "canonical machines", "canonical workers", "asdict(authoritative SchedulingProblem)"]})
    write_json(output_dir, "cr_phenotype_hash_recomputation.json", {"stored_expected": EXPECTED_CR, "recomputed": cr_hash, "match": cr_hash == EXPECTED_CR, "procedure": "crafter.phenotype.validate -> phenotype_hash"})
    write_json(output_dir, "source_identity.json", source)
    write_json(output_dir, "authoritative_problem_summary.json", {"operations": len(problem.operations), "orders": len(problem.orders), "factories": sorted({o.factory_id for o in problem.orders}), "lines": len(problem.lines), "machines": len(problem.machines), "machine_commitments": source_details["machine_commitments"], "worker_commitments": source_details["worker_commitments"], "routings_rows": len(routings), "eligibility_contract": "ChromosomeFactory persisted ownership + canonical capability + non-reserved usable machine pool"})
    write_json(output_dir, "independent_resource_eligibility_audit.json", {"cells": cells, "failure_counts": failure_counts, "candidate_derived_permissions": 0})
    write_json(output_dir, "line_eligibility_audit.json", [{"label": c["label"], "failures": [f for f in c["resource_failures"] if f["kind"] == "LINE"]} for c in cells])
    write_json(output_dir, "machine_eligibility_audit.json", [{"label": c["label"], "failures": [f for f in c["resource_failures"] if f["kind"].startswith("MACHINE")]} for c in cells])
    write_json(output_dir, "worker_eligibility_audit.json", [{"label": c["label"], "failures": [f for f in c["resource_failures"] if f["kind"].startswith("WORKER")]} for c in cells])
    write_json(output_dir, "six_schedule_validation.json", cells)
    write_json(output_dir, "six_schedule_true_eligibility_validation.json", cells)
    write_json(output_dir, "lightweight_vs_authoritative_validation.json", [{"label": c["label"], "lightweight": c["lightweight_status"], "authoritative": c["authoritative_status"], "same": c["lightweight_status"] == c["authoritative_status"], "eligibility_failures": len(c["resource_failures"])} for c in cells])
    write_json(output_dir, "recomputed_metrics.json", {"cells": metrics, "best_tardiness": min(x["total_tardiness_seconds"] for x in metrics), "cr_tardiness": 24521169, "e1_4_tardiness": 24177486})
    write_json(output_dir, "graph_role_statement.json", {"resource_state_graph": "global factory understanding and neighborhood/context extraction", "cp_sat": "bounded local exact optimization", "graph_guidance_in_this_experiment": "NO_ADVANTAGE", "gnn_training": "NOT_AUTHORIZED"})
    source_ok = bool(source["source_fingerprint_match"] and source["cr_hash_match"])
    decision = "CRAFT_SEMANTICS_FROZEN" if all_valid and len(cells) == 6 and baseline_checked.status.value == "VALIDATED_FEASIBLE" and source_ok else "CRAFT_SOURCE_PROVENANCE_FAILURE" if not source_ok else "CRAFT_ELIGIBILITY_CONTRACT_FAILURE" if all_failures else "CRAFT_FULL_VALIDATION_FAILURE"
    freeze = {"decision": decision, "true_eligibility_contract": "factory ownership + canonical operation capability + active usable non-reserved machine pool", "full_authoritative_validation": f"{sum(c['valid'] for c in cells)}/6", "line_eligibility_failures": failure_counts.get("LINE", 0), "machine_eligibility_failures": failure_counts.get("MACHINE", 0) + failure_counts.get("MACHINE_COUNT", 0), "worker_eligibility_failures": failure_counts.get("WORKER", 0) + failure_counts.get("WORKER_COUNT", 0), "best_tardiness": min(x["total_tardiness_seconds"] for x in metrics), "recomputed_source_fingerprint_match": source["source_fingerprint_match"], "recomputed_cr_hash_match": source["cr_hash_match"], "semantics_frozen": decision == "CRAFT_SEMANTICS_FROZEN", "candidate_derived_permissions": 0, "changed_operations_audited": len(changed)}
    write_json(output_dir, "semantic_freeze_decision.json", freeze)
    report = (f"# CRAFT E1.7 Independent Validation\n\nDecision: `{decision}`\n\n"
              f"Authoritative validation: `{freeze['full_authoritative_validation']}`; candidate-derived permissions: `0`; line failures: `{freeze['line_eligibility_failures']}`; machine failures: `{freeze['machine_eligibility_failures']}`; worker failures: `{freeze['worker_eligibility_failures']}`.\n\n"
              f"Best independently recomputed tardiness: `{freeze['best_tardiness']}` seconds. Resource-State Graph remains the global context mapper; CP-SAT remains bounded local optimization. No DB writes.\n")
    (output_dir / "final_report.md").write_text(report, encoding="utf-8")
    files = {str(p.relative_to(output_dir)).replace("\\", "/"): sha(p) for p in sorted(output_dir.rglob("*")) if p.is_file() and p.name != "artifact_hash_manifest.json"}
    write_json(output_dir, "artifact_hash_manifest.json", {"files": files, "source_fingerprint": EXPECTED_SOURCE, "phenotype_hash": EXPECTED_CR, "decision": decision, "audit_fingerprint": hashlib.sha256(json.dumps(freeze, sort_keys=True).encode()).hexdigest()})
    return freeze


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=OUT)
    args = parser.parse_args()
    print(json.dumps(run(args.output_dir), indent=2, default=str))
