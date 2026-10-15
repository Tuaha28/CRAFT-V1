from crafter.repair.resource_domain_v2 import (
    ResourceDomainPolicyV2,
    _expansion_targets,
    _next_cap,
    run_adaptive_planning_schedule,
    summarize_resource_domain,
)


def _audit(*, key="op", machine_more=True, worker_more=True,
           machine_capacity=3, worker_capacity=3):
    return [{
        "operation_key": key,
        "total_resource_configurations": 1,
        "truncated": machine_more or worker_more,
        "domain_rows": [{
            "more_machine_sets_available": machine_more,
            "more_worker_crews_available": worker_more,
            "total_legal_machine_sets": machine_capacity,
            "total_legal_worker_crews": worker_capacity,
            "machine_pool_complete": True,
            "worker_pool_complete": True,
            "machine_pool": [1, 2, 3],
            "worker_pool": ["w1", "w2", "w3"],
        }],
    }]


def test_domain_reports_truncation_and_exact_remaining_capacity():
    report = summarize_resource_domain(_audit())
    assert report["resource_domain_truncated"] is True
    assert report["unused_machine_alternatives"] == ["op"]
    assert report["unused_worker_alternatives"] == ["op"]


def test_next_cap_uses_legal_capacity_not_factory_specific_three():
    rows = [{"maximum_legal_machine_sets": 7,
             "machine_alternatives_remaining": True}]
    assert _next_cap(1, rows, "maximum_legal_machine_sets",
                     "machine_alternatives_remaining", 2) == 2
    assert _next_cap(4, rows, "maximum_legal_machine_sets",
                     "machine_alternatives_remaining", 2) == 5


def test_expansion_targets_are_resource_implicated_and_deterministic():
    audit = _audit(key="z") + _audit(key="a", worker_more=False)
    assert _expansion_targets(audit, ("WORKER",)) == ("z",)
    assert _expansion_targets(audit, ("MACHINE",)) == ("a", "z")


def test_policy_is_explicit_and_does_not_embed_factory_or_window_ids():
    policy = ResourceDomainPolicyV2(initial_machine_sets=1,
                                    initial_worker_crews=1,
                                    growth_factor=2)
    assert policy.growth_factor == 2
    assert policy.max_expansion_rounds > 0


def test_adaptive_controller_does_not_expand_a_sufficient_compact_domain(monkeypatch):
    calls = []
    audit = _audit(machine_more=False, worker_more=False,
                   machine_capacity=1, worker_capacity=1)

    def prepare(state, **kwargs):
        calls.append((kwargs["max_machine_sets"], kwargs["max_worker_crews"]))
        return {"state": state, "resource_audit": audit, "regimes": {}}

    def solve(prepared, **kwargs):
        prepared["acceptance"] = {
            "status": "ACCEPTED", "stages": [{"status": "OPTIMAL"}],
            "winning_stage": {"status": "OPTIMAL"}, "selected_schedule": [{"operation_key": "op"}],
            "materialization": {"mismatch_count": 0},
        }
        return prepared

    monkeypatch.setattr("crafter.repair.resource_domain_v2.prepare_planning_schedule", prepare)
    monkeypatch.setattr("crafter.repair.resource_domain_v2.run_canonical_schedule", solve)
    result = run_adaptive_planning_schedule({}, policy=ResourceDomainPolicyV2())
    assert calls == [(1, 1)]
    assert result["resource_domain_v2"]["rounds"][0]["resource_domain_status"] == "ACCEPTED"
    assert result["resource_domain_v2"]["solution_domain_status"] == "FEASIBLE_ON_COMPLETE_CONFIGURATION_DOMAIN"


def test_adaptive_controller_expands_only_diagnosed_worker_dimension(monkeypatch):
    calls = []
    caches = []
    audit = _audit(machine_more=False, worker_more=True,
                   machine_capacity=1, worker_capacity=3)

    def prepare(state, **kwargs):
        calls.append((kwargs["max_machine_sets"], kwargs["max_worker_crews"]))
        caches.append(kwargs["cache"])
        return {"state": state, "resource_audit": audit, "regimes": {}}

    outcomes = iter(("REJECTED", "ACCEPTED"))

    def solve(prepared, **kwargs):
        status = next(outcomes)
        prepared["acceptance"] = {
            "status": status,
            "stages": [{"status": "INFEASIBLE"}] if status == "REJECTED" else [{"status": "OPTIMAL"}],
            "winning_stage": None if status == "REJECTED" else {"status": "OPTIMAL"},
            "selected_schedule": [] if status == "REJECTED" else [{"operation_key": "op"}],
            "materialization": {"mismatch_count": 0},
        }
        return prepared

    monkeypatch.setattr("crafter.repair.resource_domain_v2.prepare_planning_schedule", prepare)
    monkeypatch.setattr("crafter.repair.resource_domain_v2.run_canonical_schedule", solve)
    monkeypatch.setattr(
        "crafter.repair.resource_domain_v2.diagnose_resource_domain_pressure",
        lambda state, prepared, seconds: {"status": "RESOURCE_OVERLAP_PRESSURE",
                                           "implicated_resources": ("WORKER",)},
    )
    result = run_adaptive_planning_schedule({}, policy=ResourceDomainPolicyV2())
    assert calls == [(1, 1), (1, 2)]
    assert result["resource_domain_v2"]["rounds"][0]["expansion"]["next_worker_cap"] == 2
    assert result["resource_domain_v2"]["rounds"][0]["expansion"]["next_machine_cap"] == 1
    assert caches[0] is caches[1]
    assert result["resource_domain_v2"]["solution_domain_status"] == "FEASIBLE_ON_RESTRICTED_DOMAIN"


def test_empty_compact_calendar_domain_expands_without_false_infeasibility(monkeypatch):
    calls = []

    def prepare(state, **kwargs):
        calls.append((kwargs["max_machine_sets"], kwargs["max_worker_crews"],
                      kwargs["allow_zero_regimes"]))
        empty = kwargs["max_worker_crews"] == 1
        return {"state": state, "resource_audit": _audit(
            machine_more=False, worker_more=empty,
            machine_capacity=1, worker_capacity=2),
            "regimes": {"op": () if empty else ({"legal": True},)}}

    def solve(prepared, **kwargs):
        assert prepared["regimes"]["op"]
        prepared["acceptance"] = {
            "status": "ACCEPTED", "stages": [{"status": "OPTIMAL"}],
            "winning_stage": {"status": "OPTIMAL"},
            "selected_schedule": [{"operation_key": "op"}],
            "materialization": {"mismatch_count": 0}}
        return prepared

    monkeypatch.setattr("crafter.repair.resource_domain_v2.prepare_planning_schedule", prepare)
    monkeypatch.setattr("crafter.repair.resource_domain_v2.run_canonical_schedule", solve)
    result = run_adaptive_planning_schedule({}, policy=ResourceDomainPolicyV2(
        max_expansion_rounds=1))
    assert calls == [(1, 1, True), (1, 2, True)]
    assert result["resource_domain_v2"]["rounds"][0]["resource_domain_status"] == "ZERO_CALENDAR_DOMAIN_EXPANDING"
    assert result["resource_domain_v2"]["resource_domain_complete"] is True


def test_empty_calendar_domain_budget_is_not_reported_as_physical_infeasibility(monkeypatch):
    monkeypatch.setattr("crafter.repair.resource_domain_v2.prepare_planning_schedule",
                        lambda state, **kwargs: {
                            "state": state, "resource_audit": _audit(
                                machine_more=False, worker_more=True,
                                machine_capacity=1, worker_capacity=2),
                            "regimes": {"op": ()}})
    monkeypatch.setattr("crafter.repair.resource_domain_v2.run_canonical_schedule",
                        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("CP-SAT must not run")))
    result = run_adaptive_planning_schedule({}, policy=ResourceDomainPolicyV2(
        max_expansion_rounds=0))
    trace = result["resource_domain_v2"]
    assert trace["status"] == "RESOURCE_DOMAIN_EXPANSION_LIMIT"
    assert trace["final_worker_cap"] == 1
    assert trace["physical_infeasibility_proven"] is False
    assert result["acceptance"]["reason"] == "ZERO_CALENDAR_REGIME_DOMAIN"


def test_narrow_zero_window_does_not_block_expansion_after_full_cp_infeasible(monkeypatch):
    calls = []

    def prepare(state, **kwargs):
        calls.append(kwargs['max_worker_crews'])
        return {'state': state, 'resource_audit': _audit(
            machine_more=False, worker_more=kwargs['max_worker_crews'] == 1,
            machine_capacity=1, worker_capacity=2),
            'regimes': {'op': ({'legal': True},)}}

    def solve(prepared, **_kwargs):
        if len(calls) == 1:
            prepared['acceptance'] = {
                'status': 'REJECTED', 'stages': [
                    {'status': 'ZERO_DOMAIN_AFTER_WINDOW'}, {'status': 'INFEASIBLE'}],
                'winning_stage': None, 'selected_schedule': []}
        else:
            prepared['acceptance'] = {
                'status': 'ACCEPTED', 'stages': [{'status': 'FEASIBLE'}],
                'winning_stage': {'status': 'FEASIBLE'},
                'selected_schedule': [{'operation_key': 'op'}],
                'materialization': {'mismatch_count': 0}}
        return prepared

    monkeypatch.setattr('crafter.repair.resource_domain_v2.prepare_planning_schedule', prepare)
    monkeypatch.setattr('crafter.repair.resource_domain_v2.run_canonical_schedule', solve)
    monkeypatch.setattr('crafter.repair.resource_domain_v2.diagnose_resource_domain_pressure',
                        lambda *_a, **_kw: {'status': 'RESOURCE_OVERLAP_PRESSURE',
                                            'implicated_resources': ('WORKER',)})
    result = run_adaptive_planning_schedule({}, policy=ResourceDomainPolicyV2(
        max_expansion_rounds=1))
    assert calls == [1, 2]
    assert result['resource_domain_v2']['rounds'][0]['cp_stage_statuses'] == [
        'ZERO_DOMAIN_AFTER_WINDOW', 'INFEASIBLE']
    assert result['resource_domain_v2']['physical_infeasibility_proven'] is False


def test_source_worker_duration_can_expand_without_overlap_diagnosis(monkeypatch):
    calls = []
    plans = []

    def prepare(state, **kwargs):
        calls.append(kwargs['max_worker_crews'])
        return {'state': state, 'resource_audit': _audit(
            machine_more=False, worker_more=kwargs['max_worker_crews'] == 1,
            machine_capacity=1, worker_capacity=2),
            'regimes': {'op': ({'legal': True},)}}

    def solve(prepared, **kwargs):
        plans.append(kwargs['plan'])
        accepted = len(calls) == 2
        prepared['acceptance'] = {
            'status': 'ACCEPTED' if accepted else 'REJECTED',
            'stages': [{'status': 'FEASIBLE' if accepted else 'INFEASIBLE'}],
            'winning_stage': {'status': 'FEASIBLE'} if accepted else None,
            'selected_schedule': [{'operation_key': 'op'}] if accepted else [],
            'materialization': {'mismatch_count': 0}}
        return prepared

    monkeypatch.setattr('crafter.repair.resource_domain_v2.prepare_planning_schedule', prepare)
    monkeypatch.setattr('crafter.repair.resource_domain_v2.run_canonical_schedule', solve)
    monkeypatch.setattr('crafter.repair.resource_domain_v2.diagnose_resource_domain_pressure',
                        lambda *_a, **_kw: {
                            'status': 'NON_RESOURCE_INFEASIBILITY',
                            'implicated_resources': (),
                            'full_domain_probe': {'status': 'INFEASIBLE'}})
    result = run_adaptive_planning_schedule(
        {'source_resource_domain': {'contract_version': 'SOURCE_RESOURCE_DOMAIN_V1'}},
        policy=ResourceDomainPolicyV2(max_expansion_rounds=1),
        canonical_plan_factory=lambda machines, workers: ((machines * workers, None, 1),))
    assert calls == [1, 2]
    assert plans == [((1, None, 1),), ((2, None, 1),)]
    assert result['resource_domain_v2']['rounds'][0]['diagnosis_limitation'] == \
        'WORKER_DEPENDENT_DURATION_OR_CALENDAR_ALTERNATIVE'
    assert result['resource_domain_v2']['physical_infeasibility_proven'] is False
