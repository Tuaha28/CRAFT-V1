from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from ortools.sat.python import cp_model

from crafter.contracts import (LineSpec, MachineSpec, OperationSpec, OrderSpec,
                               SchedulingProblem)
from crafter.repair.objectives import (ManagerPriorityInputError,
                                       active_manager_overrides,
                                       add_objective_variables,
                                       build_objective_context,
                                       selected_objective_metrics)
from crafter.repair.resource_configs import ResourceConfiguration
from crafter.repair.global_planner import (CommittedScheduleState,
                                           GlobalCRAFTPlanner, RollingWindow,
                                           detect_provisional_hard_freeze,
                                           factory_future_operation_keys)
import crafter.repair.planning as planning_module
from crafter.repair.planning import (PlanningContextV1, build_planning_state,
                                     select_precedence_safe_window)
from crafter.repair.validation_scope import ValidationScopeV1
from crafter.repair.window_phenotype import build_window_phenotype, validate_window_phenotype
from crafter.repair.solve import _anchor, solve_lexicographic_objectives


ORIGIN = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _state(*, deadline=1000, keys=("1_a",), rows=None):
    rows = rows or [("1_a", "1", 1)]
    orders = tuple(OrderSpec(order_id=oid, factory_id=1, release_seconds=0,
                             deadline_seconds=deadline, priority_weight=1)
                   for oid in sorted({order for _, order, _ in rows}))
    operations = tuple(OperationSpec(key, oid, key.split("_")[1], sequence, "sew", (1,), 60)
                       for key, oid, sequence in rows)
    problem = SchedulingProblem("objective-test", ORIGIN, 10_000,
                                (LineSpec(1, 1),), (MachineSpec(1, 1, 1, ("sew",)),),
                                orders, operations, ())
    assignments = []
    for key, oid, _ in rows:
        assignments.append(SimpleNamespace(
            assignment=SimpleNamespace(operation_key=key, line_id=1, machine_count=1,
                                       machine_ids=(1,), start_seconds=0, end_seconds=60),
            employee_ids=("w1",)))
    return {
        "problem": problem, "keys": tuple(keys), "baseline": SimpleNamespace(operations=assignments),
        "event": SimpleNamespace(event_time_seconds=0), "classified": {key: {"state": "FUTURE_RELEASED"} for key in keys},
    }


def _override(**extra):
    row = {"override_id": "ov-1", "order_id": "1", "tier": "URGENT", "effective_at": 0,
           "expires_at": None, "target_due_at": 500, "reason_code": "VIP", "entered_by": "manager",
           "entered_at": 0, "cleared_by": None, "cleared_at": None, "source_event_id": "evt", "revision": 1}
    row.update(extra)
    return row


def _solver_artifact(model, state, starts, ends, configs=None):
    configs = configs or {}
    cfg_lit, reg_lit = {}, {}
    for key in state["keys"]:
        cfg = configs.get(key) or ResourceConfiguration(key, 1, 1, 1, (1,), ("w1",), 0, 1)
        lit = model.NewBoolVar(f"selected_{key}")
        model.Add(lit == 1)
        cfg_lit[key] = {cfg: lit}
        reg_lit[key] = [lit]
    return {"model": model, "starts": starts, "ends": ends,
            "cfg_lit": cfg_lit, "reg_lit": reg_lit,
            "size": {"variables": len(starts) * 3, "constraints": 0,
                     "optional_intervals": 0}}


def _initial_solver(model):
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 42
    assert solver.Solve(model) in (cp_model.FEASIBLE, cp_model.OPTIMAL)
    return solver


def _stage(result, name):
    return next(row for row in result["stages"] if row["name"] == name)


def test_manager_target_supersedes_customer_due_without_overwriting_it():
    context = build_objective_context(_state(deadline=5_000), [_override(target_due_at=500)])
    row = context["orders"][0]
    assert row["customer_deadline_seconds"] == 5_000
    assert row["manager_target_seconds"] == 500
    assert row["effective_due_seconds"] == 500
    assert row["tier"] == "URGENT"


def test_manager_without_target_uses_customer_deadline():
    context = build_objective_context(_state(deadline=5_000), [_override(tier="HIGH", target_due_at=None)])
    assert context["orders"][0]["effective_due_seconds"] == 5_000


def test_high_or_urgent_without_any_due_is_rejected():
    with pytest.raises(ManagerPriorityInputError, match="MANAGER_INPUT_INCOMPLETE"):
        build_objective_context(_state(deadline=None), [_override(target_due_at=None)])


def test_override_revision_and_clear_are_auditable_and_deterministic():
    active = active_manager_overrides([_override(revision=1), _override(override_id="ov-2", revision=2, tier="HIGH")],
                                      event_time_seconds=0, origin=ORIGIN)
    assert active["1"].override_id == "ov-2"
    assert not active_manager_overrides([_override(cleared_at=0)], event_time_seconds=0, origin=ORIGIN)


def test_multi_operation_order_has_one_order_tardiness_variable():
    state = _state(deadline=50, keys=("1_a", "1_b"), rows=[("1_a", "1", 1), ("1_b", "1", 2)])
    model = cp_model.CpModel()
    starts = {key: model.NewIntVar(0, 100, f"s_{key}") for key in state["keys"]}
    ends = {key: model.NewIntVar(0, 100, f"e_{key}") for key in state["keys"]}
    for key in state["keys"]:
        model.Add(ends[key] == starts[key] + 10)
    model.Add(ends["1_a"] <= starts["1_b"])
    config = ResourceConfiguration("1_a", 1, 1, 1, (1,), ("w1",), 0, 10)
    artifact = {"model": model, "starts": starts, "ends": ends,
                "cfg_lit": {key: {config: model.NewConstant(1)} for key in state["keys"]}}
    result = add_objective_variables(state, artifact, build_objective_context(state))
    late = next(x for x in result["expressions"] if x["name"] == "late_order_count_NORMAL")
    assert late["orders"] == ("1",)


def test_urgent_tardiness_expression_dominates_normal_when_conflicting():
    rows = [("1_a", "1", 1), ("2_a", "2", 1)]
    state = _state(deadline=0, keys=("1_a", "2_a"), rows=rows)
    model = cp_model.CpModel()
    starts = {key: model.NewIntVar(0, 10, f"s_{key}") for key in state["keys"]}
    ends = {key: model.NewIntVar(0, 20, f"e_{key}") for key in state["keys"]}
    for key in state["keys"]:
        model.Add(ends[key] == starts[key] + 1)
    model.Add(starts["1_a"] + starts["2_a"] >= 10)
    cfg1 = ResourceConfiguration("1_a", 1, 1, 1, (1,), ("w1",), 0, 1)
    cfg2 = ResourceConfiguration("2_a", 1, 1, 1, (1,), ("w1",), 0, 1)
    artifact = {"model": model, "starts": starts, "ends": ends,
                "cfg_lit": {"1_a": {cfg1: model.NewConstant(1)}, "2_a": {cfg2: model.NewConstant(1)}}}
    context = build_objective_context(state, [_override(order_id="1", target_due_at=0)])
    result = add_objective_variables(state, artifact, context)
    urgent_tardy = next(x for x in result["expressions"] if x["name"] == "total_tardiness_seconds_URGENT")["expression"]
    model.Minimize(urgent_tardy)
    solver = cp_model.CpSolver(); assert solver.Solve(model) == cp_model.OPTIMAL
    assert solver.Value(starts["1_a"]) == 0


def test_objective_layer_has_no_utilization_expression():
    state = _state()
    model = cp_model.CpModel(); start = model.NewIntVar(0, 100, "s"); end = model.NewIntVar(0, 100, "e")
    model.Add(end == start + 10)
    cfg = ResourceConfiguration("1_a", 1, 1, 1, (1,), ("w1",), 0, 10)
    objective = add_objective_variables(state, {"model": model, "starts": {"1_a": start}, "ends": {"1_a": end}, "cfg_lit": {"1_a": {cfg: model.NewConstant(1)}}}, build_objective_context(state))
    assert all("utilization" not in row["name"] for row in objective["expressions"])


def test_initial_plan_reports_churn_unavailable_without_inventing_incumbent():
    state = _state(deadline=1000)
    state["baseline"] = SimpleNamespace(operations=())
    state["objective_profile"] = "INITIAL_PLAN_NO_STABILITY"
    context = build_objective_context(state)
    selected = [{"operation_key": "1_a", "start_seconds": 0, "end_seconds": 60,
                 "resource_configuration": {"line_id": 1, "k": 1, "machine_ids": [1],
                                            "worker_ids": ["w1"], "setup_seconds": 0,
                                            "productive_seconds": 60},
                 "worker_segments": [(0, 60)]}]
    metrics = selected_objective_metrics(state, context, selected)
    assert metrics["orders"][0]["completion_seconds"] == 60
    assert metrics["churn"]["status"] == "NOT_APPLICABLE_NO_PREVIOUS_PLAN"
    assert metrics["churn"]["uncompared_operation_keys"] == ["1_a"]


def test_global_foundation_delegates_to_the_shared_kernel_without_commit():
    calls = []
    def kernel(prepared, **kwargs):
        calls.append(kwargs)
        return {"acceptance": {"status": "ACCEPTED"}}
    planner = GlobalCRAFTPlanner(schedule_kernel=kernel)
    window = RollingWindow(1, 0, 600, 300, "factory-1-w1")
    result = planner.plan_factory_window(window=window, prepared={"state": "state"})
    assert calls and calls[0]["objective_mode"] is True
    assert result["kernel"] == "crafter.repair.solve.run_canonical_schedule"
    assert result["commit_status"] == "REJECTED_VALIDATION_CONTEXT_INCOMPLETE"
    assert result["database_writes"] == 0


def test_initial_plan_has_no_hidden_incumbent_stability_anchor():
    state = _state()
    state['objective_profile'] = 'INITIAL_PLAN_NO_STABILITY'
    configs = (
        ResourceConfiguration('1_a', 1, 2, 1, (2,), ('w2',), 0, 1),
        ResourceConfiguration('1_a', 1, 1, 1, (1,), ('w1',), 0, 1),
    )
    anchor, provenance = _anchor(state, '1_a', configs)
    assert provenance['status'] == 'NO_PREVIOUS_PLAN_DETERMINISTIC_ANCHOR'
    assert anchor.line_id == 1


def test_initial_plan_reports_stability_not_applicable():
    state = _state()
    state['objective_profile'] = 'INITIAL_PLAN_NO_STABILITY'
    model = cp_model.CpModel()
    start = model.NewIntVar(0, 100, 'initial_start')
    end = model.NewIntVar(0, 100, 'initial_end')
    model.Add(end == start + 10)
    cfg = ResourceConfiguration('1_a', 1, 1, 1, (1,), ('w1',), 0, 10)
    objective = add_objective_variables(
        state, {'model': model, 'starts': {'1_a': start}, 'ends': {'1_a': end},
                'cfg_lit': {'1_a': {cfg: model.NewConstant(1)}}},
        build_objective_context(state))
    assert objective['stability_status'] == 'NOT_APPLICABLE_NO_PREVIOUS_PLAN'
    assert not any(row['level'] == 'STABILITY' for row in objective['expressions'])


def test_window_selection_includes_required_uncommitted_predecessor():
    state = _state(keys=('1_a',), rows=[('1_a', '1', 1), ('1_b', '1', 2)])
    context = PlanningContextV1('initial-1', 'INITIAL_PLAN', 1, 0, 0, 500, 250,
                                ('1_b',), 'fixture')
    classified = {'1_a': {'state': 'FUTURE_RELEASED'}, '1_b': {'state': 'FUTURE_RELEASED'}}
    keys, provenance = select_precedence_safe_window(
        state['problem'],
        SimpleNamespace(route_predecessor={'1_a': None, '1_b': '1_a'}),
        classified, context, maximum_operations=2)
    assert keys == ('1_a', '1_b')
    assert provenance[0]['reason'] == 'RELEASE_DUE_WITH_PRECEDENCE_CLOSURE'


def test_snapshot_as_of_is_not_rolling_window_start_for_frozen_rows(monkeypatch):
    state = _state()
    captured = {}

    def fake_classify(rows, as_of, parameters):
        captured['as_of'] = as_of
        return [{'operation_key': '1_a', 'state': 'FUTURE_RELEASED'}]

    monkeypatch.setattr(planning_module, 'classify_operations', fake_classify)
    monkeypatch.setattr(planning_module, 'build_incumbent_index',
                        lambda *args: SimpleNamespace(route_predecessor={}, route_successor={}))
    monkeypatch.setattr(planning_module, 'outside_machine_reservations',
                        lambda *args: {})
    monkeypatch.setattr(planning_module, 'outside_worker_reservations',
                        lambda *args: {})
    context = PlanningContextV1(
        'rolling-2', 'ROLLING_PLAN', 1, 20_000, 20_000, 30_000, 25_000,
        ('1_a',), 'FROZEN_BENCHMARK_RAW', snapshot_as_of_seconds=0)
    loaded = (state['problem'], {}, [{'operation_key': '1_a'}], {}, {}, None,
              state['baseline'], None, 'fixture')
    prepared = build_planning_state(loaded, context)
    assert captured['as_of'] == 0
    assert prepared['snapshot_as_of_seconds'] == 0
    assert prepared['planning_context']['window_start_seconds'] == 20_000
    assert prepared['classified']['1_a']['state'] == 'MOVABLE_WINDOW'


def test_snapshot_as_of_does_not_promote_historical_future_rows(monkeypatch):
    state = _state(keys=(), rows=[('1_a', '1', 1)])
    monkeypatch.setattr(planning_module, 'classify_operations',
                        lambda rows, as_of, parameters: [
                            {'operation_key': '1_a', 'state': 'FUTURE_RELEASED'}])
    monkeypatch.setattr(planning_module, 'build_incumbent_index',
                        lambda *args: SimpleNamespace(route_predecessor={}, route_successor={}))
    monkeypatch.setattr(planning_module, 'outside_machine_reservations', lambda *args: {})
    monkeypatch.setattr(planning_module, 'outside_worker_reservations', lambda *args: {})
    context = PlanningContextV1(
        'rolling-2', 'ROLLING_PLAN', 1, 20_000, 20_000, 30_000, 25_000,
        (), 'FROZEN_BENCHMARK_RAW', snapshot_as_of_seconds=0)
    loaded = (state['problem'], {}, [{'operation_key': '1_a'}], {}, {}, None,
              state['baseline'], None, 'fixture')
    prepared = build_planning_state(loaded, context)
    assert prepared['classified']['1_a']['state'] == 'FUTURE_OUTSIDE_WINDOW'
    assert prepared['snapshot_as_of_seconds'] == 0


def test_window_phenotype_never_backfills_future_baseline_assignments():
    scope = ValidationScopeV1('WINDOW', ('1_a',), ('1_b', '1_c'))
    window, accounting = build_window_phenotype(
        ({'operation_key': '1_a', 'start_seconds': 0, 'end_seconds': 10},),
        boundary_facts=({'operation_key': '0_z', 'role': 'BOUNDARY_ONLY'},),
        future_outside_operation_keys=('1_b', '1_c'), validation_scope=scope)
    assert window.assignment_keys == ('1_a',)
    assert '0_z' not in window.assignment_keys
    assert accounting['future_outside_operations'] == 2
    report = validate_window_phenotype(window)
    assert report['scope_status'] == 'SCOPE_VALID'
    assert report['physical_status'] == 'NOT_RUN'
    assert report['factory_complete_claimed'] is False
    assert report['historical_future_assignments_backfilled'] == 0


def test_future_outside_accounting_is_factory_local():
    orders = (
        OrderSpec(order_id='1', factory_id=1, release_seconds=0,
                  deadline_seconds=1000, priority_weight=1),
        OrderSpec(order_id='2', factory_id=2, release_seconds=0,
                  deadline_seconds=1000, priority_weight=1),
    )
    operations = (
        OperationSpec('1_a', '1', 'a', 1, 'sew', (1,), 60),
        OperationSpec('2_a', '2', 'a', 1, 'sew', (1,), 60),
    )
    problem = SchedulingProblem(
        'factory-local-future', ORIGIN, 10_000,
        (LineSpec(1, 1),), (MachineSpec(1, 1, 1, ('sew',)),),
        orders, operations, ())
    state = {
        'problem': problem,
        'planning_context': {'factory_id': 1},
        'classified': {
            '1_a': {'state': 'FUTURE_OUTSIDE_WINDOW'},
            '2_a': {'state': 'FUTURE_OUTSIDE_WINDOW'},
        },
    }
    assert factory_future_operation_keys(state, 1) == ('1_a',)


def test_rolling_boundary_commits_exact_machine_and_worker_segments_without_cutting_operation():
    selected = [{"operation_key": "1_a", "start_seconds": 100, "end_seconds": 400,
                 "worker_segments": ((115, 200), (300, 400)),
                 "resource_configuration": {"factory_id": 1, "line_id": 1,
                                            "machine_ids": (7,), "worker_ids": ("w1",)}}]
    result = {"acceptance": {"status": "ACCEPTED", "selected_schedule": selected}}
    boundary = GlobalCRAFTPlanner.boundary_state_from_accepted_repair(
        result, factory_id=1, commit_end_seconds=250, source_repair_id="w1")
    assert boundary.committed_operation_keys == ("1_a",)
    assert boundary.cross_boundary_operation_keys == ("1_a",)
    assert boundary.machine_commitments[0]["start_seconds"] == 100
    assert boundary.machine_commitments[0]["end_seconds"] == 400
    assert {(x["start_seconds"], x["end_seconds"]) for x in boundary.worker_commitments} == {(115, 200), (300, 400)}
    with pytest.raises(ValueError, match="duplicate"):
        CommittedScheduleState(1).append(boundary).append(boundary)


def test_provisional_hard_freeze_detector_uses_incoming_boundary_not_outgoing_tautology():
    # A: a provisional row becomes a new commitment after this solve: legal.
    assert detect_provisional_hard_freeze(
        prior_provisional_keys=("P",), incoming_hard_operation_keys=(),
        previously_committed_keys=(), current_new_commitment_keys=("P",)) == ()
    # B: it remains provisional and is not carried as hard: legal.
    assert detect_provisional_hard_freeze(
        prior_provisional_keys=("P",), incoming_hard_operation_keys=(),
        previously_committed_keys=(), current_new_commitment_keys=()) == ()
    # C: it was already present in the incoming hard boundary: illegal.
    assert detect_provisional_hard_freeze(
        prior_provisional_keys=("P",), incoming_hard_operation_keys=("P",),
        previously_committed_keys=(), current_new_commitment_keys=()) == ("P",)
    # An operation already legitimate in the committed state is not a
    # provisional freeze, even if a richer boundary repeats its key.
    assert detect_provisional_hard_freeze(
        prior_provisional_keys=("P",), incoming_hard_operation_keys=("P",),
        previously_committed_keys=("P",), current_new_commitment_keys=()) == ()


def test_production_sequencer_keeps_urgent_service_ahead_of_normal_service():
    rows = [("1_a", "1", 1), ("2_a", "2", 1)]
    state = _state(deadline=0, keys=("1_a", "2_a"), rows=rows)
    model = cp_model.CpModel()
    urgent_start = model.NewIntVar(0, 10, "urgent_start")
    normal_start = model.NewIntVar(0, 10, "normal_start")
    urgent_end = model.NewIntVar(1, 11, "urgent_end")
    normal_end = model.NewIntVar(1, 11, "normal_end")
    model.Add(urgent_end == urgent_start + 1)
    model.Add(normal_end == normal_start + 1)
    # Exactly one order can receive the early slot.  Both candidate schedules
    # are physically feasible; only priority may decide between them.
    model.Add(urgent_start + normal_start == 10)
    artifact = _solver_artifact(model, state,
                                {"1_a": urgent_start, "2_a": normal_start},
                                {"1_a": urgent_end, "2_a": normal_end})
    result = solve_lexicographic_objectives(
        state, artifact, _initial_solver(model), manager_overrides=[_override(target_due_at=1)],
        time_limit_seconds=2)
    assert result["status"] == "OBJECTIVE_OPTIMIZATION_COMPLETE"
    assert result["solver"].Value(urgent_start) == 0
    assert result["solver"].Value(normal_start) == 10
    assert _stage(result, "late_order_count_URGENT")["proven_optimal"] is True


def test_production_sequencer_keeps_high_service_ahead_of_normal_service():
    rows = [("1_a", "1", 1), ("2_a", "2", 1)]
    state = _state(deadline=0, keys=("1_a", "2_a"), rows=rows)
    model = cp_model.CpModel()
    high_start, normal_start = model.NewIntVar(0, 10, "high_start"), model.NewIntVar(0, 10, "normal_start")
    high_end, normal_end = model.NewIntVar(1, 11, "high_end"), model.NewIntVar(1, 11, "normal_end")
    model.Add(high_end == high_start + 1); model.Add(normal_end == normal_start + 1)
    model.Add(high_start + normal_start == 10)
    artifact = _solver_artifact(model, state, {"1_a": high_start, "2_a": normal_start},
                                {"1_a": high_end, "2_a": normal_end})
    result = solve_lexicographic_objectives(
        state, artifact, _initial_solver(model),
        manager_overrides=[_override(tier="HIGH", target_due_at=1)], time_limit_seconds=2)
    assert result["status"] == "OBJECTIVE_OPTIMIZATION_COMPLETE"
    assert result["solver"].Value(high_start) == 0
    assert _stage(result, "late_order_count_HIGH")["proven_optimal"] is True


def test_same_tier_late_count_precedes_total_tardiness():
    rows = [("1_a", "1", 1), ("2_a", "2", 1)]
    state = _state(deadline=0, keys=("1_a", "2_a"), rows=rows)
    model = cp_model.CpModel(); choice = model.NewBoolVar("one_late")
    starts = {key: model.NewIntVar(0, 100, f"s_{key}") for key in state["keys"]}
    ends = {key: model.NewIntVar(0, 100, f"e_{key}") for key in state["keys"]}
    # choice=1 has one late order (100 seconds); choice=0 has two orders each
    # 10 seconds late.  The count must win before total tardiness is considered.
    model.Add(ends["1_a"] == 100 * choice + 10 * (1 - choice))
    model.Add(ends["2_a"] == 10 * (1 - choice))
    for key in state["keys"]: model.Add(starts[key] == 0)
    result = solve_lexicographic_objectives(state, _solver_artifact(model, state, starts, ends),
                                            _initial_solver(model), time_limit_seconds=2)
    assert result["status"] == "OBJECTIVE_OPTIMIZATION_COMPLETE"
    assert result["solver"].Value(choice) == 1
    assert _stage(result, "late_order_count_NORMAL")["value"] == 1
    assert _stage(result, "total_tardiness_seconds_NORMAL")["value"] == 100


def test_same_tier_total_then_maximum_tardiness_breaks_ties_in_order():
    rows = [("1_a", "1", 1), ("2_a", "2", 1)]
    state = _state(deadline=0, keys=("1_a", "2_a"), rows=rows)
    model = cp_model.CpModel(); choice = model.NewBoolVar("balanced_max")
    starts = {key: model.NewIntVar(0, 10, f"s_{key}") for key in state["keys"]}
    ends = {key: model.NewIntVar(0, 20, f"e_{key}") for key in state["keys"]}
    # Both choices have two late orders and equal total (12): 10+2 versus
    # 6+6.  The maximum-tardiness stage must choose the balanced 6+6 result.
    model.Add(ends["1_a"] == 6 * choice + 10 * (1 - choice))
    model.Add(ends["2_a"] == 6 * choice + 2 * (1 - choice))
    for key in state["keys"]: model.Add(starts[key] == 0)
    result = solve_lexicographic_objectives(state, _solver_artifact(model, state, starts, ends),
                                            _initial_solver(model), time_limit_seconds=2)
    assert result["status"] == "OBJECTIVE_OPTIMIZATION_COMPLETE"
    assert result["solver"].Value(choice) == 1
    assert _stage(result, "total_tardiness_seconds_NORMAL")["value"] == 12
    assert _stage(result, "maximum_tardiness_seconds_NORMAL")["value"] == 6


def test_stability_minimizes_changed_operations_after_delivery_ties():
    keys = tuple(f"{i}_a" for i in range(20))
    rows = [(key, str(i), 1) for i, key in enumerate(keys)]
    state = _state(deadline=9_000, keys=keys, rows=rows)
    model = cp_model.CpModel()
    changes = [model.NewBoolVar(f"changed_{key}") for key in keys]
    starts = {key: model.NewIntVar(0, 1, f"s_{key}") for key in keys}
    ends = {key: model.NewIntVar(1, 2, f"e_{key}") for key in keys}
    for key, changed in zip(keys, changes):
        model.Add(starts[key] == changed); model.Add(ends[key] == starts[key] + 1)
    model.Add(sum(changes) >= 2)
    result = solve_lexicographic_objectives(state, _solver_artifact(model, state, starts, ends),
                                            _initial_solver(model), time_limit_seconds=2)
    assert result["status"] == "OBJECTIVE_OPTIMIZATION_COMPLETE"
    assert sum(result["solver"].Value(x) for x in changes) == 2
    assert _stage(result, "changed_operations")["value"] == 2


def test_stability_prefers_incumbent_line_crew_machine_and_k_when_delivery_ties():
    state = _state(deadline=9_000)
    model = cp_model.CpModel(); start = model.NewIntVar(0, 0, "s"); end = model.NewIntVar(1, 1, "e")
    model.Add(end == start + 1)
    incumbent = ResourceConfiguration("1_a", 1, 1, 1, (1,), ("w1",), 0, 1)
    alternative = ResourceConfiguration("1_a", 1, 2, 2, (2, 3), ("w2", "w3"), 0, 1)
    use_incumbent, use_alternative = model.NewBoolVar("incumbent"), model.NewBoolVar("alternative")
    model.Add(use_incumbent + use_alternative == 1)
    artifact = {"model": model, "starts": {"1_a": start}, "ends": {"1_a": end},
                "cfg_lit": {"1_a": {incumbent: use_incumbent, alternative: use_alternative}},
                "reg_lit": {"1_a": [use_incumbent, use_alternative]},
                "size": {"variables": 4, "constraints": 2, "optional_intervals": 0}}
    initial = _initial_solver(model)
    result = solve_lexicographic_objectives(state, artifact, initial, time_limit_seconds=2)
    assert result["status"] == "OBJECTIVE_OPTIMIZATION_COMPLETE"
    assert result["solver"].Value(use_incumbent) == 1
    for name in ("line_changes", "crew_changes", "machine_changes", "k_changes"):
        assert _stage(result, name)["value"] == 0


def test_hard_feasibility_cannot_be_overridden_by_urgent_target():
    state = _state(deadline=1000)
    model = cp_model.CpModel(); start = model.NewIntVar(0, 20, "s"); end = model.NewIntVar(1, 21, "e")
    model.Add(start >= 7); model.Add(end == start + 1)
    artifact = _solver_artifact(model, state, {"1_a": start}, {"1_a": end})
    result = solve_lexicographic_objectives(state, artifact, _initial_solver(model),
                                            manager_overrides=[_override(target_due_at=0)], time_limit_seconds=2)
    assert result["status"] == "OBJECTIVE_OPTIMIZATION_COMPLETE"
    assert result["solver"].Value(start) == 7


def test_objective_sequencer_is_deterministic_for_same_snapshot_and_seed():
    def run_once():
        rows = [("1_a", "1", 1), ("2_a", "2", 1)]
        state = _state(deadline=0, keys=("1_a", "2_a"), rows=rows)
        model = cp_model.CpModel(); first, second = model.NewIntVar(0, 10, "a"), model.NewIntVar(0, 10, "b")
        first_end, second_end = model.NewIntVar(1, 11, "ea"), model.NewIntVar(1, 11, "eb")
        model.Add(first + second == 10); model.Add(first_end == first + 1); model.Add(second_end == second + 1)
        artifact = _solver_artifact(model, state, {"1_a": first, "2_a": second}, {"1_a": first_end, "2_a": second_end})
        result = solve_lexicographic_objectives(state, artifact, _initial_solver(model),
                                                manager_overrides=[_override(target_due_at=1)], time_limit_seconds=2)
        return (result["status"], result["solver"].Value(first), result["solver"].Value(second),
                tuple((x["name"], x.get("value"), x["proven_optimal"]) for x in result["stages"]))
    assert run_once() == run_once()


def test_partial_stage_preserves_feasible_incumbent_without_false_optimum():
    state = _state(deadline=0)
    model = cp_model.CpModel(); start = model.NewIntVar(0, 100, "s"); end = model.NewIntVar(1, 101, "e")
    model.Add(end == start + 1)
    initial = _initial_solver(model)
    result = solve_lexicographic_objectives(state, _solver_artifact(model, state, {"1_a": start}, {"1_a": end}),
                                            initial, time_limit_seconds=0)
    assert result["status"] == "OBJECTIVE_OPTIMIZATION_PARTIAL"
    row = _stage(result, "late_order_count_NORMAL")
    assert row["proven_optimal"] is False
    assert result["solver"] is initial
