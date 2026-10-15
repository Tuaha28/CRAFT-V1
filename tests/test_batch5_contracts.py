"""Batch 5 regression tests of real consumers, not claimed guarantees."""
from datetime import datetime, timedelta
import json
import pandas as pd
import numpy as np
import pytest
from sqlalchemy import text
from unittest.mock import Mock
from utils.factory_ownership import OwnershipSnapshot, resolve_ownership, FactoryOwnershipError
from utils.run_artifacts import stamp_artifact, require_same_run, RunIdentityError
from graph.edge_builder import build_disjunctive_edges
from graph.node_features import _aggregate_dispatch_context
from ga.chromosome import ChromosomeFactory
from test_factory_ownership import _frames
from test_schedule_exporter import _build_scenario
from test_local_patch_row_identity import store, assessment, T0
import agent.impact_assessor as impact
from agent.decision_engine import AIDecisionEngine


def test_ownership_read_failure_aborts_both_consumers(monkeypatch):
    read = Mock(side_effect=RuntimeError('ownership read failed'))
    monkeypatch.setattr('utils.factory_ownership.load_factory_ownership', read)
    routings, machines, lines = _frames()
    with pytest.raises(RuntimeError, match='ownership read failed'):
        ChromosomeFactory(routings, machines, lines)
    with pytest.raises(RuntimeError, match='ownership read failed'):
        build_disjunctive_edges(routings, {'1_1': 0})


def test_one_ownership_read_shared_by_graph_and_ga(monkeypatch):
    read = Mock(return_value={'1': 2})
    monkeypatch.setattr('utils.factory_ownership.load_factory_ownership', read)
    snapshot = resolve_ownership(['1'])
    routings, machines, lines = _frames()
    factory = ChromosomeFactory(routings, machines, lines, ownership_snapshot=snapshot)
    build_disjunctive_edges(routings, {'1_1': 0}, ownership_snapshot=snapshot)
    assert factory.ownership_snapshot is snapshot
    assert factory.order_factory == {'1': 2}
    assert read.call_count == 1


def test_partial_ownership_aborts_graph_and_ga():
    routings, machines, lines = _frames()
    missing = OwnershipSnapshot((('other', 1),))
    with pytest.raises(FactoryOwnershipError):
        ChromosomeFactory(routings, machines, lines, ownership_snapshot=missing)
    with pytest.raises(FactoryOwnershipError):
        build_disjunctive_edges(routings, {'1_1': 0}, ownership_snapshot=missing)


def test_delayed_lot_still_occupies_resource(store):
    engine, table, rows = store
    with engine.begin() as conn:
        conn.execute(table.update().where(table.c.schedule_item_id == 1).values(execution_status='Delayed'))
        conn.execute(table.update().where(table.c.schedule_item_id == 3).values(asset_id=3000))
    before = rows()
    proposed = assessment(before[2])
    proposed.update(candidate_asset_id=2025, candidate_line_id=10, candidate_start_time=T0)
    assert not impact.apply_local_patch(proposed)
    assert proposed['patch_rejection']['blocking_schedule_item_ids'] == [1]
    assert rows() == before


def test_unobserved_decisive_lot_makes_context_unavailable():
    rows = pd.DataFrame([
        dict(production_order_id='1', operation_id='1', employee_on_leave_at_dispatch=0,
             queue_delay_mins_at_dispatch=4., machine_breakdown_count_at_dispatch=2., planned_end_time=T0),
        dict(production_order_id='1', operation_id='1', employee_on_leave_at_dispatch=None,
             queue_delay_mins_at_dispatch=None, machine_breakdown_count_at_dispatch=None,
             planned_end_time=T0+timedelta(hours=1)),
    ])
    row = _aggregate_dispatch_context(rows).iloc[0]
    assert row.context_observed == 0
    assert pd.isna(row.mean_queue_delay_mins)
    assert pd.isna(row.decisive_lot_breakdown_count_at_dispatch)


def test_two_real_exports_carry_worker_reservations(tmp_path, monkeypatch):
    """The real exporter carries productive segments and waits for them.

    This supersedes the Batch-5 expectation that any overlap made the second
    export permanently understaffed. Reservations are temporary calendar
    exclusions under the authoritative same-worker pause/resume decoder.
    """
    monkeypatch.setattr('agent.decision_engine.OUTPUT_DIR', str(tmp_path))
    engine = AIDecisionEngine()
    W = 7001
    index = {'by_line': {(1,'body assembly'): [W]},
             'by_factory': {(1,'body assembly'): [W]},
             'by_skill': {'body assembly': [W]}, 'line_factory': {1: 1},
             'shift_of': {}, 'crew_of': {},
             'efficiency_of': {(W,'body assembly'):80.}}

    def export(order, reservations):
        kwargs = _build_scenario({order: {'num_ops': 1}})
        kwargs['df_routings']['required_machine_type'] = (
            'single-needle lockstitch'
        )
        kwargs.update(
            operator_index=index,
            base_time=T0,
            initial_operator_reservations=reservations,
            label=order,
        )
        return engine._export_proposed_schedule(**kwargs)

    first = export('1001', {})
    assert first['understaffed_operations'] == 0
    first_end = first['operator_reservations_out'][str(W)][-1][1]

    second = export('1002', first['operator_reservations_out'])
    assert second['understaffed_operations'] == 0
    assert second['execution_feasible']
    assert set(second['operator_reservations_out']) == {str(W)}
    second_intervals = second['operator_reservations_out'][str(W)]
    assert second_intervals[-1][1] > first_end
    # ONE key namespace (str) for both reservations and busy-minute state.
    assert set(second['operator_busy_minutes_out']) == {str(W)}
    assert (second['operator_busy_minutes_out'][str(W)]
            > first['operator_busy_minutes_out'][str(W)])

def test_unknown_affected_order_is_visible_with_one_timestamp(monkeypatch):
    class Clock(datetime):
        calls = 0
        @classmethod
        def now(cls):
            cls.calls += 1
            return T0
    monkeypatch.setattr(impact, 'datetime', Clock)
    row = dict(schedule_item_id=42, production_order_id='unknown', operation_id=1,
               asset_id=1, line_id=10, owned_factory_id=1, planned_start_time=T0, planned_end_time=T0+timedelta(hours=1))
    monkeypatch.setattr(impact, '_get_affected_operations', lambda aid: pd.DataFrame([row]))
    monkeypatch.setattr(impact, 'require_factory_ownership', lambda ids: {'unknown': 1})
    monkeypatch.setattr(impact, '_get_machine_operation_type', lambda aid: 'body assembly')
    monkeypatch.setattr(impact, '_get_alternate_machines', lambda *a: pd.DataFrame(columns=['asset_id']))
    monkeypatch.setattr(impact, '_get_reserve_context', lambda: (set(), {}))
    monkeypatch.setattr(impact, 'fetch_machine_state', lambda: pd.DataFrame(
        columns=['asset_id', 'line_id', 'factory_id']))
    monkeypatch.setattr(impact, '_get_committed_schedules_bulk', lambda *a: {})
    monkeypatch.setattr(impact, '_get_order_slack_days_bulk', lambda *a, **k: {})
    result = impact.assess_machine_breakdown(1)
    assert result[0]['assessment_status'] == 'cannot_assess_safely'
    assert result[0]['schedule_item_id'] == 42
    assert Clock.calls == 1


def test_report_rejects_mixed_and_stale_list_artifacts(tmp_path):
    schedule, completions = tmp_path/'schedule.json', tmp_path/'completions.json'
    for p in (schedule, completions): p.write_text('[]')
    stamp_artifact(schedule, 'run-a'); stamp_artifact(completions, 'run-b')
    with pytest.raises(RunIdentityError, match='different runs'):
        require_same_run([schedule, completions])
    stamp_artifact(completions, 'run-a')
    assert require_same_run([schedule, completions]) == 'run-a'
    schedule.write_text('[{}]')
    with pytest.raises(RunIdentityError, match='no longer match'):
        require_same_run([schedule, completions])


def test_empty_valid_heads_are_unavailable(monkeypatch):
    import models.evaluate as evaluate
    monkeypatch.setattr(evaluate, '_load_model_and_val_predictions', lambda *a: {
        'delay_probs': np.array([]), 'delay_true': np.array([]), 'n_delay_masked': 2,
        'completion_pred': np.empty((0,1)), 'completion_true': np.empty((0,1)), 'n_completion_masked': 2,
        'priority_pred': np.array([[.5]]), 'priority_true': np.array([[.5]])})
    metrics = evaluate.evaluate_gnn('unused')
    assert metrics['completion_mae_minutes'] is None
    assert metrics['completion_labelled_operations'] == 0
    assert metrics['delay_accuracy'] is None
    assert evaluate.compute_delay_curves('unused') is None
