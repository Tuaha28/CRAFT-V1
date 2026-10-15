"""
Regression tests for the scheduling-correctness fix batch.

Each test pins a specific defect that was found by audit and fixed here, so
a later change that reintroduces it fails loudly instead of quietly going
back to producing a better-looking but physically impossible schedule.
"""

from datetime import datetime, timedelta

import pandas as pd
import pytest

from agent.decision_engine import AIDecisionEngine
from ga.chromosome import ChromosomeFactory
from ga.fitness import evaluate_schedule
from ga.infeasibility import ScheduleInfeasibleError


# ---------------------------------------------------------------------------
# FIX 2 -- the fitness precedence validator uses the real routing sequence
# ---------------------------------------------------------------------------

def _one_order_kwargs(op_keys, seq_map):
    op_details = {k: {'setup_mins': 0.0, 'serial_processing_mins': 30.0} for k in op_keys}
    gnn = {k: {'delay': 0.0, 'priority': 0.5} for k in op_keys}
    pools = {k: {10: ['m1']} for k in op_keys}
    return dict(
        all_line_ids=[10], machine_pools=pools, machine_line_map={'m1': 10},
        usable_machine_count=1, present_operator_count=1, return_details=True,
        op_sequence_map=seq_map,
    )


def test_validator_accepts_routing_order_when_ids_are_out_of_sequence():
    # Order 500's routing steps 1, 2, 3 are operation_ids 30, 10, 20 -- the
    # same out-of-sequence case the repair_precedence fix used. A chromosome
    # in correct ROUTING order is 500_30, 500_10, 500_20.
    #
    # The old validator parsed int(op_id) and would have seen 30, 10, 20 --
    # decreasing -- and rejected this correct schedule with fitness=inf.
    keys = ['500_30', '500_10', '500_20']
    seq = {'500_30': 1, '500_10': 2, '500_20': 3}
    individual = keys + [10, 10, 10] + [1, 1, 1]
    fit, det = evaluate_schedule(individual, 3, *_details(keys), **_one_order_kwargs(keys, seq))
    assert 'invalid' not in det, det.get('invalid')
    assert fit[0] != float('inf')


def test_validator_still_rejects_a_genuine_precedence_violation():
    # Same operations, listed in ID order -- which is routing steps 1, 3, 2.
    keys = ['500_30', '500_20', '500_10']
    seq = {'500_30': 1, '500_10': 2, '500_20': 3}
    individual = keys + [10, 10, 10] + [1, 1, 1]
    fit, det = evaluate_schedule(individual, 3, *_details(keys), **_one_order_kwargs(keys, seq))
    assert fit[0] == float('inf')
    assert 'precedence violated' in det['invalid']


def test_unknown_routing_sequence_is_rejected_not_silently_skipped():
    # The old code had `except ValueError: continue`, so an operation whose
    # sequence could not be determined silently passed validation.
    keys = ['500_30', '500_10']
    seq = {'500_30': 1}  # 500_10 deliberately absent
    individual = keys + [10, 10] + [1, 1]
    fit, det = evaluate_schedule(individual, 2, *_details(keys), **_one_order_kwargs(keys, seq))
    assert fit[0] == float('inf')
    assert 'no routing sequence known' in det['invalid']


def _details(keys):
    return (
        {k: {'setup_mins': 0.0, 'serial_processing_mins': 30.0} for k in keys},
        {k: {'delay': 0.0, 'priority': 0.5} for k in keys},
    )


# ---------------------------------------------------------------------------
# FIX 3 -- one factory per order
# ---------------------------------------------------------------------------

def _routing_rows(rows):
    return pd.DataFrame([
        {'production_order_id': oid, 'operation_id': op, 'operation_sequence_number': seq,
         'sam': 2.0, 'target_quantity': 40, 'remaining_frac': 1.0,
         'required_machine_type': mtype}
        for oid, op, seq, mtype in rows
    ])


def _machine_rows(rows):
    return pd.DataFrame([
        {'asset_id': aid, 'factory_id': fid, 'line_id': lid, 'operation_type': otype}
        for aid, fid, lid, otype in rows
    ])


def _lines(line_ids):
    return pd.DataFrame([
        {'line_id': lid, 'is_active': 1, 'target_efficiency_percentage': 85.0}
        for lid in line_ids
    ])


def test_all_operations_of_an_order_are_confined_to_one_factory():
    # Both capabilities exist in BOTH factories, so the old company-wide
    # eligibility would have offered lines from each. Every operation of the
    # order must now land in the same factory.
    routings = _routing_rows([
        (1, 1, 1, 'single-needle lockstitch'),
        (1, 2, 2, 'overlock serger (4-thread)'),
    ])
    machines = _machine_rows([
        (1, 1, 10, 'body assembly'), (2, 1, 11, 'seam edge overlock'),
        (3, 2, 20, 'body assembly'), (4, 2, 21, 'seam edge overlock'),
    ])
    factory = ChromosomeFactory(routings, machines, _lines([10, 11, 20, 21]), legacy_mode='legacy_company_wide')

    chosen = factory.order_factory['1']
    for key in ('1_1', '1_2'):
        for line_id in factory.eligibility_map[key]:
            assert factory.line_factory[line_id] == chosen, (
                f"{key} eligible on line {line_id} outside the order's factory {chosen}")


def test_order_needing_capabilities_split_across_factories_is_infeasible():
    # Factory 1 has only body assembly, factory 2 only seam edge overlock.
    # Both capabilities exist company-wide, so this is NOT a missing-machine
    # failure -- it is infeasible specifically because of the one-factory
    # rule, and must say so rather than silently splitting the order.
    routings = _routing_rows([
        (1, 1, 1, 'single-needle lockstitch'),
        (1, 2, 2, 'overlock serger (4-thread)'),
    ])
    machines = _machine_rows([
        (1, 1, 10, 'body assembly'),
        (2, 2, 20, 'seam edge overlock'),
    ])
    with pytest.raises(ScheduleInfeasibleError, match="no single factory"):
        ChromosomeFactory(routings, machines, _lines([10, 20]), legacy_mode='legacy_company_wide')


def test_orders_are_distributed_across_feasible_factories_not_all_piled_on_one():
    # Deterministic least-loaded-per-capacity assignment: with two equally
    # capable factories and several orders, both must be used.
    routings = _routing_rows([(oid, 1, 1, 'single-needle lockstitch') for oid in range(1, 7)])
    machines = _machine_rows([
        (1, 1, 10, 'body assembly'), (2, 1, 11, 'body assembly'),
        (3, 2, 20, 'body assembly'), (4, 2, 21, 'body assembly'),
    ])
    factory = ChromosomeFactory(routings, machines, _lines([10, 11, 20, 21]), legacy_mode='legacy_company_wide')
    assert len(set(factory.order_factory.values())) > 1


def test_factory_assignment_is_deterministic_across_rebuilds():
    routings = _routing_rows([(oid, 1, 1, 'single-needle lockstitch') for oid in range(1, 7)])
    machines = _machine_rows([
        (1, 1, 10, 'body assembly'), (2, 2, 20, 'body assembly'),
    ])
    a = ChromosomeFactory(routings, machines, _lines([10, 20]), legacy_mode='legacy_company_wide').order_factory
    b = ChromosomeFactory(routings, machines, _lines([10, 20]), legacy_mode='legacy_company_wide').order_factory
    assert a == b


# ---------------------------------------------------------------------------
# FIX 4 -- export-time worker assignment is interval-feasible
# ---------------------------------------------------------------------------

NOW = datetime(2026, 1, 5, 8, 0, 0)


def _operator_index(employee_ids, line_id=10, optype='body assembly', factory_id=1):
    return {
        'by_line': {(line_id, optype): list(employee_ids)},
        'by_factory': {(factory_id, optype): list(employee_ids)},
        'by_skill': {optype: list(employee_ids)},
        'line_factory': {line_id: factory_id},
        'shift_of': {},
        'crew_of': {},
    }


def test_the_same_employee_is_not_assigned_two_overlapping_operations():
    index = _operator_index(['e1'])
    reservations, busy = {}, {}

    first, tier1, under1 = AIDecisionEngine._assign_operators(
        index, 'single-needle lockstitch', 10, 1, busy, 120.0,
        start_dt=NOW, operator_reservations=reservations)
    assert first == ['e1'] and under1 is None

    # Starts 60 minutes in, while e1 is still committed to the first.
    second, tier2, under2 = AIDecisionEngine._assign_operators(
        index, 'single-needle lockstitch', 10, 1, busy, 120.0,
        start_dt=NOW + timedelta(minutes=60), operator_reservations=reservations)
    assert second == [], "e1 was double-booked across overlapping operations"
    assert under2 is not None
    assert under2['required'] == 1 and under2['available'] == 0


def test_a_non_overlapping_later_operation_can_reuse_the_same_employee():
    index = _operator_index(['e1'])
    reservations, busy = {}, {}
    AIDecisionEngine._assign_operators(
        index, 'single-needle lockstitch', 10, 1, busy, 60.0,
        start_dt=NOW, operator_reservations=reservations)
    later, _tier, under = AIDecisionEngine._assign_operators(
        index, 'single-needle lockstitch', 10, 1, busy, 60.0,
        start_dt=NOW + timedelta(minutes=90), operator_reservations=reservations)
    assert later == ['e1']
    assert under is None


def test_employee_whose_shift_ends_mid_operation_is_not_eligible(monkeypatch):
    # Available at the start instant, unavailable an hour in.
    cutoff = NOW + timedelta(minutes=90)

    def fake_available(operator_index, employee_id, when):
        return when < cutoff

    monkeypatch.setattr(AIDecisionEngine, '_available_at', staticmethod(fake_available))
    index = _operator_index(['e1'])
    crew, _tier, under = AIDecisionEngine._assign_operators(
        index, 'single-needle lockstitch', 10, 1, {}, 240.0,
        start_dt=NOW, operator_reservations={})
    assert crew == [], "an operator whose shift ends mid-operation was still assigned"
    assert under is not None
    assert under['limiting_factor'] == 'shift'


def test_undersized_pool_reports_structured_understaffing_not_a_short_crew():
    index = _operator_index(['e1', 'e2'])
    crew, _tier, under = AIDecisionEngine._assign_operators(
        index, 'single-needle lockstitch', 10, 5, {}, 60.0,
        start_dt=NOW, operator_reservations={})
    # It still returns whoever is genuinely available -- but it must ALSO
    # report that this is short, which the old code never did.
    assert len(crew) == 2
    assert under is not None
    assert under['required'] == 5
    assert under['available'] == 2
    assert under['operation_type'] == 'body assembly'
    assert under['line_id'] == 10
    assert under['limiting_factor'] in {'skill', 'factory', 'shift', 'already-reserved'}


def test_no_skilled_operator_at_all_reports_skill_as_the_limiting_factor():
    index = _operator_index([])
    crew, tier, under = AIDecisionEngine._assign_operators(
        index, 'single-needle lockstitch', 10, 2, {}, 60.0,
        start_dt=NOW, operator_reservations={})
    assert crew == [] and tier == 'unfilled'
    assert under['limiting_factor'] == 'skill'


# ---------------------------------------------------------------------------
# Batch-4 fix 6 -- half-open intervals, no cross-factory, carried
# reservations, efficiency-aware selection among eligible candidates
# ---------------------------------------------------------------------------

def _index_with_efficiency(effs, line_id=10, optype='body assembly', factory_id=1):
    """effs: {employee_id: efficiency_rating}. All in ONE factory."""
    emps = list(effs)
    return {
        'by_line': {(line_id, optype): emps},
        'by_factory': {(factory_id, optype): emps},
        'by_skill': {optype: emps},
        'line_factory': {line_id: factory_id},
        'shift_of': {}, 'crew_of': {},
        'efficiency_of': {(e, optype): v for e, v in effs.items()},
    }


def test_operation_ending_exactly_at_shift_end_is_still_crewable(monkeypatch):
    # 6(a). Worker rostered 06:00-14:00, operation occupies exactly
    # 06:00-14:00. The old check probed the END instant (14:00), which is
    # the first tick of the next shift, and rejected a perfectly valid crew.
    shift_end = datetime(2026, 1, 5, 14, 0, 0)

    def rostered_until_1400(operator_index, employee_id, when):
        return when < shift_end

    monkeypatch.setattr(AIDecisionEngine, '_available_at',
                        staticmethod(rostered_until_1400))
    crew, _tier, under = AIDecisionEngine._assign_operators(
        _index_with_efficiency({'e1': 84.0}), 'single-needle lockstitch', 10, 1,
        {}, 480.0, start_dt=datetime(2026, 1, 5, 6, 0, 0),
        operator_reservations={})
    assert crew == ['e1'], "operation ending exactly at shift end was wrongly rejected"
    assert under is None


def test_cross_factory_candidate_is_a_shortage_not_a_fallback():
    # 6(b). The only skilled operator is indexed in factory 2; the operation
    # runs on a line in factory 1. Previously the 'anywhere' tier silently
    # crewed it. It must now be reported as a shortage.
    index = {
        'by_line': {}, 'by_factory': {(2, 'body assembly'): ['far']},
        'by_skill': {'body assembly': ['far']},
        'line_factory': {10: 1},
        'shift_of': {}, 'crew_of': {}, 'efficiency_of': {('far', 'body assembly'): 98.0},
    }
    crew, tier, under = AIDecisionEngine._assign_operators(
        index, 'single-needle lockstitch', 10, 1, {}, 60.0,
        start_dt=NOW, operator_reservations={})
    assert crew == [], "a worker from another factory was assigned"
    assert tier == 'unfilled'
    assert under is not None and under['limiting_factor'] == 'factory'


def test_reservations_passed_in_are_honoured_across_calls():
    # 6(c). A reservation made in a previous window must still block here.
    prior = {'e1': [(NOW, NOW + timedelta(minutes=120))]}
    crew, _tier, under = AIDecisionEngine._assign_operators(
        _index_with_efficiency({'e1': 84.0}), 'single-needle lockstitch', 10, 1,
        {}, 60.0, start_dt=NOW + timedelta(minutes=30),
        operator_reservations=prior)
    assert crew == [], "a carried-forward reservation did not block re-assignment"
    assert under is not None


def test_higher_efficiency_wins_among_otherwise_equal_candidates():
    # 6(d). Same load (both idle), same eligibility -> skill decides.
    index = _index_with_efficiency({'slow': 70.0, 'fast': 98.0})
    crew, _tier, _under = AIDecisionEngine._assign_operators(
        index, 'single-needle lockstitch', 10, 1, {}, 60.0,
        start_dt=NOW, operator_reservations={}, order_priority=1.0)
    assert crew == ['fast']


def test_efficiency_never_overrides_eligibility():
    # The best operator in the plant is already reserved for this window.
    # Skill must not buy them a second booking.
    index = _index_with_efficiency({'slow': 70.0, 'fast': 98.0})
    reserved = {'fast': [(NOW, NOW + timedelta(hours=4))]}
    crew, _tier, _under = AIDecisionEngine._assign_operators(
        index, 'single-needle lockstitch', 10, 1, {}, 60.0,
        start_dt=NOW, operator_reservations=reserved, order_priority=1.0)
    assert crew == ['slow'], "an ineligible high-efficiency operator was selected"


def test_load_balance_still_moves_selection_when_efficiency_is_tied():
    index = _index_with_efficiency({'busy': 84.0, 'idle': 84.0})
    crew, _tier, _under = AIDecisionEngine._assign_operators(
        index, 'single-needle lockstitch', 10, 1,
        {'busy': 5000.0, 'idle': 0.0}, 60.0,
        start_dt=NOW, operator_reservations={}, order_priority=1.0)
    assert crew == ['idle']


@pytest.mark.parametrize('priority,expected', [(0.0, 'slow'), (0.5, 'slow'), (1.0, 'fast')])
@pytest.mark.parametrize('committed', [10_000.0, 1_000_000_000.0])
def test_efficiency_load_tradeoff_across_priority_range(priority, expected, committed):
    # Actual policy, not a claimed starvation guarantee. Absolute load cannot
    # grow beyond one after dividing by the maximum candidate load.
    index = _index_with_efficiency({'slow': 70.0, 'fast': 98.0})
    crew, _tier, _under = AIDecisionEngine._assign_operators(
        index, 'single-needle lockstitch', 10, 1,
        {'fast': committed, 'slow': 0.0}, 60.0,
        start_dt=NOW, operator_reservations={}, order_priority=priority)
    assert crew == [expected]


def test_load_balance_behaviour_across_the_full_priority_range():
    """The load/skill trade-off, probed at BOTH ends and the middle.

    An earlier test asserted "a heavily loaded worker eventually loses" using
    priority 0.0 only -- the one region where that holds -- and the claim was
    then cited as general. It is not: load_term is a ratio to the BUSIEST
    candidate, so the busiest is pinned at 1 however large its load, and
    above W=0.5 skill wins outright. Both ends are pinned here so the claim
    cannot drift back into the comments unnoticed.
    """
    index = _index_with_efficiency({'slow': 70.0, 'fast': 98.0})

    def winner(priority):
        crew, _t, _u = AIDecisionEngine._assign_operators(
            index, 'single-needle lockstitch', 10, 1,
            {'fast': 1_000_000_000.0, 'slow': 0.0}, 60.0,
            start_dt=NOW, operator_reservations={}, order_priority=priority)
        return crew

    # Low/mid priority: load dominates, the saturated expert is passed over.
    assert winner(0.0) == ['slow']
    assert winner(0.5) == ['slow']
    # High priority: skill dominates and load magnitude is irrelevant. This
    # is the DOCUMENTED LIMITATION, asserted rather than wished away.
    assert winner(1.0) == ['fast'], (
        "if this now returns ['slow'], a fairness constraint was added -- "
        "update _select_crew's docstring, which documents the opposite as a "
        "known limitation")


def test_raising_load_further_cannot_flip_the_high_priority_choice():
    # The mechanism behind the limitation: normalisation pins the busiest
    # candidate's load_term at 1, so scaling its load by 1000x changes
    # nothing. If this ever fails, load is no longer ratio-to-max and the
    # docstring's analysis needs redoing.
    index = _index_with_efficiency({'slow': 70.0, 'fast': 98.0})
    for load in (1e6, 1e9, 1e12):
        crew, _t, _u = AIDecisionEngine._assign_operators(
            index, 'single-needle lockstitch', 10, 1,
            {'fast': load, 'slow': 0.0}, 60.0,
            start_dt=NOW, operator_reservations={}, order_priority=1.0)
        assert crew == ['fast'], f"load {load} unexpectedly flipped the choice"
