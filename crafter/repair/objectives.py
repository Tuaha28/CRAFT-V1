"""Versioned, lexicographic business objectives for canonical CRAFT repair.

This module deliberately owns no scheduling semantics.  It only builds exact
integer expressions over the canonical configuration/regime CP model.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Mapping

from ortools.sat.python import cp_model

from crafter.authoritative_semantics import project_instant_seconds


OBJECTIVE_CONTRACT_VERSION = "CRAFT_OBJECTIVE_CONTRACT_V1"
PRIORITY_TIERS = ("NORMAL", "HIGH", "URGENT")


class ManagerPriorityInputError(ValueError):
    """An active manager override lacks the information needed for service."""


@dataclass(frozen=True)
class ManagerPriorityOverrideV1:
    override_id: str
    order_id: str
    tier: str
    effective_at: int
    expires_at: int | None = None
    target_due_at: int | None = None
    reason_code: str = "UNSPECIFIED"
    entered_by: str = "UNKNOWN"
    entered_at: int = 0
    cleared_by: str | None = None
    cleared_at: int | None = None
    source_event_id: str | None = None
    revision: int = 1


def _seconds(value: Any, origin: datetime | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if origin is None:
            raise ManagerPriorityInputError("datetime manager timestamp requires problem origin")
        return int(round((value - origin).total_seconds()))
    return int(value)


def _coerce_override(value: ManagerPriorityOverrideV1 | Mapping[str, Any], origin: datetime) -> ManagerPriorityOverrideV1:
    if isinstance(value, ManagerPriorityOverrideV1):
        raw = asdict(value)
    elif isinstance(value, Mapping):
        raw = dict(value)
    else:
        raise TypeError("manager override must be ManagerPriorityOverrideV1 or mapping")
    tier = str(raw.get("tier", "")).upper()
    if tier not in PRIORITY_TIERS:
        raise ManagerPriorityInputError(f"invalid manager priority tier {tier!r}")
    required = ("override_id", "order_id", "effective_at", "reason_code", "entered_by", "entered_at", "revision")
    missing = [name for name in required if raw.get(name) is None]
    if missing:
        raise ManagerPriorityInputError(f"manager override missing {', '.join(missing)}")
    return ManagerPriorityOverrideV1(
        override_id=str(raw["override_id"]), order_id=str(raw["order_id"]), tier=tier,
        effective_at=_seconds(raw["effective_at"], origin) or 0,
        expires_at=_seconds(raw.get("expires_at"), origin),
        target_due_at=_seconds(raw.get("target_due_at"), origin),
        reason_code=str(raw["reason_code"]), entered_by=str(raw["entered_by"]),
        entered_at=_seconds(raw["entered_at"], origin) or 0,
        cleared_by=None if raw.get("cleared_by") is None else str(raw.get("cleared_by")),
        cleared_at=_seconds(raw.get("cleared_at"), origin),
        source_event_id=None if raw.get("source_event_id") is None else str(raw.get("source_event_id")),
        revision=int(raw["revision"]),
    )


def active_manager_overrides(overrides, *, event_time_seconds: int, origin: datetime) -> dict[str, ManagerPriorityOverrideV1]:
    """Return one deterministically latest active revision per order.

    A clear or expiry at/before the event instant is inactive.  Later revisions
    win; ties are deterministically resolved by entered time then override ID.
    """
    candidates: dict[str, list[ManagerPriorityOverrideV1]] = {}
    for value in overrides or ():
        item = _coerce_override(value, origin)
        now = int(event_time_seconds)
        active = item.effective_at <= now
        active = active and (item.expires_at is None or now < item.expires_at)
        active = active and not (item.cleared_at is not None and item.cleared_at <= now)
        if active:
            candidates.setdefault(item.order_id, []).append(item)
    return {
        order_id: max(rows, key=lambda x: (int(x.revision), int(x.entered_at), str(x.override_id)))
        for order_id, rows in candidates.items()
    }


def _baseline_assignment(state, key):
    return next((x.assignment for x in state["baseline"].operations if x.assignment.operation_key == key), None)


def _baseline_seconds(value) -> int:
    return int(project_instant_seconds(float(value)).projected_seconds)


def build_objective_context(state, manager_overrides=()):
    """Resolve due dates/tier membership without modifying canonical state."""
    problem = state["problem"]
    event = state.get("event")
    event_time = int(getattr(event, "event_time_seconds", 0) or 0)
    active = active_manager_overrides(manager_overrides, event_time_seconds=event_time, origin=problem.origin)
    order_ops: dict[str, list[Any]] = {}
    for op in problem.operations:
        order_ops.setdefault(str(op.order_id), []).append(op)
    region_orders = {str(problem.operation_by_key[key].order_id) for key in state["keys"]}
    rows = []
    for order_id in sorted(region_orders | set(active)):
        if order_id not in problem.order_by_id:
            raise ManagerPriorityInputError(f"manager override references unknown order {order_id}")
        order = problem.order_by_id[order_id]
        final = max(order_ops[order_id], key=lambda op: int(op.sequence)).operation_key
        override = active.get(order_id)
        tier = override.tier if override else "NORMAL"
        customer_due = order.deadline_seconds
        manager_target = override.target_due_at if override else None
        effective_due = manager_target if manager_target is not None else customer_due
        if tier in {"HIGH", "URGENT"} and effective_due is None:
            raise ManagerPriorityInputError(
                f"MANAGER_INPUT_INCOMPLETE: {tier} order {order_id} has neither manager target nor customer deadline")
        assignment = _baseline_assignment(state, final)
        baseline_completion = _baseline_seconds(assignment.end_seconds) if assignment is not None else None
        rows.append({
            "order_id": order_id,
            "tier": tier,
            "customer_deadline_seconds": customer_due,
            "manager_target_seconds": manager_target,
            "effective_due_seconds": effective_due,
            "final_operation_key": final,
            "completion_movable": final in set(state["keys"]),
            "baseline_completion_seconds": baseline_completion,
            "active_override": asdict(override) if override else None,
        })
    return {
        "objective_contract_version": OBJECTIVE_CONTRACT_VERSION,
        "event_time_seconds": event_time,
        "active_manager_overrides": {key: asdict(value) for key, value in active.items()},
        "orders": rows,
    }


def _predicate_literal(model, literals, name):
    same = model.NewBoolVar(name)
    if literals:
        model.Add(sum(literals) == same)
    else:
        model.Add(same == 0)
    return same


def add_objective_variables(state, artifact, context):
    """Attach exact objective variables to an already canonical CP model."""
    model = artifact["model"]
    horizon = int(state["problem"].horizon_seconds)
    tiers = {tier: {"late": [], "tardy": [], "records": []} for tier in PRIORITY_TIERS}
    completion_records = []
    for row in context["orders"]:
        due = row["effective_due_seconds"]
        if due is None:
            completion_records.append({**row, "objective_included": False, "reason": "NO_EFFECTIVE_DUE"})
            continue
        final = row["final_operation_key"]
        completion = artifact["ends"].get(final)
        dynamic = completion is not None
        if completion is None:
            completion = int(row["baseline_completion_seconds"] or 0)
        tardy = model.NewIntVar(0, horizon, f"obj_tardy_{row['order_id']}")
        model.AddMaxEquality(tardy, [completion - int(due), 0])
        late = model.NewBoolVar(f"obj_late_{row['order_id']}")
        model.Add(tardy >= 1).OnlyEnforceIf(late)
        model.Add(tardy == 0).OnlyEnforceIf(late.Not())
        tiers[row["tier"]]["late"].append(late)
        tiers[row["tier"]]["tardy"].append(tardy)
        tiers[row["tier"]]["records"].append(row["order_id"])
        completion_records.append({**row, "objective_included": True, "completion_dynamic": dynamic,
                                   "completion_expression": "CP_END" if dynamic else "FROZEN_BASELINE_CONSTANT"})
    expressions = []
    for tier in ("URGENT", "HIGH", "NORMAL"):
        group = tiers[tier]
        late_expr = sum(group["late"]) if group["late"] else 0
        tardy_expr = sum(group["tardy"]) if group["tardy"] else 0
        if group["tardy"]:
            maximum = model.NewIntVar(0, horizon, f"obj_max_tardy_{tier}")
            model.AddMaxEquality(maximum, group["tardy"])
        else:
            maximum = 0
        expressions.extend([
            {"name": f"late_order_count_{tier}", "level": f"DELIVERY_{tier}", "expression": late_expr, "orders": tuple(group["records"]), "unit": "orders"},
            {"name": f"total_tardiness_seconds_{tier}", "level": f"DELIVERY_{tier}", "expression": tardy_expr, "orders": tuple(group["records"]), "unit": "seconds"},
            {"name": f"maximum_tardiness_seconds_{tier}", "level": f"DELIVERY_{tier}", "expression": maximum, "orders": tuple(group["records"]), "unit": "seconds"},
        ])

    churn = {name: [] for name in ("changed_operations", "line_changes", "crew_changes", "machine_changes", "k_changes", "late_start_shift_seconds", "absolute_start_shift_seconds")}
    churn_records = []
    stability_applicable = state.get("objective_profile") != "INITIAL_PLAN_NO_STABILITY"
    movable = ({key for key in state["keys"] if state.get("classified", {}).get(key, {}).get("state") in {"FUTURE_RELEASED", "FUTURE_PLANNED", "FUTURE", "BLOCKED", "MOVABLE_WINDOW"}}
               if stability_applicable else set())
    for key in sorted(movable):
        assignment = _baseline_assignment(state, key)
        if assignment is None:
            continue
        start0 = _baseline_seconds(assignment.start_seconds)
        machines0 = tuple(sorted(int(x) for x in assignment.machine_ids))
        workers0 = tuple(sorted(str(x) for x in next(x.employee_ids for x in state["baseline"].operations if x.assignment.operation_key == key)))
        line0, k0 = int(assignment.line_id), int(assignment.machine_count)
        cfgs = artifact["cfg_lit"][key]
        same_line = _predicate_literal(model, [lit for cfg, lit in cfgs.items() if int(cfg.line_id) == line0], f"obj_same_line_{key}")
        same_crew = _predicate_literal(model, [lit for cfg, lit in cfgs.items() if tuple(sorted(str(x) for x in cfg.worker_ids)) == workers0], f"obj_same_crew_{key}")
        same_machine = _predicate_literal(model, [lit for cfg, lit in cfgs.items() if tuple(sorted(int(x) for x in cfg.machine_ids)) == machines0], f"obj_same_machine_{key}")
        same_k = _predicate_literal(model, [lit for cfg, lit in cfgs.items() if int(cfg.k) == k0], f"obj_same_k_{key}")
        same_cfg = _predicate_literal(model, [lit for cfg, lit in cfgs.items() if int(cfg.line_id) == line0 and int(cfg.k) == k0 and tuple(sorted(int(x) for x in cfg.machine_ids)) == machines0 and tuple(sorted(str(x) for x in cfg.worker_ids)) == workers0], f"obj_same_cfg_{key}")
        same_start = model.NewBoolVar(f"obj_same_start_{key}")
        model.Add(artifact["starts"][key] == start0).OnlyEnforceIf(same_start)
        model.Add(artifact["starts"][key] != start0).OnlyEnforceIf(same_start.Not())
        changed = model.NewBoolVar(f"obj_changed_{key}")
        model.AddBoolAnd([same_start, same_cfg]).OnlyEnforceIf(changed.Not())
        model.AddBoolOr([same_start.Not(), same_cfg.Not()]).OnlyEnforceIf(changed)
        diff = model.NewIntVar(-horizon, horizon, f"obj_shift_diff_{key}")
        model.Add(diff == artifact["starts"][key] - start0)
        absolute = model.NewIntVar(0, horizon, f"obj_abs_shift_{key}")
        late = model.NewIntVar(0, horizon, f"obj_late_shift_{key}")
        model.AddAbsEquality(absolute, diff)
        model.AddMaxEquality(late, [diff, 0])
        for field, literal in (("line_changes", same_line), ("crew_changes", same_crew), ("machine_changes", same_machine), ("k_changes", same_k)):
            changed_field = model.NewBoolVar(f"obj_{field}_{key}")
            model.Add(changed_field + literal == 1)
            churn[field].append(changed_field)
        churn["changed_operations"].append(changed)
        churn["late_start_shift_seconds"].append(late)
        churn["absolute_start_shift_seconds"].append(absolute)
        churn_records.append({"operation_key": key, "incumbent_start_seconds": start0, "incumbent_line_id": line0,
                              "incumbent_k": k0, "incumbent_machine_ids": machines0, "incumbent_worker_ids": workers0})
    if stability_applicable:
        for name in ("changed_operations", "line_changes", "crew_changes", "machine_changes", "k_changes", "late_start_shift_seconds", "absolute_start_shift_seconds"):
            expressions.append({"name": name, "level": "STABILITY", "expression": sum(churn[name]) if churn[name] else 0,
                                "orders": (), "unit": "operations" if name.endswith("changes") or name == "changed_operations" else "seconds"})
    if artifact["ends"]:
        makespan = model.NewIntVar(0, horizon, "obj_regional_makespan")
        model.AddMaxEquality(makespan, list(artifact["ends"].values()))
    else:
        makespan = 0
    expressions.append({"name": "regional_makespan_seconds", "level": "SECONDARY", "expression": makespan, "orders": (), "unit": "seconds"})
    return {"context": context, "expressions": expressions, "order_records": completion_records,
            "churn_records": churn_records,
            "stability_status": "APPLICABLE" if stability_applicable else "NOT_APPLICABLE_NO_PREVIOUS_PLAN",
            "utilization": {"status": "REPORTING_ONLY_V1", "avoidable_feasible_idle": "NOT_OPTIMIZED"}}


def expression_is_constant(expression) -> bool:
    return isinstance(expression, int)


def baseline_objective_metrics(state, context):
    """Exact baseline tier outcomes for manager impact preview provenance."""
    tiers = {tier: {"late_order_count": 0, "total_tardiness_seconds": 0, "maximum_tardiness_seconds": 0} for tier in PRIORITY_TIERS}
    rows = []
    for item in context["orders"]:
        due, end = item["effective_due_seconds"], item["baseline_completion_seconds"]
        tardy = max(0, int(end or 0) - int(due)) if due is not None and end is not None else None
        if tardy is not None:
            entry = tiers[item["tier"]]
            entry["late_order_count"] += int(tardy > 0)
            entry["total_tardiness_seconds"] += tardy
            entry["maximum_tardiness_seconds"] = max(entry["maximum_tardiness_seconds"], tardy)
        rows.append({**item, "baseline_tardiness_seconds": tardy})
    return {"tiers": tiers, "orders": rows}


def selected_objective_metrics(state, context, selected):
    """Reconstruct objective/reporting metrics from the exact CP phenotype."""
    by_key = {row["operation_key"]: row for row in selected}
    tiers = {tier: {"late_order_count": 0, "total_tardiness_seconds": 0,
                    "maximum_tardiness_seconds": 0} for tier in PRIORITY_TIERS}
    order_rows = []
    for item in context["orders"]:
        selected_final = by_key.get(item["final_operation_key"])
        completion = int(selected_final["end_seconds"]) if selected_final else item["baseline_completion_seconds"]
        due = item["effective_due_seconds"]
        tardy = max(0, int(completion) - int(due)) if due is not None and completion is not None else None
        if tardy is not None:
            entry = tiers[item["tier"]]
            entry["late_order_count"] += int(tardy > 0)
            entry["total_tardiness_seconds"] += tardy
            entry["maximum_tardiness_seconds"] = max(entry["maximum_tardiness_seconds"], tardy)
        order_rows.append({**item, "completion_seconds": completion, "tardiness_seconds": tardy,
                           "completion_source": "CP_SELECTED_FINAL_OPERATION" if selected_final else "FROZEN_BASELINE_FINAL_OPERATION"})
    baseline = {x.assignment.operation_key: x for x in state["baseline"].operations}
    churn = {"changed_operations": 0, "line_changes": 0, "crew_changes": 0, "machine_changes": 0,
             "k_changes": 0, "late_start_shift_seconds": 0, "absolute_start_shift_seconds": 0}
    missing_baseline = []
    for row in selected:
        item = baseline.get(row["operation_key"])
        if item is None:
            missing_baseline.append(row["operation_key"])
            continue
        cfg = row["resource_configuration"]
        old_start = _baseline_seconds(item.assignment.start_seconds)
        old_workers = tuple(sorted(str(x) for x in item.employee_ids))
        old_machines = tuple(sorted(int(x) for x in item.assignment.machine_ids))
        line_changed = int(int(cfg["line_id"]) != int(item.assignment.line_id))
        crew_changed = int(tuple(sorted(str(x) for x in cfg["worker_ids"])) != old_workers)
        machine_changed = int(tuple(sorted(int(x) for x in cfg["machine_ids"])) != old_machines)
        k_changed = int(int(cfg["k"]) != int(item.assignment.machine_count))
        shift = int(row["start_seconds"]) - old_start
        changed = int(line_changed or crew_changed or machine_changed or k_changed or shift != 0)
        churn["changed_operations"] += changed; churn["line_changes"] += line_changed
        churn["crew_changes"] += crew_changed; churn["machine_changes"] += machine_changed
        churn["k_changes"] += k_changed; churn["late_start_shift_seconds"] += max(0, shift)
        churn["absolute_start_shift_seconds"] += abs(shift)
    churn["status"] = ("NOT_APPLICABLE_NO_PREVIOUS_PLAN" if not baseline else
                       "PARTIAL_BASELINE_COVERAGE" if missing_baseline else "COMPARED_TO_BASELINE")
    churn["uncompared_operation_keys"] = sorted(missing_baseline)
    worker = {}
    for row in selected:
        worker_ids = [str(x) for x in row["resource_configuration"]["worker_ids"]]
        for wid in worker_ids:
            entry = worker.setdefault(wid, {"productive_seconds": 0, "selected_pause_seconds": 0,
                                              "immutable_reservation_seconds": 0,
                                              "available_worker_seconds": None,
                                              "calendar_unavailable_seconds": None,
                                              "idle_without_feasible_ready_work_seconds": None,
                                              "avoidable_feasible_idle_seconds": None})
            entry["productive_seconds"] += sum(int(b) - int(a) for a, b in row["worker_segments"])
            cfg = row["resource_configuration"]
            entry["selected_pause_seconds"] += max(0, int(row["end_seconds"]) - int(row["start_seconds"])
                                                  - int(cfg["setup_seconds"]) - int(cfg["productive_seconds"]))
    # The current authoritative calendar API constructs exact executable
    # segments, but does not expose a standalone inverse calendar interval set.
    # Do not fabricate free/avoidable time: persist exact productive, pause and
    # immutable-reservation facts and mark the counterfactual categories absent.
    for wid, rows in state.get("_worker_res", {}).items():
        if str(wid) in worker:
            worker[str(wid)]["immutable_reservation_seconds"] = sum(
                max(0, int(round((b-a).total_seconds()))) for a, b in rows)
    return {"tiers": tiers, "orders": order_rows, "churn": churn,
            "regional_makespan_seconds": max((int(x["end_seconds"]) for x in selected), default=0),
            "workforce_utilization": {"status": "REPORTING_ONLY_V1_EXACT_PARTIAL",
                                        "workers": worker,
                                        "note": "No raw free-time proxy is used; availability/avoidable idle await an exact inverse-calendar and jointly-feasible-ready-work proof."}}
