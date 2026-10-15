"""Isolated two-order priority proof through the unchanged canonical CRAFT solver.

Both orders contend for one machine and one skilled worker. Order A has an
executed historical lot and two remaining route steps; order B has one
remaining step. All times, quantities and resource holds are synthetic TEST
evidence, never observations about garment_fyp_db.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
from types import SimpleNamespace

from crafter.contracts import LineSpec, MachineSpec, OperationSpec, OrderSpec, SchedulingProblem
from crafter.repair.calendar_regimes import generate_calendar_regimes
from crafter.repair.resource_configs import ResourceConfiguration
from crafter.repair.solve import run_canonical_schedule
from web_v1.backend.integrations.craft.contracts import digest
from web_v1.backend.integrations.craft.residual_execution import normalize_problem, validate_residual_output


ORIGIN = datetime(2026, 9, 10, 6)
KEYS = ("A_residual", "A_finish", "B_residual")


def evidence(operation: OperationSpec, completed: int, remaining: int) -> dict:
    return {
        "operation_key": operation.operation_key, "order_id": operation.order_id,
        "factory_id": 1, "as_of": ORIGIN.isoformat(), "event_time": ORIGIN.isoformat(),
        "captured_at": ORIGIN.isoformat(), "available_at": ORIGIN.isoformat(),
        "valid_until": (ORIGIN + timedelta(days=1)).isoformat(),
        "authorization_id": "SYNTHETIC-PRIORITY-TEST", "source_operation_hash": digest(asdict(operation)),
        "execution_mode": "SINGLE_UNSTARTED_TAIL", "residual_transfer_authorized": True,
        "required_quantity": completed + remaining, "in_flight": [], "future_holds": [],
        "remaining_setup_seconds": 0,
        "lots": [
            {"lot_id": f"{operation.operation_key}-done", "quantity": completed,
             "state": "COMPLETED", "completed_at": (ORIGIN - timedelta(hours=1)).isoformat()},
            {"lot_id": f"{operation.operation_key}-tail", "quantity": remaining,
             "state": "NOT_STARTED"},
        ],
        "remaining_work": {"serial_productive_seconds": remaining * 10,
                           "source": "STANDARD_WORK_MODEL", "model_version": "priority-fixture-v1",
                           "assumptions": ["synthetic 10 productive seconds/unit; one eligible crew"]},
    }


def prepare():
    operations = (
        OperationSpec(KEYS[0], "A", "residual", 1, "sew", (1,), 1000, 0),
        OperationSpec(KEYS[1], "A", "finish", 2, "sew", (1,), 120, 0),
        OperationSpec(KEYS[2], "B", "residual", 1, "sew", (1,), 1000, 0),
    )
    problem = SchedulingProblem(
        "SYNTHETIC-PRIORITY-TEST-NOT-LIVE", ORIGIN, 3600,
        (LineSpec(1, 1),), (MachineSpec(1, 1, 1, ("sew",)),),
        (OrderSpec("A", 1, 0, 1050), OrderSpec("B", 1, 0, 3000)), operations, (),
    )
    packets = (evidence(operations[0], 40, 60), evidence(operations[2], 40, 60))
    for packet in packets:
        problem, _ = normalize_problem(problem, packet, decision_time=ORIGIN.isoformat())
    index = {"by_factory": {(1, "sew"): [1]}, "by_line": {(1, "sew"): [1]},
             "by_skill": {"sew": [1]}, "line_factory": {1: 1},
             "shift_of": {1: (6, 14)}, "crew_of": {}, "efficiency_of": {(1, "sew"): 1.0}}
    route = SimpleNamespace(rows=(), by_key={key: {"start_minutes": 0} for key in KEYS},
                            route_predecessor={KEYS[0]: None, KEYS[1]: KEYS[0], KEYS[2]: None},
                            route_successor={KEYS[0]: KEYS[1], KEYS[1]: None, KEYS[2]: None})
    hold_end = 300
    state = {"problem": problem, "operator_index": index, "after_index": route,
             "keys": KEYS, "baseline": SimpleNamespace(operations=()), "event": None,
             "planning_context": {"mode": "INITIAL_PLAN"},
             "objective_profile": "INITIAL_PLAN_NO_STABILITY",
             "classified": {key: {"state": "MOVABLE_WINDOW"} for key in KEYS},
             "_machine_res": {1: [(0, hold_end, "MAINTENANCE", "event:synthetic-hold")]},
             "_worker_res": {"1": [(ORIGIN, ORIGIN + timedelta(seconds=hold_end))]},
             "boundary_facts": ({"machine_id": 1, "start_seconds": 0, "end_seconds": hold_end,
                                 "operation_key": "event:synthetic-hold"},
                                {"worker_id": "1", "start_seconds": 0, "end_seconds": hold_end,
                                 "operation_key": "event:synthetic-hold"})}
    configurations = {op.operation_key: (
        ResourceConfiguration(op.operation_key, 1, 1, 1, (1,), ("1",), 0,
                              op.serial_productive_seconds,
                              ("SYNTHETIC_SINGLE_LEGAL_DOMAIN",), op.serial_productive_seconds / 60),
    ) for op in problem.operations}
    regimes, _, regime_count, equivalence = generate_calendar_regimes(state, configurations)
    return {"state": state, "resource_configurations": configurations, "regimes": regimes}, packets, {
        "regime_count": regime_count, "calendar_equivalence": equivalence,
        "fixture_contract": "SYNTHETIC_PRIORITY_TWO_ORDER_V1",
    }


def solve(tier: str = "NORMAL", *, order_id: str = "B", target_due_seconds: int = 900,
          entered_by: str = "controlled-test", override_id: str = "fixture-override",
          revision: int = 1):
    if tier not in {"NORMAL", "HIGH", "URGENT"}:
        raise ValueError("UNSUPPORTED_PRIORITY_TIER")
    if order_id not in {"A", "B"} or not 1 <= target_due_seconds <= 3600:
        raise ValueError("UNSUPPORTED_PRIORITY_TARGET")
    prepared, packets, metadata = prepare()
    overrides = () if tier == "NORMAL" else ({
        "override_id": override_id, "order_id": order_id, "tier": tier,
        "effective_at": 0, "target_due_at": target_due_seconds,
        "reason_code": "MANAGER_DELIVERY_RISK", "entered_by": entered_by,
        "entered_at": 0, "revision": revision,
    },)
    result = run_canonical_schedule(prepared, plan=((1, None, 5),),
                                    objective_mode=True, manager_overrides=overrides,
                                    objective_time_limit_seconds=5)
    acceptance = result["acceptance"]
    quantities = [validate_residual_output(packet, acceptance.get("selected_schedule") or [],
                                           decision_time=ORIGIN.isoformat()) for packet in packets]
    rows = {row["operation_key"]: row for row in acceptance.get("selected_schedule") or []}
    if set(rows) != set(KEYS):
        raise RuntimeError("PRIORITY_FIXTURE_OPERATION_LOSS")
    order_completion = {"A": rows[KEYS[1]]["end_seconds"], "B": rows[KEYS[2]]["end_seconds"]}
    return {"source": "SYNTHETIC_PRIORITY_FIXTURE", "priority_tier": tier,
            "manager_overrides": list(overrides), "acceptance": acceptance,
            "quantity": {"status": "PASS" if all(row.get("status") == "PASS" for row in quantities)
                         else "FAIL", "rows": quantities},
            "fixture": {**metadata, "order_completion_seconds": order_completion,
                        "order_deadline_seconds": {"A": 1050, "B": 3000},
                        "manager_target_seconds": {order_id: target_due_seconds} if overrides else {},
                        "operation_keys": list(KEYS), "fixed_hold_end_seconds": 300}}
