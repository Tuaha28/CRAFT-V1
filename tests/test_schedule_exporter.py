"""
Tests for the schedule exporter (agent/decision_engine.py::
AIDecisionEngine._export_proposed_schedule / _export_order_completions) --
the piece of orchestration that turns a winning GA chromosome into the JSON
files an operator/manager actually reads. Zero prior coverage: everything
else in tests/ exercises the GA that PRODUCES the chromosome, nothing
exercised what happens to it after.

Each operation gets its OWN dedicated machine id (never shared across
operations) so no two operations ever contend for a machine -- this makes
every operation's start/end time exactly predictable from
setup_mins + serial_processing_mins alone, which is what the deadline/
lateness tests below depend on. SHIFT_START_HOUR/SHIFT_END_HOUR are 0/24 in
this project's config (utils/config.py) -- a real no-op shift window -- so
there is no start-time snapping to account for either.

AIDecisionEngine() is safe to construct directly: __init__ does no DB I/O
(see agent/decision_engine.py -- it only sets model_path and an empty
manager-override set), same premise tests/test_rolling_horizon_integration.py
already relies on.
"""
import json
import os
from datetime import datetime, timedelta

import pandas as pd
import pytest

import agent.decision_engine as decision_engine_module
from agent.decision_engine import AIDecisionEngine

SETUP_MINS = 15.0
SERIAL_MINS = 60.0  # duration per op at machine_count=1: 15 + 60/1 = 75 min


@pytest.fixture(autouse=True)
def _redirect_output_dir(tmp_path, monkeypatch):
    """Every test in this file writes through OUTPUT_DIR -- redirected to a
    pytest tmp_path so nothing ever touches the real outputs/ directory."""
    monkeypatch.setattr(decision_engine_module, "OUTPUT_DIR", str(tmp_path))
    return tmp_path


def _build_scenario(orders):
    """
    orders: {order_id: {'num_ops': int, 'deadline': datetime|None}}

    Every operation of every order gets its own dedicated machine (asset id
    derived from a running counter), one line, no risk padding (empty
    gnn_preds -> delay=0.0 default), machine_count=1 throughout. Returns the
    exact kwargs _export_proposed_schedule expects.
    """
    op_details, routings_rows, machine_pools, eligibility_map = {}, [], {}, {}
    os_array, la_array, mc_array = [], [], []
    order_deadline_abs = {}
    asset_counter = 1000

    for order_id, cfg in orders.items():
        order_deadline_abs[order_id] = cfg.get('deadline')
        for op_id in range(1, cfg['num_ops'] + 1):
            op_key = f"{order_id}_{op_id}"
            op_details[op_key] = {'setup_mins': SETUP_MINS, 'serial_processing_mins': SERIAL_MINS}
            routings_rows.append({
                'production_order_id': order_id, 'operation_id': op_id,
                'operation_sequence_number': op_id,
                'required_machine_type': 'single-needle lockstitch',
            })
            asset_counter += 1
            machine_pools[op_key] = {1: [asset_counter]}
            eligibility_map[op_key] = [1]
            os_array.append(op_key)
            la_array.append(1)
            mc_array.append(1)

    chromosome = os_array + la_array + mc_array
    df_routings = pd.DataFrame(routings_rows)
    df_lines = pd.DataFrame({'line_id': [1], 'line_number': ['L01']})
    df_machines = pd.DataFrame({
        'asset_id': list(range(1001, asset_counter + 1)),
        'line_id': [1] * (asset_counter - 1000),
    })

    employees = list(range(7001, 7001 + len(os_array)))
    operator_index = {
        'by_line': {(1, 'body assembly'): employees},
        'by_factory': {(1, 'body assembly'): employees},
        'by_skill': {'body assembly': employees},
        'line_factory': {1: 1},
        'shift_of': {},
        'crew_of': {},
        'efficiency_of': {
            (employee, 'body assembly'): 84.0 for employee in employees
        },
    }
    return dict(
        chromosome=chromosome, num_ops=len(os_array), op_details=op_details,
        gnn_preds={}, df_lines=df_lines, eligibility_map=eligibility_map,
        order_deadline_abs=order_deadline_abs, df_routings=df_routings,
        df_machines=df_machines, machine_pools=machine_pools,
        usable_machine_count=len(df_machines), present_operator_count=10,
        operator_index=operator_index,
    )


def _export(orders, label=None):
    engine = AIDecisionEngine()
    kwargs = _build_scenario(orders)
    summary = engine._export_proposed_schedule(label=label, **kwargs)
    return engine, summary, kwargs


# ---------------------------------------------------------------------------
# proposed_schedule.json: completeness and per-operation shape
# ---------------------------------------------------------------------------

def test_every_operation_produces_exactly_one_entry_no_drops_or_duplicates():
    _, summary, kwargs = _export({'1001': {'num_ops': 2}, '1002': {'num_ops': 3}})
    with open(summary['schedule_file']) as f:
        proposed_changes = json.load(f)
    assert len(proposed_changes) == kwargs['num_ops'] == 5
    seen = {(e['production_order_id'], e['operation_id']) for e in proposed_changes}
    expected = {(str(o), str(i)) for o, cfg in [('1001', 2), ('1002', 3)] for i in range(1, cfg + 1)}
    assert seen == expected


def test_ids_are_strings_in_the_exported_json():
    _, summary, _ = _export({'1001': {'num_ops': 1}})
    with open(summary['schedule_file']) as f:
        entry = json.load(f)[0]
    assert isinstance(entry['production_order_id'], str)
    assert isinstance(entry['operation_id'], str)


def test_timestamps_are_internally_consistent_per_operation():
    _, summary, _ = _export({'1001': {'num_ops': 2}})
    with open(summary['schedule_file']) as f:
        proposed_changes = json.load(f)
    for e in proposed_changes:
        start = datetime.fromisoformat(e['proposed_start_time'])
        base_end = datetime.fromisoformat(e['base_end_time'])
        cushioned_end = datetime.fromisoformat(e['cushioned_end_time'])
        assert start <= base_end <= cushioned_end


def test_second_operation_of_an_order_starts_no_earlier_than_the_first_ends():
    _, summary, _ = _export({'1001': {'num_ops': 2}})
    with open(summary['schedule_file']) as f:
        proposed_changes = json.load(f)
    op1 = next(e for e in proposed_changes if e['operation_id'] == '1')
    op2 = next(e for e in proposed_changes if e['operation_id'] == '2')
    assert datetime.fromisoformat(op2['proposed_start_time']) >= datetime.fromisoformat(op1['cushioned_end_time'])


def test_label_suffixes_both_output_filenames():
    _, summary, _ = _export({'1001': {'num_ops': 1}}, label='w2')
    assert summary['schedule_file'].endswith('proposed_schedule_w2.json')
    assert os.path.exists(summary['schedule_file'])
    order_completions_path = os.path.join(os.path.dirname(summary['schedule_file']), 'order_completions_w2.json')
    assert os.path.exists(order_completions_path)


def test_no_label_leaves_filenames_unsuffixed():
    _, summary, _ = _export({'1001': {'num_ops': 1}})
    assert summary['schedule_file'].endswith('proposed_schedule.json')
    assert 'proposed_schedule_' not in os.path.basename(summary['schedule_file'])


# ---------------------------------------------------------------------------
# Order-level rollup: final operation is the highest SEQUENCE NUMBER seen,
# not whichever operation happened to be visited last in os_array. This is
# the documented contract in _export_proposed_schedule's own docstring.
# ---------------------------------------------------------------------------

def test_export_rejects_out_of_precedence_operation_visit_order():
    engine = AIDecisionEngine()
    kwargs = _build_scenario({'1001': {'num_ops': 3}})
    n = kwargs['num_ops']
    kwargs['chromosome'] = (
        ['1001_3', '1001_1', '1001_2']
        + kwargs['chromosome'][n:2 * n]
        + kwargs['chromosome'][2 * n:]
    )
    with pytest.raises(RuntimeError, match='precedence violated'):
        engine._export_proposed_schedule(label=None, **kwargs)


# ---------------------------------------------------------------------------
# Lateness classification: "already overdue before this schedule" vs
# "made late BY this schedule" vs "on time" must stay genuinely distinct --
# PROJECT_STATUS.txt documents this file previously conflating them into one
# unreadable headline number (450,827 lateness-hours).
# ---------------------------------------------------------------------------

def test_on_time_order_is_not_flagged_late_in_any_way():
    now = datetime.now()
    _, summary, _ = _export({'1001': {'num_ops': 1, 'deadline': now + timedelta(days=30)}})
    with open(os.path.join(os.path.dirname(summary['schedule_file']), 'order_completions.json')) as f:
        completions = json.load(f)
    row = completions[0]
    assert row['deadline_already_passed'] is False
    assert row['breach_risk'] is False
    assert row['late_by_hours'] is None
    assert row['schedule_added_late_hours'] == 0.0
    assert summary['on_time_rate'] == 1.0
    assert summary['orders_made_late_by_this_schedule'] == 0
    assert summary['orders_already_past_deadline'] == 0


def test_already_overdue_order_reports_lateness_clamped_to_now_not_to_the_stale_date():
    # Deadline 10 days in the PAST -- already overdue before this schedule
    # existed. late_by_hours (vs. the RAW committed deadline) reflects the
    # full ~10-day gap; schedule_added_late_hours is deliberately computed
    # against effective_deadline = max(committed_deadline, now) instead (the
    # same clamp ga/fitness.py's objective uses -- see the source comment),
    # so it reflects only the order's own ~150 min of remaining work, not
    # the stale 10-day backlog. The two numbers must therefore differ hugely.
    now = datetime.now()
    _, summary, _ = _export({'1001': {'num_ops': 2, 'deadline': now - timedelta(days=10)}})
    with open(os.path.join(os.path.dirname(summary['schedule_file']), 'order_completions.json')) as f:
        completions = json.load(f)
    row = completions[0]
    assert row['deadline_already_passed'] is True
    assert row['breach_risk'] is True
    # ~10 days (240h) plus this order's own 150 min (2.5h) of real remaining
    # work before it completes, measured against the RAW committed deadline.
    assert row['late_by_hours'] == pytest.approx(242.5, abs=0.1)
    # 2 ops x 75 min (setup 15 + serial 60, no risk buffer) = 150 min = 2.5h,
    # measured from NOW (the clamped floor), not from 10 days ago.
    assert row['schedule_added_late_hours'] == pytest.approx(2.5, abs=0.05)
    assert summary['orders_already_past_deadline'] == 1
    # This order's own not-yet-done work necessarily completes after "now"
    # (nothing can finish in zero time), so by the SAME clamped-to-now
    # definition ga/fitness.py's objective uses, it also counts here --
    # this is deliberate, not a double-count bug: the two counters answer
    # different questions ("was it already gone before we started" vs
    # "does the remaining work alone still push past what's achievable now").
    assert summary['orders_made_late_by_this_schedule'] == 1


def test_schedule_caused_lateness_is_flagged_when_deadline_is_future_but_too_soon():
    # Deadline 30 minutes from now -- NOT already overdue (it's in the
    # future) -- but the order's two 75-min operations (150 min total)
    # finish well after it, so THIS schedule is what breaches it.
    now = datetime.now()
    _, summary, _ = _export({'1001': {'num_ops': 2, 'deadline': now + timedelta(minutes=30)}})
    with open(os.path.join(os.path.dirname(summary['schedule_file']), 'order_completions.json')) as f:
        completions = json.load(f)
    row = completions[0]
    assert row['deadline_already_passed'] is False
    assert row['breach_risk'] is True
    assert row['schedule_added_late_hours'] > 0
    assert summary['orders_already_past_deadline'] == 0
    assert summary['orders_made_late_by_this_schedule'] == 1
    assert summary['on_time_rate'] == 0.0


def test_orders_without_a_deadline_are_excluded_from_on_time_rate_not_counted_against_it():
    _, summary, _ = _export({
        '1001': {'num_ops': 1, 'deadline': None},
        '1002': {'num_ops': 1, 'deadline': datetime.now() + timedelta(days=30)},
    })
    assert summary['orders_with_deadline'] == 1  # only 1002 counts
    assert summary['on_time_rate'] == 1.0  # the one WITH a deadline made it


def test_on_time_rate_reflects_a_mix_of_late_and_on_time_orders():
    now = datetime.now()
    _, summary, _ = _export({
        '1001': {'num_ops': 2, 'deadline': now + timedelta(minutes=30)},  # will breach
        '1002': {'num_ops': 1, 'deadline': now + timedelta(days=30)},     # comfortably on time
    })
    assert summary['orders_with_deadline'] == 2
    assert summary['orders_made_late_by_this_schedule'] == 1
    assert summary['on_time_rate'] == pytest.approx(0.5)
