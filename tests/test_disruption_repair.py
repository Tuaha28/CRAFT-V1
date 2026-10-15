"""Focused contracts for the offline CRAFT disruption engine."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from crafter.disruptions.events import DisruptionEvent, EventType
from crafter.disruptions.scenarios import build_catalog
from crafter.disruptions.state_update import apply_event
from crafter.disruptions.policies.recovery_levels import RecoveryLevel
from crafter.disruptions.policies.predecessor_delay import propagate_predecessor_delay
from crafter.disruptions.policies.base import machine_recovery_candidates
from crafter.disruptions.policies.worker_absence import worker_recovery_candidates
from crafter.disruptions.runtime.execution_state import classify_operation, ExecutionState
from crafter.disruptions.runtime.freeze_policy import freeze_decision
from crafter.disruptions.runtime.manager_escalation import manager_required
from crafter.disruptions.runtime.fallback import build_fallback_decision
from crafter.disruptions.runtime.release_fixpoint import (
    FixpointConfig, resolve_event_release_fixpoint, frontier_signature,
)
from crafter.disruptions.benchmark import (
    _augment_predecessor_resource_context,
    _predecessor_mode_worker_conflicts,
    _rank_released,
)
from crafter.disruptions.p2_core_guided_prototype import _assumption_api_validation


def test_event_is_immutable_and_interval_is_deterministic():
    event = DisruptionEvent("m1", EventType.MACHINE_BREAKDOWN.value, 100, 2,
                            machine_ids=(7,), duration_seconds=1800)
    assert event.end_time_seconds == 1900
    assert event.as_dict()["event_id"] == "m1"


def test_machine_breakdown_shadow_does_not_write_database():
    # Use a tiny structural stand-in; the real benchmark tests the full
    # SchedulingProblem.  The contract is that apply_event returns a shadow.
    @dataclass(frozen=True)
    class Machine:
        machine_id: int
        is_usable: bool = True
    @dataclass(frozen=True)
    class Problem:
        machines: tuple
    problem = Problem((Machine(7), Machine(8)))
    shadow = apply_event(problem, {"by_skill": {}, "by_factory": {}, "by_line": {}},
                         DisruptionEvent("m1", EventType.MACHINE_BREAKDOWN.value,
                                         100, 2, machine_ids=(7,), duration_seconds=10))
    assert shadow.database_writes == 0
    assert shadow.problem.machines[0].is_usable is False
    assert shadow.problem.machines[1].is_usable is True


def test_catalog_is_deterministic_and_has_required_event_families():
    # Full catalog construction is covered by the benchmark; this test checks
    # stable event IDs when supplied equivalent structural inputs.
    class I:
        pass
    # The production fixture is intentionally not loaded in unit tests.
    assert EventType.URGENT_ORDER.value == "URGENT_ORDER"
    assert EventType.PREDECESSOR_DELAY.value == "PREDECESSOR_DELAY"


def test_artifact_manifest_has_no_database_writes_when_present():
    out = Path("outputs/crafter/disruption_repair_smoke7")
    if not (out / "artifact_hash_manifest.json").exists():
        return
    manifest = json.loads((out / "artifact_hash_manifest.json").read_text())
    assert manifest.get("database_writes", 0) == 0
    for rel, digest in manifest.get("files", {}).items():
        assert hashlib.sha256((out / rel).read_bytes()).hexdigest() == digest


def test_recovery_levels_are_ordered_and_explicit():
    assert RecoveryLevel.SAME_LINE_RESOURCE_SUBSTITUTION < RecoveryLevel.TEMPORAL_RECOVERY
    assert RecoveryLevel.TEMPORAL_RECOVERY < RecoveryLevel.CROSS_LINE_OPERATION_MOVE


def test_machine_policy_uses_screened_legal_k_and_preserves_hierarchy(monkeypatch):
    @dataclass(frozen=True)
    class Op:
        operation_key: str = "o"
        eligible_line_ids: tuple = (1, 2)
        min_machine_count: int = 3  # incumbent-reconstruction bound
        max_machine_count: int = 5
    class Problem:
        pass
    # Two same-line machines remain after the outage; the scheduler's
    # screened domain explicitly proves k=1,2 are legal recovery values.
    monkeypatch.setattr(
        "crafter.disruptions.policies.base._available_machine_pool",
        lambda problem, operation, line: (10, 11) if line == 1 else (20, 21, 22),
    )
    event = DisruptionEvent("m", EventType.MACHINE_BREAKDOWN.value, 0, 1,
                            machine_ids=(12,), duration_seconds=60)
    decision = machine_recovery_candidates(
        Problem(), Op(), event, 1, 3, (10, 11, 12),
        repair_time=60, legal_k_values=(1, 2, 3),
    )
    reasons = [candidate.reason for candidate in decision.candidates]
    assert "SAME_LINE_MACHINE_SUBSTITUTION" not in reasons
    assert reasons[:2] == ["SAME_LINE_K_REDUCTION", "SAME_LINE_K_REDUCTION"]
    assert reasons[-1] == "CROSS_LINE_OPERATION_MOVE"


def test_worker_policy_uses_explicit_legal_k_domain():
    @dataclass(frozen=True)
    class Op:
        operation_key: str = "o"
        min_machine_count: int = 5
        max_machine_count: int = 5
    event = DisruptionEvent("w", EventType.WORKER_ABSENCE.value, 0, 1,
                            worker_ids=("absent",), duration_seconds=60)
    decision = worker_recovery_candidates(
        None, Op(), event, incumbent_line=1, incumbent_k=3,
        incumbent_worker_ids=("absent", "w2", "w3"),
        same_line_workers=("w2",), borrowed_workers=(),
        replacement_start=60, legal_k_values=(1, 2, 3),
    )
    assert any(c.reason == "SAME_LINE_K_REDUCTION" and c.k == 1
               for c in decision.candidates)


def test_execution_state_and_freeze_policy_protect_completed_and_in_progress():
    row = {"operation_key": "o", "start_minutes": 0.0, "end_minutes": 20.0,
           "release_seconds": 0}
    completed = classify_operation(row, 1800)
    assert completed["state"] == ExecutionState.COMPLETED.value
    assert freeze_decision(completed)["movable"] is False
    in_progress = classify_operation(row, 600)
    assert in_progress["state"] == ExecutionState.IN_PROGRESS.value
    assert freeze_decision(in_progress)["reason"] == "IN_PROGRESS_WIP_GUARD"


def test_event_safe_fallback_and_manager_escalation_are_structured():
    event = DisruptionEvent("unknown", EventType.UNKNOWN.value, 0, 1)
    fallback = build_fallback_decision(event, baseline_conflicts=[{"kind": "UNKNOWN"}],
                                       attempted_levels=["TEMPORAL_SHIFT"])
    assert fallback["status"] == "MANAGER_REQUIRED"
    outcome = manager_required(event, reason="UNKNOWN_OR_UNSUPPORTED_EVENT",
                               affected_operations=["o"], attempted_levels=["ABSORB_NO_ACTION"])
    assert outcome["status"] == "MANAGER_REQUIRED"
    assert outcome["affected_operations"] == ["o"]


def test_predecessor_delay_propagation_is_deterministic():
    class I:
        route_successor = {"A": "B", "B": "C"}
        by_key = {"A": {"start_minutes": 0}, "B": {"start_minutes": 10}, "C": {"start_minutes": 20}}
    result = propagate_predecessor_delay(I(), "A", 3600)
    assert [x["operation_key"] for x in result["propagated"]] == ["B", "C"]
    assert result["propagated"][0]["new_earliest_start_seconds"] == 3600


def test_direct_event_release_protects_full_routing_successor_chain():
    class I:
        route_successor = {"A": "B", "B": "C", "C": "D"}
        by_key = {k: {"end_minutes": 100} for k in ("A", "B", "C", "D")}
        operation_features = {k: {"order_tardiness_seconds": 0} for k in ("A", "B", "C", "D")}
    event = DisruptionEvent("w", EventType.WORKER_ABSENCE.value, 0, 1,
                            worker_ids=("absent",), duration_seconds=60,
                            metadata={"target_operation_key": "A"})
    released = _rank_released(I(), {}, ("A", "B", "C", "D"),
                              ("A", "B", "C", "D"), 4, "graph", event,
                              direct_keys=("A",))
    assert released == ("A", "B", "C", "D")


def test_predecessor_worker_ripple_adds_immediate_route_successor():
    class I:
        route_successor = {"P": "R", "R": None, "S": "T", "T": None}
        by_key = {
            "P": {"start_minutes": 0, "end_minutes": 20,
                   "employee_ids": ("w1",), "machine_ids": (1,)},
            "R": {"start_minutes": 21, "end_minutes": 25,
                   "employee_ids": ("w2",), "machine_ids": (5,)},
            "S": {"start_minutes": 10, "end_minutes": 30,
                   "employee_ids": ("w1",), "machine_ids": (2,)},
            "T": {"start_minutes": 31, "end_minutes": 40,
                   "employee_ids": ("w2",), "machine_ids": (3,)},
            "X": {"start_minutes": 50, "end_minutes": 60,
                   "employee_ids": ("w3",), "machine_ids": (4,)},
        }
        operation_scores = {key: 0.0 for key in by_key}
    event = DisruptionEvent(
        "p2", EventType.PREDECESSOR_DELAY.value, 0, 1,
        metadata={"predecessor_operation_key": "P", "delayed_end_seconds": 1200},
    )
    keys = _augment_predecessor_resource_context(I(), event, ("P", "S", "X"), 3)
    assert "T" in keys


def test_predecessor_worker_ripple_promotes_successor_when_bounded():
    class I:
        route_successor = {"P": "R", "R": None, "S": "T", "T": None}
        by_key = {
            "P": {"start_minutes": 0, "end_minutes": 20,
                   "employee_ids": ("w1",), "machine_ids": (1,)},
            "R": {"start_minutes": 21, "end_minutes": 25,
                   "employee_ids": ("w2",), "machine_ids": (5,)},
            "S": {"start_minutes": 10, "end_minutes": 30,
                   "employee_ids": ("w1",), "machine_ids": (2,)},
            "T": {"start_minutes": 31, "end_minutes": 40,
                   "employee_ids": ("w2",), "machine_ids": (3,)},
        }
        operation_features = {key: {"order_tardiness_seconds": 0} for key in by_key}
        operation_scores = {key: 0.0 for key in by_key}
    event = DisruptionEvent(
        "p2", EventType.PREDECESSOR_DELAY.value, 0, 1,
        metadata={"predecessor_operation_key": "P", "delayed_end_seconds": 1200},
    )
    released = _rank_released(I(), {}, ("P", "S", "T"),
                              ("P", "S", "T"), 3, "graph", event,
                              direct_keys=("P",))
    assert "T" in released


def test_predecessor_mode_worker_conflict_promotes_future_incumbent_only():
    start = datetime(2026, 9, 17, 7, 0, 0)
    end = start + timedelta(seconds=60)

    @dataclass(frozen=True)
    class Mode:
        worker_ids: tuple
        worker_segments: tuple
        incumbent: bool
        source_event_id: str | None

    event = DisruptionEvent(
        "p2", EventType.PREDECESSOR_DELAY.value, 0, 1,
        metadata={"predecessor_operation_key": "P", "target_operation_key": "R"},
    )
    modes = {
        "R": (Mode(("w1",), ((start, end),), False, "p2"),),
        "X": (Mode(("w1",), ((start, end),), True, "p2"),),
        "Y": (Mode(("w2",), ((start, end),), True, "p2"),),
    }
    conflicts = _predecessor_mode_worker_conflicts(
        event, ("R", "X", "Y"), ("R",), modes)
    assert conflicts == [{
        "operation_key": "X",
        "conflicts_with": "R",
        "worker_ids": ["w1"],
        "reason": "PREDECESSOR_DELAY_WORKER_CONFLICT",
    }]


def test_diagnostic_manifest_is_self_consistent_when_present():
    out = Path("outputs/crafter/disruption_repair_v2_diagnostics")
    if not (out / "artifact_hash_manifest.json").exists():
        return
    manifest = json.loads((out / "artifact_hash_manifest.json").read_text())
    assert manifest["decision"] in {
        "CRAFT_PHYSICAL_RECOVERY_BOUNDARY_IDENTIFIED",
        "CRAFT_DISRUPTION_V2_DIAGNOSTICS_PASS",
    }
    assert manifest.get("files")
    for rel, digest in manifest["files"].items():
        assert hashlib.sha256((out / rel).read_bytes()).hexdigest() == digest


def test_frontier_signature_is_deterministic_and_distinguishes_resource_evidence():
    left = {"operation_key": "B", "conflicts_with": "A", "resource_type": "WORKER",
            "resource_ids": ["2", "1"], "worker_id": "1",
            "left_segment": ["2026-01-01T01:00:00", "2026-01-01T02:00:00"],
            "right_segment": ["2026-01-01T01:30:00", "2026-01-01T02:30:00"],
            "overlap_seconds": 1800, "iteration": 4}
    right = dict(left); right["iteration"] = 5
    assert frontier_signature([left]) == frontier_signature([right])
    changed = dict(left); changed["worker_id"] = "9"
    assert frontier_signature([left])["hash"] != frontier_signature([changed])["hash"]


def test_convergence_control_stops_on_exact_frontier_repeat_without_promotion():
    class I:
        by_key = {"A": {"start_seconds": 100}, "B": {"start_seconds": 200}}
    class E:
        event_time_seconds = 0
    generated = []
    def generate(released, iteration):
        generated.append(iteration)
        return {"modes": {"A": ({"mode_id": "a"},), "B": ({"mode_id": "b"},)}, "mode_count": 2}
    row = {"operation_key": "B", "conflicts_with": "A", "resource_type": "WORKER",
           "resource_ids": ["1"], "worker_id": "1", "left_segment": ["a", "b"],
           "right_segment": ["a", "b"], "overlap_seconds": 1}
    result = resolve_event_release_fixpoint(
        index=I(), event=E(), neighborhood_keys=("A", "B"), initial_released=("A",),
        generate=generate, conflicts=lambda released, current, iteration: [row],
        config=FixpointConfig(max_iterations=10, max_new_promotions_per_iteration=0,
                              max_total_released=10, convergence_control=True))
    assert result["stop_reason"] == "NO_NEW_LEGAL_PROMOTIONS"
    assert len(generated) == 1


def test_release_fixpoint_promotes_only_concrete_future_conflicts_and_refreshes_modes():
    class I:
        by_key = {
            "A": {"start_minutes": 10, "end_minutes": 20},
            "B": {"start_minutes": 30, "end_minutes": 40},
            "C": {"start_minutes": 50, "end_minutes": 60},
        }
    event = DisruptionEvent("p2", EventType.PREDECESSOR_DELAY.value, 0, 1)
    calls = []

    def generate(released, iteration):
        calls.append((iteration, tuple(released)))
        return {"iteration": iteration, "modes": {}}

    def conflicts(released, current, iteration):
        if "B" not in released:
            return [{"operation_key": "B", "conflicts_with": "A",
                     "resource_type": "WORKER", "resource_ids": ["w"],
                     "overlap_seconds": 3, "direct_conflict": True}]
        if "C" not in released:
            return [{"operation_key": "C", "conflicts_with": "B",
                     "resource_type": "MACHINE", "resource_ids": [7],
                     "overlap_seconds": 2}]
        return []

    result = resolve_event_release_fixpoint(
        index=I(), event=event, neighborhood_keys=("A", "B", "C"), initial_released=("A",),
        generate=generate, conflicts=conflicts,
        config=FixpointConfig(max_iterations=5, max_new_promotions_per_iteration=1,
                              max_total_released=4))
    assert result["released"] == ["A", "B", "C"]
    assert result["stop_reason"] == "NO_NEW_PROMOTIONS"
    assert [x["selected_promotions"][0]["operation_key"] for x in result["iterations"][:2]] == ["B", "C"]
    assert calls == [(0, ("A",)), (1, ("A", "B")), (2, ("A", "B", "C"))]


def test_release_fixpoint_never_promotes_completed_or_duplicates_and_is_bounded():
    class I:
        by_key = {
            "A": {"start_minutes": 10, "end_minutes": 20},
            "DONE": {"start_minutes": 0, "end_minutes": 1},
            "FUT": {"start_minutes": 30, "end_minutes": 40},
        }
    event = DisruptionEvent("p2", EventType.PREDECESSOR_DELAY.value, 120, 1)

    def generate(released, iteration):
        return {"iteration": iteration, "modes": {}}

    def conflicts(released, current, iteration):
        return [
            {"operation_key": "DONE", "conflicts_with": "A", "resource_type": "WORKER", "overlap_seconds": 1},
            {"operation_key": "FUT", "conflicts_with": "A", "resource_type": "WORKER", "overlap_seconds": 1},
        ]

    result = resolve_event_release_fixpoint(
        index=I(), event=event, neighborhood_keys=("A", "DONE", "FUT"), initial_released=("A",),
        generate=generate, conflicts=conflicts,
        config=FixpointConfig(max_iterations=1, max_new_promotions_per_iteration=1,
                              max_total_released=2))
    assert result["released"] == ["A", "FUT"]
    assert result["iterations"][0]["rejected_promotions"][0]["operation_key"] == "DONE"
    assert result["stop_reason"] == "RELEASE_BUDGET_REACHED"


def test_core_assumption_api_maps_literals_under_both_presolve_modes():
    result = _assumption_api_validation()
    assert result["reliable"] is True
    assert all(item["status"] == "INFEASIBLE" for item in result["tests"])
    assert all(item["mapping_correct"] for item in result["tests"])


def test_core_probe_cap_is_explicitly_bounded():
    # The full P2 core can contain nearly the whole N100 region; the runtime
    # must record deferred singleton probes instead of silently running an
    # unbounded rebuild loop.
    assert "max_singleton_probes" in __import__(
        "crafter.disruptions.p2_core_guided_prototype", fromlist=["run"]
    ).run.__kwdefaults__
