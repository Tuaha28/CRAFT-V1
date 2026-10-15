"""
The capacity floor must be built from the SCHEDULER's work representation and
must report its two components separately.

Previously it recomputed work as sam*quantity/efficiency, omitting
DURATION_CALIBRATION_FACTOR, which ga/scheduler.py:276 applies -- describing a
problem 1/0.825454 = 1.2114x slower than the one being optimised. A bound from
a slower problem is not necessarily a lower bound on this one, so the "1.00x
the floor" near-optimality reading it produced was unjustified.

It also returned max(workload, critical_path) as one number. Those answer
different questions: only the workload bound speaks to machine capacity, while
the critical-path bound can bind with capacity idle. Conflating them is how a
release-date-driven bound came to be read as capacity saturation.
"""

import importlib.util
import pathlib
import sys

import pandas as pd
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_floor_fn(monkeypatch):
    """Import compute_capacity_floor_days without executing the script's
    module-level pipeline (which would hit the database and run a full GA)."""
    src = (ROOT / 'run_authoritative_comparison.py').read_text(encoding='utf-8')
    start = src.index('def compute_capacity_floor_days')
    end = src.index('\nn_windows = len(', start)
    ns = {'pd': pd, 'MINS_PER_DAY': 1440.0, 'DEFAULT_SETUP_TIME_MINS': 15.0,
          'GA_MAX_MACHINES_PER_OPERATION': 40, 'DURATION_CALIBRATION_FACTOR': 0.825454}
    exec(compile(src[start:end], 'floor_fragment', 'exec'), ns)
    return ns['compute_capacity_floor_days']


class _Factory:
    def __init__(self):
        self.optype_for_op = {'1_1': 'sew', '1_2': 'sew', '2_1': 'sew'}
        self.machines_per_line_map = {k: {1: 4} for k in self.optype_for_op}
        self.usable_cap_by_type = {'sew': 10}


def _routings():
    return pd.DataFrame([
        {'production_order_id': '1', 'operation_id': '1', 'sam': 10.0,
         'target_quantity': 100, 'remaining_frac': 1.0, 'release_date': '2026-01-01'},
        {'production_order_id': '1', 'operation_id': '2', 'sam': 10.0,
         'target_quantity': 100, 'remaining_frac': 1.0, 'release_date': '2026-01-01'},
        {'production_order_id': '2', 'operation_id': '1', 'sam': 10.0,
         'target_quantity': 100, 'remaining_frac': 1.0, 'release_date': '2026-01-01'},
    ])


ORIGIN = pd.Timestamp('2026-01-01')


def test_returns_both_components_separately_not_a_single_maxed_number(monkeypatch):
    fn = _load_floor_fn(monkeypatch)
    out = fn(_routings(), _Factory(), 1.0, ORIGIN, op_work={k: 1000.0 for k in _Factory().optype_for_op})
    assert isinstance(out, dict)
    for key in ('workload_days', 'critical_path_days', 'binding', 'floor_days', 'fallback_ops'):
        assert key in out, key
    assert out['floor_days'] == max(out['workload_days'], out['critical_path_days'])
    assert out['binding'] in ('workload', 'critical_path')


def test_binding_component_names_the_larger_bound(monkeypatch):
    fn = _load_floor_fn(monkeypatch)
    out = fn(_routings(), _Factory(), 1.0, ORIGIN, op_work={k: 1000.0 for k in _Factory().optype_for_op})
    larger = 'workload' if out['workload_days'] >= out['critical_path_days'] else 'critical_path'
    assert out['binding'] == larger


def test_uses_supplied_scheduler_work_not_a_local_recomputation(monkeypatch):
    # Doubling the scheduler's work content must move the bound. If the
    # function quietly recomputed from sam*qty it would ignore op_work
    # entirely and return the same number -- the exact defect being fixed.
    fn = _load_floor_fn(monkeypatch)
    f, r = _Factory(), _routings()
    small = fn(r, f, 1.0, ORIGIN, op_work={k: 1000.0 for k in f.optype_for_op})
    large = fn(r, f, 1.0, ORIGIN, op_work={k: 2000.0 for k in f.optype_for_op})
    assert large['workload_days'] > small['workload_days']
    assert large['critical_path_days'] > small['critical_path_days']
    assert small['fallback_ops'] == 0


def test_missing_operations_fall_back_with_the_calibration_factor_applied(monkeypatch):
    # The fallback must NOT reintroduce the uncalibrated formula. With
    # op_work empty every operation falls back, and the resulting work must
    # match the calibrated expression, not the bare sam*qty/eff.
    fn = _load_floor_fn(monkeypatch)
    f, r = _Factory(), _routings()
    out = fn(r, f, 1.0, ORIGIN, op_work={})
    assert out['fallback_ops'] == 3
    calibrated = fn(r, f, 1.0, ORIGIN,
                    op_work={k: 0.825454 * 10.0 * 100 / 1.0 for k in f.optype_for_op})
    assert out['workload_days'] == pytest.approx(calibrated['workload_days'], rel=1e-9)
    uncalibrated = fn(r, f, 1.0, ORIGIN,
                      op_work={k: 10.0 * 100 / 1.0 for k in f.optype_for_op})
    assert out['workload_days'] != pytest.approx(uncalibrated['workload_days'], rel=1e-6)


def test_omitting_the_calibration_factor_inflates_the_workload_bound(monkeypatch):
    # Pins the magnitude of the defect EXACTLY, including the detail that
    # makes it not simply 1/0.825454: the workload bound sums
    # serial_processing_mins + DEFAULT_SETUP_TIME_MINS, and setup is additive
    # and NOT calibration-scaled. So the inflation is
    #     (W + S) / (0.825454*W + S)
    # which only approaches 1/0.825454 = 1.2114x as setup becomes negligible
    # against work content. Quoting the bare reciprocal overstates it.
    fn = _load_floor_fn(monkeypatch)
    f, r = _Factory(), _routings()
    W, S, FACTOR = 1000.0, 15.0, 0.825454
    cal = fn(r, f, 1.0, ORIGIN, op_work={k: FACTOR * W for k in f.optype_for_op})
    unc = fn(r, f, 1.0, ORIGIN, op_work={k: W for k in f.optype_for_op})
    expected = (W + S) / (FACTOR * W + S)
    assert unc['workload_days'] / cal['workload_days'] == pytest.approx(expected, rel=1e-9)
    assert expected < 1 / FACTOR          # strictly below the bare reciprocal
    # With work content dwarfing setup, it converges on the reciprocal.
    big = 10_000_000.0
    cal_b = fn(r, f, 1.0, ORIGIN, op_work={k: FACTOR * big for k in f.optype_for_op})
    unc_b = fn(r, f, 1.0, ORIGIN, op_work={k: big for k in f.optype_for_op})
    assert unc_b['workload_days'] / cal_b['workload_days'] == pytest.approx(1 / FACTOR, rel=1e-5)


def test_empty_subset_returns_zeroed_components(monkeypatch):
    fn = _load_floor_fn(monkeypatch)
    out = fn(_routings().iloc[0:0], _Factory(), 1.0, ORIGIN, op_work={})
    assert out['workload_days'] == 0.0 and out['critical_path_days'] == 0.0


# ---------------------------------------------------------------------------
# Bounds must be valid (non-negative) and must evaluate the origin cutoff
# ---------------------------------------------------------------------------

def _routings_released_in_the_past():
    # Every order released BEFORE the scheduling origin -- exactly window 1 of
    # the real book, whose orders are the earliest-released in the order set.
    return pd.DataFrame([
        {'production_order_id': '1', 'operation_id': '1', 'sam': 10.0,
         'target_quantity': 100, 'remaining_frac': 1.0, 'release_date': '2025-12-20'},
        {'production_order_id': '1', 'operation_id': '2', 'sam': 10.0,
         'target_quantity': 100, 'remaining_frac': 1.0, 'release_date': '2025-12-20'},
        {'production_order_id': '2', 'operation_id': '1', 'sam': 10.0,
         'target_quantity': 100, 'remaining_frac': 1.0, 'release_date': '2025-12-22'},
    ])


def test_no_bound_is_ever_negative(monkeypatch):
    # A negative lower bound on a makespan is not a bound. Before the fix the
    # critical-path component returned -12.16 days for window 1 and its ratio
    # printed as -1.0966.
    fn = _load_floor_fn(monkeypatch)
    out = fn(_routings_released_in_the_past(), _Factory(), 1.0, ORIGIN,
             op_work={k: 5000.0 for k in _Factory().optype_for_op})
    assert out['workload_days'] >= 0.0
    assert out['critical_path_days'] >= 0.0
    assert out['floor_days'] >= 0.0


def test_work_already_released_still_produces_a_positive_workload_bound(monkeypatch):
    # THE defect: with every release date in the past, the old bound sampled
    # only negative cutoffs, every candidate bound was negative, and the
    # running max stayed at its 0.0 seed -- a vacuous floor. The t=0 cutoff
    # must bind: makespan >= total_work / capacity.
    fn = _load_floor_fn(monkeypatch)
    f = _Factory()
    work = 5000.0
    out = fn(_routings_released_in_the_past(), f, 1.0, ORIGIN,
             op_work={k: work for k in f.optype_for_op})
    # 3 operations of `work` minutes each + setup, through a pool of 10.
    expected = (3 * (work + 15.0) / 1440.0) / 10
    assert out['workload_days'] == pytest.approx(expected, rel=1e-9)
    assert out['workload_days'] > 0.0


def test_origin_cutoff_can_only_raise_the_bound_never_lower_it(monkeypatch):
    # Adding a cutoff to a max() cannot reduce it. Pins that the fix tightens
    # the bound rather than loosening it -- so corrected makespan/floor ratios
    # move DOWN, never up, and cannot flatter the result.
    fn = _load_floor_fn(monkeypatch)
    f = _Factory()
    future = _routings()          # released at the origin
    past = _routings_released_in_the_past()
    for r in (future, past):
        out = fn(r, f, 1.0, ORIGIN, op_work={k: 5000.0 for k in f.optype_for_op})
        naive_total = sum(5000.0 + 15.0 for _ in f.optype_for_op) / 1440.0 / 10
        assert out['workload_days'] >= naive_total - 1e-9
