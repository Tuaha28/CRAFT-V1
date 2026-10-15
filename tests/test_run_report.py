"""
Tests for the human-readable run report.

Uses fabricated data throughout -- no database, no live pipeline state, and
no reliance on this repo's real outputs/ directory -- so this suite stays
fast, independent, and deterministic. What it checks: the narrative is built
from EXACTLY the data handed to it (no invented numbers or order IDs), every
section degrades gracefully when its source data is absent, and the
tier-code -> plain-English translation and singular/plural wording are
correct.
"""

import json
import os

from reporting.run_report import generate_run_report


FAKE_COMPARISON = {
    'ga_aggregate': {'orders': 500, 'makespan_days': 100.0},
    'overall_capacity_floor_days': 100.0,
    'per_window': {
        'GA': [
            {'window': 1, 'classification': 'dense',
             'margin_pct_vs_best_of_5_heuristics': 9.0,
             'capacity_floor_ratio_cum_makespan_over_floor': None},
            {'window': 2, 'classification': 'dense',
             'margin_pct_vs_best_of_5_heuristics': 5.0,
             'capacity_floor_ratio_cum_makespan_over_floor': 3.4},
            {'window': 3, 'classification': 'slack',
             'margin_pct_vs_best_of_5_heuristics': 0.2,
             'capacity_floor_ratio_cum_makespan_over_floor': 1.0},
        ]
    },
}

FAKE_SCHEDULE = [
    {'production_order_id': '100', 'operation_id': '1', 'assigned_line_number': 'LINE01',
     'machines_used': 2, 'operators_used': 2, 'operator_sourcing_tier': 'factory',
     'proposed_start_time': '2026-01-01T00:00:00', 'base_end_time': '2026-01-02T00:00:00',
     'cushioned_end_time': '2026-01-02T00:00:00', 'xai_reason': 'Assigned to LINE01 because it was idle.'},
    {'production_order_id': '100', 'operation_id': '2', 'assigned_line_number': 'LINE02',
     'machines_used': 1, 'operators_used': 1, 'operator_sourcing_tier': 'batch',
     'proposed_start_time': '2026-01-02T00:00:00', 'base_end_time': '2026-01-03T00:00:00',
     'cushioned_end_time': '2026-01-03T00:00:00', 'xai_reason': 'Assigned to LINE02, the only line free.'},
    {'production_order_id': '200', 'operation_id': '3', 'assigned_line_number': 'LINE03',
     'machines_used': 3, 'operators_used': 3, 'operator_sourcing_tier': 'factory',
     'proposed_start_time': '2026-01-01T00:00:00', 'base_end_time': '2026-01-05T00:00:00',
     'cushioned_end_time': '2026-01-05T00:00:00', 'xai_reason': 'Assigned to LINE03 despite heavy load.'},
]

FAKE_COMPLETIONS = [
    {'production_order_id': '100', 'predicted_completion': '2026-01-03T00:00:00',
     'existing_committed_deadline': '2026-01-10T00:00:00', 'deadline_already_passed': False,
     'breach_risk': False, 'late_by_hours': None, 'schedule_added_late_hours': None},
    {'production_order_id': '200', 'predicted_completion': '2026-01-05T00:00:00',
     'existing_committed_deadline': '2025-12-01T00:00:00', 'deadline_already_passed': False,
     'breach_risk': True, 'late_by_hours': 840.0, 'schedule_added_late_hours': 840.0},
    {'production_order_id': '300', 'predicted_completion': '2026-01-06T00:00:00',
     'existing_committed_deadline': '2025-11-01T00:00:00', 'deadline_already_passed': True,
     'breach_risk': True, 'late_by_hours': 1200.0, 'schedule_added_late_hours': 1200.0},
]

FAKE_BREAKDOWN_AGGREGATE = {
    'asset': 55, 'affected_operations': 3, 'tiers': {'NO_ACTION': 2, 'LOCAL_PATCH': 1},
    'covered_from_reserve': 0, 'donor_preemptions': 0, 'max_impact_hours': 4.5,
}

FAKE_BREAKDOWN_LIST = [
    {'original_asset_id': 55, 'tier': 'NO_ACTION', 'impact_hours': 0.0,
     'from_standby_reserve': False, 'donor_plan': None},
    {'original_asset_id': 55, 'tier': 'FULL_RESCHEDULE', 'impact_hours': 12.0,
     'from_standby_reserve': False, 'donor_plan': {'donor_order': '999'}},
]


def _write(tmp_path, name, data):
    path = os.path.join(str(tmp_path), name)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f)
    from utils.run_artifacts import stamp_artifact
    stamp_artifact(path, "test-report-run")
    return path


def _generate(tmp_path, comparison=FAKE_COMPARISON, schedule=FAKE_SCHEDULE,
             completions=FAKE_COMPLETIONS, breakdown_path=None, comparison_path=None,
             schedule_path=None, completions_path=None):
    # pipeline_report_path always pinned inside tmp_path -- never let a test
    # accidentally fall back to this repo's real outputs/final_run_report.json.
    return generate_run_report(
        comparison_path=comparison_path or _write(tmp_path, 'comparison.json', comparison),
        schedule_path=schedule_path or _write(tmp_path, 'schedule.json', schedule),
        completions_path=completions_path or _write(tmp_path, 'completions.json', completions),
        breakdown_path=breakdown_path,
        pipeline_report_path=os.path.join(str(tmp_path), 'no_pipeline_report.json'),
        out_path=os.path.join(str(tmp_path), 'r.md'),
    )


def test_report_generates_and_writes_file(tmp_path):
    out_path = os.path.join(str(tmp_path), 'run_report.md')
    text = generate_run_report(
        comparison_path=_write(tmp_path, 'comparison.json', FAKE_COMPARISON),
        schedule_path=_write(tmp_path, 'schedule.json', FAKE_SCHEDULE),
        completions_path=_write(tmp_path, 'completions.json', FAKE_COMPLETIONS),
        pipeline_report_path=os.path.join(str(tmp_path), 'no_pipeline_report.json'),
        out_path=out_path,
    )
    assert os.path.exists(out_path)
    with open(out_path, encoding='utf-8') as f:
        assert f.read() == text


def test_executive_summary_uses_given_numbers_not_recomputed(tmp_path):
    text = _generate(tmp_path)
    assert "3 orders" in text
    assert "1 (33%)" in text  # exactly 1 of 3 fake orders is on time
    assert "500 orders" in text  # ga_aggregate['orders'], not the 3 fake completions
    assert "1.00x" in text  # makespan_days == overall_capacity_floor_days


def test_most_at_risk_order_is_the_largest_overrun(tmp_path):
    text = _generate(tmp_path)
    # order 300 has late_by_hours=1200 > order 200's 840
    assert "order 300 is the least safe order" in text
    at_risk_block = text.split("**Most at-risk order**")[1].split("**A typical order**")[0]
    assert "Its final operation runs on" not in at_risk_block  # order 300 has no ops in FAKE_SCHEDULE


def test_no_at_risk_orders_states_so_plainly(tmp_path):
    safe = [dict(FAKE_COMPLETIONS[0])]
    text = _generate(tmp_path, completions=safe)
    assert "no order in this run is currently predicted to miss its committed deadline" in text


def test_infeasible_orders_flagged_explicitly(tmp_path):
    text = _generate(tmp_path)
    assert "1 order(s) already had a deadline in the past" in text
    assert "300" in text.split("Infeasible orders")[1]


def test_no_infeasible_orders_states_so_plainly(tmp_path):
    feasible = [dict(o) for o in FAKE_COMPLETIONS if not o['deadline_already_passed']]
    text = _generate(tmp_path, completions=feasible)
    assert "Infeasible orders** -- none" in text


def test_no_breakdown_omits_disruption_section(tmp_path):
    text = _generate(tmp_path)
    assert "Disruption narrative" not in text


def test_aggregate_breakdown_translates_tier_codes_to_plain_english(tmp_path):
    breakdown = _write(tmp_path, 'breakdown.json', FAKE_BREAKDOWN_AGGREGATE)
    text = _generate(tmp_path, breakdown_path=breakdown)
    assert "Disruption narrative" in text
    assert "NO_ACTION" not in text
    assert "LOCAL_PATCH" not in text
    assert "2 operation(s) were absorbed by another machine" in text
    assert "1 operation(s) was moved to a different machine" in text
    assert "never needed the standby-reserve or donor-preemption logic" in text


def test_list_breakdown_reports_donor_preemption(tmp_path):
    breakdown = _write(tmp_path, 'breakdown.json', FAKE_BREAKDOWN_LIST)
    text = _generate(tmp_path, breakdown_path=breakdown)
    assert "required taking a slot from another order's schedule (donor preemption)" in text
    assert "FULL_RESCHEDULE" not in text


def test_caveats_flag_null_capacity_floor_window(tmp_path):
    text = _generate(tmp_path)
    assert "Honest caveats" in text
    assert "window(s) 1" in text


def test_missing_comparison_report_does_not_crash(tmp_path):
    text = _generate(tmp_path, comparison_path=os.path.join(str(tmp_path), 'missing.json'))
    assert "Executive summary" in text
    assert "Order-by-order highlights" in text
