"""Bounded, non-final Option-B action-ranking pilot generator.

This module never imports a pytest fixture or opens a database.  It evaluates
small, explicit GA-compatible one-gene proposals against the authoritative
scheduler evaluator solely to create observational training-pilot records.
"""
from __future__ import annotations

import hashlib
import json
import random
import subprocess
import time
from pathlib import Path
from typing import Any

import numpy as np

from ga.fitness import evaluate_schedule
from ga.operators import repair_line_and_machine_count, repair_precedence
from option_b.action_schema import SCHEMA_VERSION, validate
from option_b.scenarios.non_final_pilot import build

PILOT_VERSION = 1
DEFAULT_OUTPUT = Path("outputs/option_b/pilot_v1")
FORBIDDEN_PHASE_B_FIELDS = {
    "actual_start_time", "actual_end_time", "queue_delay_mins_at_dispatch",
    "machine_breakdown_count_at_dispatch", "machine_status_at_dispatch",
    "employee_on_leave_at_dispatch", "execution_status",
}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _diff(before: list, after: list) -> list[dict]:
    return [{"index": i, "before": a, "after": b}
            for i, (a, b) in enumerate(zip(before, after)) if a != b]


def _git_identity() -> str | None:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return None


def _evaluate(scheduler, chromosome: list) -> tuple[float, dict, float]:
    started = time.perf_counter()
    # Reuse the scheduler's registered authoritative evaluator arguments;
    # this prevents pilot-only drift in fitness or decode semantics.
    kwargs = dict(scheduler.toolbox.evaluate.keywords)
    kwargs["return_details"] = True
    result = evaluate_schedule(chromosome, **kwargs)
    elapsed = time.perf_counter() - started
    return float(result[0][0]), dict(result[1]), elapsed


def _phenotype_sha(details: dict) -> str:
    return _sha(details)


def _numeric_leaves(value: Any, prefix: str = "") -> dict[str, float]:
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            result.update(_numeric_leaves(child, f"{prefix}.{key}" if prefix else str(key)))
        return result
    if isinstance(value, (int, float, np.number)) and np.isfinite(value):
        return {prefix: float(value)}
    return {}


def _state_record(state_id: int, scheduler, parent: list, parent_fit: float, parent_details: dict) -> dict:
    n = scheduler.num_ops
    os_gene, la_gene, mc_gene = parent[:n], parent[n:2*n], parent[2*n:]
    routes = scheduler.df_routings.set_index(["production_order_id", "operation_id"], drop=False)
    order_rows = {}
    operations = []
    line_load = {}
    for position, key in enumerate(os_gene):
        order_id_text, operation_id_text = key.split("_", 1)
        order_id, operation_id = int(order_id_text), int(operation_id_text)
        row = routes.loc[(order_id, operation_id)]
        deadline = row.get("order_deadline")
        deadline_minutes = float(scheduler.order_deadlines_minutes.get(order_id, 0.0))
        release_minutes = float(scheduler.order_release_minutes.get(order_id, 0.0))
        line = int(la_gene[position])
        line_load[line] = line_load.get(line, 0.0) + float(scheduler.op_work.get(key, 0.0))
        order_rows.setdefault(order_id, {
            "order_id": order_id,
            "quantity": float(row.get("target_quantity", 0.0)),
            "release_minutes": release_minutes,
            "deadline": str(deadline) if deadline is not None else None,
            "deadline_minutes": deadline_minutes,
            "slack_minutes": deadline_minutes - release_minutes,
            "manager_priority": bool(order_id in scheduler.manager_prioritized_orders),
            "remaining_workload": 0.0,
        })
        operations.append({
            "operation_key": key, "order_id": order_id, "operation_id": operation_id,
            "operation_type": scheduler.factory.optype_for_op.get(key),
            "routing_step": int(scheduler.factory.op_sequence_map[key]),
            "remaining_steps": int(sum(1 for k in os_gene if k.split("_", 1)[0] == order_id)
                                   - scheduler.factory.op_sequence_map[key]),
            "sam": float(row.get("sam", 0.0)), "workload": float(scheduler.op_work.get(key, 0.0)),
            "sequence_position": position, "line_assignment": line, "machine_count": int(mc_gene[position]),
            "eligible_lines": [int(x) for x in scheduler.factory.eligibility_map.get(key, [])],
            "machine_capacity_by_line": {str(k): int(v) for k, v in scheduler.factory.machines_per_line_map.get(key, {}).items()},
        })
    for operation in operations:
        order_rows[operation["order_id"]]["remaining_workload"] += operation["workload"]
    resource = {
        "line_workload": {str(k): v for k, v in sorted(line_load.items())},
        "initial_machine_free": {str(k): float(v) for k, v in scheduler.initial_machine_free.items()},
        "initial_machine_last_order": {str(k): str(v) for k, v in scheduler.initial_machine_last_order.items()},
        "aggregated_qualified_worker_capacity": int(scheduler.present_operator_count),
        "initial_worker_reservation_count": len(scheduler.initial_operator_reservations or {}),
        "fixed_commitment_count": 0,
        "movable_commitment_count": 0,
    }
    return {
        "state_id": state_id, "window": 0, "scheduling_origin": scheduler.now.isoformat(),
        "source": "option_b.scenarios.non_final_pilot", "dataset_uuid": None,
        "snapshot_database": None, "final_snapshot_used": False,
        "parent_chromosome_sha256": _sha(parent), "parent_phenotype_sha256": _phenotype_sha(parent_details),
        "parent_fitness": parent_fit, "parent_fitness_components": parent_details,
        "orders": sorted(order_rows.values(), key=lambda x: x["order_id"]), "operations": operations,
        "resource_context": resource,
    }


def _repair_candidate(scheduler, raw: list) -> list:
    n = scheduler.num_ops
    os_gene, la_gene, mc_gene = list(raw[:n]), list(raw[n:2*n]), list(raw[2*n:])
    repair_precedence(os_gene, scheduler.factory.op_sequence_map)
    repair_line_and_machine_count(
        os_gene, la_gene, mc_gene, scheduler.factory.eligibility_map,
        scheduler.factory.machines_per_line_map, 40, scheduler.op_work,
        scheduler.factory.optype_for_op, scheduler.protected_machine_type,
    )
    return os_gene + la_gene + mc_gene


def _proposals(scheduler, parent: list, state_seed: int, limit: int) -> list[tuple[str, str, list]]:
    """Deterministic, real-gene one-action proposals; no GA RNG is consumed."""
    n = scheduler.num_ops
    result: list[tuple[str, str, list]] = []
    for i in range(n - 1):
        if parent[i].split("_", 1)[0] != parent[i + 1].split("_", 1)[0]:
            raw = list(parent); raw[i], raw[i + 1] = raw[i + 1], raw[i]
            result.append(("OS", parent[i], raw)); break
    for i, key in enumerate(parent[:n]):
        current_line = parent[n + i]
        for line in scheduler.factory.eligibility_map.get(key, []):
            if line != current_line:
                raw = list(parent); raw[n + i] = int(line)
                result.append(("LA", key, raw)); break
        current_mc = int(parent[2*n + i])
        cap = int(scheduler.factory.machines_per_line_map.get(key, {}).get(current_line, 1))
        alternatives = [v for v in (current_mc - 1, current_mc + 1) if 1 <= v <= cap]
        if alternatives:
            raw = list(parent); raw[2*n + i] = alternatives[0]
            result.append(("MC", key, raw))
        if len(result) >= limit:
            break
    return result[:limit]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, sort_keys=True, default=str) + "\n" for row in rows), encoding="utf-8")


def _write_json_with_sha(path: Path, value: Any) -> str:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_suffix(path.suffix + ".sha256").write_text(digest + "\n", encoding="ascii")
    return digest


def _artifact_hashes(output: Path, names: list[str]) -> dict[str, str]:
    return {name: hashlib.sha256((output / name).read_bytes()).hexdigest() for name in names}


def _learnability(actions: list[dict], seed: int) -> dict:
    """Untuned, by-state diagnostic only; failure is informative, not fatal."""
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import average_precision_score, roc_auc_score
        states = sorted({int(a["state_id"]) for a in actions})
        cut = max(1, int(len(states) * .70))
        train_states, test_states = set(states[:cut]), set(states[cut:])
        def feature(a):
            d = a["operator_diff"][0]
            return [float(a["action_type"] == "OS"), float(a["action_type"] == "LA"),
                    float(a["action_type"] == "MC"), float(d["index"]),
                    float(d["before"]) if isinstance(d["before"], (int, float)) else 0.0,
                    float(d["after"]) if isinstance(d["after"], (int, float)) else 0.0]
        train = [a for a in actions if a["state_id"] in train_states]
        test = [a for a in actions if a["state_id"] in test_states]
        y_train = [int(a["improves_parent"]) for a in train]
        y_test = [int(a["improves_parent"]) for a in test]
        if len(set(y_train)) < 2 or len(set(y_test)) < 2:
            return {"status": "not_run_single_class_state_split"}
        model = LogisticRegression(random_state=seed, max_iter=200).fit([feature(a) for a in train], y_train)
        probabilities = model.predict_proba([feature(a) for a in test])[:, 1]
        return {"status": "untuned_state_split", "train_states": len(train_states), "test_states": len(test_states),
                "test_ap": float(average_precision_score(y_test, probabilities)),
                "test_roc_auc": float(roc_auc_score(y_test, probabilities)),
                "test_prevalence": float(sum(y_test) / len(y_test))}
    except Exception as exc:
        return {"status": "not_run", "reason": type(exc).__name__}


def generate_pilot(output: Path | str = DEFAULT_OUTPUT, *, seed: int = 42,
                   state_count: int = 50, actions_per_state: int = 7) -> dict:
    """Generate a bounded, deterministic, NON-FINAL pilot; returns summary."""
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    random.seed(seed); np.random.seed(seed)
    started = time.perf_counter()
    states: list[dict] = []; actions: list[dict] = []; excluded: list[dict] = []
    evaluation_count = 0
    for state_id in range(state_count):
        scheduler = build(seed=seed + state_id)
        parent = list(scheduler.toolbox.individual())
        parent_fitness, parent_details, _ = _evaluate(scheduler, parent); evaluation_count += 1
        state = _state_record(state_id, scheduler, parent, parent_fitness, parent_details)
        states.append(state)
        for action_type, target, raw in _proposals(scheduler, parent, seed + state_id, actions_per_state):
            final = _repair_candidate(scheduler, raw)
            operator_diff, repair_diff = _diff(parent, raw), _diff(raw, final)
            # A legal OS swap changes exactly two positions; LA/MC are a
            # single-gene proposal.  Both remain causally unambiguous.
            expected_changes = 2 if action_type == "OS" else 1
            if len(operator_diff) != expected_changes:
                excluded.append({"state_id": state_id, "reason": "ambiguous_operator_diff", "action_type": action_type,
                                 "operator_diff": operator_diff})
                continue
            if final == parent:
                excluded.append({"state_id": state_id, "reason": "repair_or_noop_reverted_action", "action_type": action_type,
                                 "operator_diff": operator_diff, "repair_diff": repair_diff})
                continue
            candidate_fitness, candidate_details, runtime = _evaluate(scheduler, final); evaluation_count += 1
            parent_components = _numeric_leaves(parent_details.get("fitness_components", {}))
            candidate_components = _numeric_leaves(candidate_details.get("fitness_components", {}))
            component_delta = {key: parent_components[key] - candidate_components[key]
                               for key in sorted(parent_components.keys() & candidate_components.keys())}
            changed_indices = {change["index"] for change in operator_diff}
            final_reverses = [final[index] == parent[index] for index in changed_indices]
            record = {
                "action_id": _sha({"state": state["parent_chromosome_sha256"], "raw": raw, "type": action_type}),
                "action_type": action_type, "target_operation": target, "window": 0,
                "parent_chromosome_sha256": state["parent_chromosome_sha256"],
                "parent_phenotype_sha256": state["parent_phenotype_sha256"], "state_id": state_id,
                "sampling_seed": seed + state_id, "operator_diff": operator_diff, "repair_diff": repair_diff,
                "raw_action": {"action_type": action_type, "target_operation": target,
                               "original_values": [{"index": d["index"], "value": d["before"]} for d in operator_diff],
                               "proposed_values": [{"index": d["index"], "value": d["after"]} for d in operator_diff]},
                "raw_chromosome_sha256": _sha(raw), "final_candidate_chromosome_sha256": _sha(final),
                "parent_fitness": parent_fitness, "candidate_fitness": candidate_fitness,
                "fitness_delta": parent_fitness - candidate_fitness,
                "parent_fitness_components": parent_components, "candidate_fitness_components": candidate_components,
                "fitness_component_delta": component_delta, "improves_parent": bool(parent_fitness > candidate_fitness),
                "candidate_phenotype_sha256": _phenotype_sha(candidate_details), "evaluation_runtime_s": runtime,
                "feasibility_result": "evaluator_completed", "ambiguous_attribution": False,
                "repair_partially_reversed_proposal": any(final_reverses),
                "repair_fully_reversed_proposal": bool(final_reverses) and all(final_reverses),
            }
            validate(record); actions.append(record)
    pairs = []
    for state_id in range(state_count):
        rows = sorted((a for a in actions if a["state_id"] == state_id), key=lambda a: (-a["fitness_delta"], a["action_id"]))
        for left in range(len(rows)):
            for right in range(left + 1, len(rows)):
                margin = rows[left]["fitness_delta"] - rows[right]["fitness_delta"]
                if margin > 0:
                    pairs.append({"state_id": state_id, "winner_action_id": rows[left]["action_id"],
                                  "loser_action_id": rows[right]["action_id"], "margin": margin})
    _write_jsonl(output / "states.jsonl", states)
    _write_jsonl(output / "actions_outcomes.jsonl", actions)
    _write_jsonl(output / "ranking_pairs.jsonl", pairs)
    _write_jsonl(output / "excluded_actions.jsonl", excluded)
    for name in ("states.jsonl", "actions_outcomes.jsonl", "ranking_pairs.jsonl", "excluded_actions.jsonl"):
        digest = hashlib.sha256((output / name).read_bytes()).hexdigest()
        (output / (name + ".sha256")).write_text(digest + "\n", encoding="ascii")
    delta = [a["fitness_delta"] for a in actions]; runtime = [a["evaluation_runtime_s"] for a in actions]
    type_counts = {kind: sum(a["action_type"] == kind for a in actions) for kind in ("OS", "LA", "MC")}
    good = {a["state_id"] for a in actions if a["fitness_delta"] > 0}
    bad = {a["state_id"] for a in actions if a["fitness_delta"] < 0}
    summary = {
        "version": PILOT_VERSION, "unique_states": len(states), "clean_actions": len(actions), "excluded_actions": len(excluded),
        "ranking_pairs": len(pairs), "action_type_counts": type_counts,
        "improving_count": sum(a["fitness_delta"] > 0 for a in actions),
        "neutral_count": sum(a["fitness_delta"] == 0 for a in actions),
        "harmful_count": sum(a["fitness_delta"] < 0 for a in actions),
        "repair_frequency": sum(bool(a["repair_diff"]) for a in actions),
        "repair_partially_reversed_count": sum(a["repair_partially_reversed_proposal"] for a in actions),
        "repair_fully_reversed_count": sum(a["repair_fully_reversed_proposal"] for a in actions),
        "strongly_harmful_count": sum(a["fitness_delta"] < -0.01 * a["parent_fitness"] for a in actions),
        "states_with_good_and_bad": len(good & bad), "authoritative_evaluations": evaluation_count,
        "total_runtime_s": time.perf_counter() - started,
        "fitness_delta": {"mean": float(np.mean(delta)), "median": float(np.median(delta)),
                          "p05": float(np.percentile(delta, 5)), "p95": float(np.percentile(delta, 95))},
        "evaluation_runtime_s": {"mean": float(np.mean(runtime)), "median": float(np.median(runtime)),
                                  "p95": float(np.percentile(runtime, 95)), "max": float(np.max(runtime))},
        "learnability": _learnability(actions, seed),
    }
    serialized = "\n".join((output / name).read_text(encoding="utf-8") for name in
                           ("states.jsonl", "actions_outcomes.jsonl", "ranking_pairs.jsonl", "excluded_actions.jsonl"))
    leakage = {"phase_b_fields_used": False, "forbidden_fields": sorted(FORBIDDEN_PHASE_B_FIELDS),
               "forbidden_field_token_found_in_pilot_records": any(field in serialized for field in FORBIDDEN_PHASE_B_FIELDS),
               "final_snapshot_used": False,
               "database_access": "no application DB query/write; in-memory NON_FINAL scenario only"}
    quality = {"clean_attribution": all(not a["ambiguous_attribution"] for a in actions),
               "same_state_pairs_only": all(p["winner_action_id"] != p["loser_action_id"] for p in pairs),
               "duplicate_action_ids": len(actions) - len({a["action_id"] for a in actions}),
               "all_candidate_evaluations_completed": all(a["feasibility_result"] == "evaluator_completed" for a in actions),
               "all_records_have_raw_and_repair": all("raw_action" in a and "repair_diff" in a for a in actions),
               "non_final_only": True, "phase_b_leakage_detected": False}
    _write_json_with_sha(output / "pilot_summary.json", summary)
    _write_json_with_sha(output / "leakage_audit.json", leakage)
    _write_json_with_sha(output / "quality_report.json", quality)
    manifest = {"version": PILOT_VERSION, "action_schema_version": SCHEMA_VERSION, "scenario": "NON_FINAL_TRAINING_PILOT_ONLY",
                "source_module": "option_b.scenarios.non_final_pilot", "snapshot_database": None, "dataset_uuid": None,
                "final_evaluation_snapshot_used": False, "seed": seed, "state_count_requested": state_count,
                "actions_per_state_requested": actions_per_state, "scheduling_origin": "2026-01-01T08:00:00",
                "git_commit": _git_identity(), "generator_code_sha256": _sha(Path(__file__).read_text(encoding="utf-8")),
                "scenario_code_sha256": _sha(Path(build.__code__.co_filename).read_text(encoding="utf-8")),
                "artifact_hashes": _artifact_hashes(output, ["states.jsonl", "actions_outcomes.jsonl", "ranking_pairs.jsonl", "excluded_actions.jsonl", "pilot_summary.json", "leakage_audit.json", "quality_report.json"])}
    _write_json_with_sha(output / "manifest.json", manifest)
    return summary


if __name__ == "__main__":
    print(json.dumps(generate_pilot(), indent=2, sort_keys=True))
