"""
The scheduler/evaluator must use the SAME execution semantics as the
simulator and worker assignment: productive work content walked on a
calendar, not `start + duration`.

STATE OF THIS STEP. The architecture is aligned and the primitive is shared
(utils/work_calendar), but the availability PREDICATE is deliberately left at
factory level -- the three 8h bands tile all 24 hours, so no pause is
inserted and spans are unchanged. A real pause pattern belongs to the
ASSIGNED worker, which does not exist until the worker-aware decoder does;
guessing "the band containing the start, no handover" would reimport the very
assumption that produced the 6.08M-worker-minute phantom shortage.

So two tests carry the weight of this step:

  * test_always_available_predicate_is_exactly_shift_blind -- proves the
    change is semantics-NEUTRAL, so no delivery metric moves.
  * test_restrictive_predicate_does_insert_pauses (and the machine/successor
    variants) -- proves the machinery is LIVE rather than dead code that
    happens to return the old answer.

Setup is ELAPSED machine occupancy, outside the calendar, because the
simulator models no setup at all -- see ga/schedule_utils.split_operation_work.
"""

from datetime import datetime, timedelta

import pytest

from ga.fitness import CHANGEOVER_MINUTES, evaluate_schedule
from ga.schedule_utils import calculate_operation_work, split_operation_work
from utils.work_calendar import (elapsed_completion_minutes,
                                 productive_minutes, productive_segments)

ORIGIN = datetime(2026, 3, 2, 6, 0)        # Monday 06:00, start of a band
MORNING_ONLY = lambda t: 6 <= t.hour < 14  # a single 8h band, no handover


# ---------------------------------------------------------------------------
# The primitive
# ---------------------------------------------------------------------------

def test_case1_operation_fully_inside_one_shift_matches_simple_arithmetic():
    # 120 min of work from 06:00 finishes at 08:00 either way.
    assert elapsed_completion_minutes(ORIGIN, 0.0, 120.0, MORNING_ONLY) == \
        pytest.approx(120.0)


def test_case2_operation_crossing_one_boundary_includes_the_pause():
    # 600 min of work from 06:00: 480 today, pause 16h, 120 tomorrow.
    end = elapsed_completion_minutes(ORIGIN, 0.0, 600.0, MORNING_ONLY)
    assert end > 600.0
    # 8h work + 16h pause + 2h work = 26h elapsed.
    assert end == pytest.approx(26 * 60, abs=1)


def test_case3_multi_shift_span_conserves_productive_minutes_exactly():
    for minutes in (500.0, 960.0, 2000.0, 5000.0):
        _s, _e, segs = productive_segments(ORIGIN, minutes, MORNING_ONLY)
        assert productive_minutes(segs) == pytest.approx(minutes, abs=1 / 60), minutes


def test_case13_zero_and_tiny_durations():
    assert elapsed_completion_minutes(ORIGIN, 50.0, 0.0, MORNING_ONLY) == 50.0
    assert elapsed_completion_minutes(ORIGIN, 50.0, -5.0, MORNING_ONLY) == 50.0
    tiny = elapsed_completion_minutes(ORIGIN, 0.0, 0.5, MORNING_ONLY)
    assert 0.0 < tiny <= 1.0


def test_predicate_without_an_origin_raises_rather_than_silently_ignoring_it():
    # Reporting shift-aware semantics while computing shift-blind ones is
    # exactly the class of mismatch this whole step exists to remove.
    with pytest.raises(ValueError, match='needs a scheduling origin'):
        elapsed_completion_minutes(None, 0.0, 100.0, MORNING_ONLY)


# ---------------------------------------------------------------------------
# Case 15 -- boundary-exact cases. This codebase has had an exclusive-end
# boundary defect before, so these are pinned explicitly.
# ---------------------------------------------------------------------------

def test_case15_start_exactly_on_a_boundary_inserts_no_spurious_pause():
    # 06:00 is a boundary AND the first instant of availability. A walk that
    # treated the boundary as unavailable would pause 8h before starting.
    _s, end, segs = productive_segments(ORIGIN, 60.0, MORNING_ONLY)
    assert len(segs) == 1
    assert segs[0][0] == ORIGIN
    assert end == ORIGIN + timedelta(minutes=60)


def test_case15_duration_exactly_one_shift_ends_on_the_boundary_without_a_trailing_pause():
    # Exactly 480 min from 06:00 ends at 14:00. The work is complete AT the
    # boundary, so no pause may be appended -- an inclusive-end walk would
    # add one and report 22h.
    _s, end, segs = productive_segments(ORIGIN, 480.0, MORNING_ONLY)
    assert len(segs) == 1
    assert end == ORIGIN.replace(hour=14)
    assert productive_minutes(segs) == pytest.approx(480.0)
    assert elapsed_completion_minutes(ORIGIN, 0.0, 480.0, MORNING_ONLY) == \
        pytest.approx(480.0)


def test_case15_exact_multiples_of_the_shift_length_do_not_lose_or_gain_a_pause():
    # 2 shifts of work = 2 productive blocks and exactly ONE intervening
    # pause, not two. 480 + 16h + 480 = 40h.
    _s, end, segs = productive_segments(ORIGIN, 960.0, MORNING_ONLY)
    assert len(segs) == 2
    assert productive_minutes(segs) == pytest.approx(960.0)
    # 8h work + 16h off + 8h work = 32h elapsed, not 40h.
    assert (end - ORIGIN) == timedelta(hours=32)
    gap = segs[1][0] - segs[0][1]
    assert gap == timedelta(hours=16)


def test_case15_one_minute_past_the_boundary_pauses_for_the_whole_gap():
    # 481 min: the final minute cannot fit before 14:00, so it waits until
    # 06:00 next day. Half-open handling means the 14:00 instant itself is
    # not workable by this predicate.
    end = elapsed_completion_minutes(ORIGIN, 0.0, 481.0, MORNING_ONLY)
    assert end == pytest.approx(24 * 60 + 1, abs=1)


def test_case15_work_starting_at_the_last_minute_of_a_band():
    start_min = (13 * 60 + 59) - (6 * 60)      # 13:59 relative to 06:00 origin
    end = elapsed_completion_minutes(ORIGIN, float(start_min), 2.0, MORNING_ONLY)
    # 1 min today, 1 min tomorrow morning.
    assert end == pytest.approx((24 + 6) * 60 - 6 * 60 + 1, abs=1)


# ---------------------------------------------------------------------------
# Case 14 -- setup is elapsed occupancy, NOT productive work
# ---------------------------------------------------------------------------

def test_case14_setup_is_split_out_and_not_walked_on_the_calendar():
    detail = {'setup_mins': 15.0, 'serial_processing_mins': 600.0}
    setup, prod = split_operation_work(detail, 2)
    assert setup == 15.0
    assert prod == pytest.approx(300.0)
    # The sum is still exactly what the one duration formula returns, so the
    # split cannot drift from it.
    assert setup + prod == pytest.approx(calculate_operation_work(detail, 2))


def test_case14_setup_delays_productive_start_without_being_paused():
    # Setup elapses as machine occupancy from the operation's start; only the
    # productive remainder is walked. With 470 min of work and 15 min setup
    # from 06:00, productive begins 06:15 and 465 min fit before 14:00, so
    # the last 5 min spill to the next day -- the spill is caused by setup
    # DISPLACING the work, not by setup itself pausing.
    detail = {'setup_mins': 15.0, 'serial_processing_mins': 470.0}
    setup, prod = split_operation_work(detail, 1)
    end = elapsed_completion_minutes(ORIGIN, setup, prod, MORNING_ONLY)
    assert end > setup + prod


# ---------------------------------------------------------------------------
# Case 11 -- no double-counting of pauses
# ---------------------------------------------------------------------------

def test_case11_duration_entering_the_calendar_is_pure_work_content():
    # The split must be pure arithmetic on work content: productive minutes
    # are serial/machines exactly, with no calendar term. If an elapsed span
    # ever leaked into serial_processing_mins upstream, walking it would
    # double-count the pause -- so this pins that the split itself is
    # pause-free and scales exactly with machine count.
    detail = {'setup_mins': 15.0, 'serial_processing_mins': 480.0}
    for machines, expected in ((1, 480.0), (2, 240.0), (4, 120.0)):
        setup, prod = split_operation_work(detail, machines)
        assert setup == 15.0
        assert prod == pytest.approx(expected), machines
    # Doubling the machines exactly halves productive work -- impossible if a
    # calendar or pause term were folded in.
    _s1, p1 = split_operation_work(detail, 1)
    _s2, p2 = split_operation_work(detail, 2)
    assert p1 == pytest.approx(2 * p2)

def test_case11_walking_an_already_elapsed_span_would_inflate_it():
    # Demonstrates why the precondition matters: walking a span that already
    # contains its pause produces a strictly longer answer.
    work = 600.0
    once = elapsed_completion_minutes(ORIGIN, 0.0, work, MORNING_ONLY)
    twice = elapsed_completion_minutes(ORIGIN, 0.0, once, MORNING_ONLY)
    assert twice > once


# ---------------------------------------------------------------------------
# The evaluator, through the real caller
# ---------------------------------------------------------------------------

OPS = ['1_1', '1_2', '2_1']


def _problem(**over):
    op_details = {k: {'setup_mins': 0.0, 'serial_processing_mins': 120.0} for k in OPS}
    gnn = {k: {'delay': 0.0, 'priority': 0.5} for k in OPS}
    kwargs = dict(
        all_line_ids=[1],
        order_deadlines={'1': 1e9, '2': 1e9},
        order_release_minutes={'1': 0.0, '2': 0.0},
        machine_pools={k: {1: [1, 2, 3]} for k in OPS},
        machine_line_map={1: 1, 2: 1, 3: 1},
        usable_machine_count=3, present_operator_count=50,
        op_sequence_map={k: i for i, k in enumerate(OPS)},
        return_details=True, base_time=ORIGIN)
    kwargs.update(over)
    return op_details, gnn, kwargs


def _score(op_details, gnn, kwargs, lines=None, counts=None):
    individual = list(OPS) + (lines or [1] * len(OPS)) + (counts or [1] * len(OPS))
    return evaluate_schedule(individual, len(OPS), op_details, gnn, **kwargs)[1]


def test_always_available_predicate_is_exactly_shift_blind():
    # THE neutrality proof for this step. With factory-level availability the
    # evaluator must reproduce start + setup + productive bit for bit, so no
    # delivery metric can move. Any drift here -- including sub-second
    # rounding from a datetime round trip -- would falsely look like the
    # semantic change altering the schedule.
    op_details, gnn, kwargs = _problem()
    blind = _score(op_details, gnn, dict(kwargs, worker_availability=None))
    per_op = calculate_operation_work(op_details['1_1'], 1)
    # The fixture chains 1_1 -> 1_2 for order 1; order 2 has one operation.
    assert blind['order_completion_minutes']['1'] == pytest.approx(2 * per_op, abs=1e-9)
    assert blind['order_completion_minutes']['2'] == pytest.approx(per_op, abs=1e-9)
    assert blind['makespan_minutes'] == pytest.approx(2 * per_op, abs=1e-9)

    # STRONGER: an always-True predicate forces the real calendar walk rather
    # than the exact-arithmetic short-circuit. The two must agree, so the
    # short-circuit cannot be hiding a defect in the walk.
    walked = _score(op_details, gnn, dict(kwargs, worker_availability=lambda t: True))
    assert walked['makespan_minutes'] == pytest.approx(
        blind['makespan_minutes'], abs=1 / 60)
    for order in ('1', '2'):
        assert walked['order_completion_minutes'][order] == pytest.approx(
            blind['order_completion_minutes'][order], abs=1 / 60)


def test_restrictive_predicate_does_insert_pauses_so_the_machinery_is_live():
    # Proves the wiring is not dead code returning the old answer. 600 min of
    # work under a single 8h band must stretch well past 600.
    op_details, gnn, kwargs = _problem()
    for k in op_details:
        op_details[k]['serial_processing_mins'] = 600.0
    blind = _score(op_details, gnn, dict(kwargs, worker_availability=None))
    shifted = _score(op_details, gnn, dict(kwargs, worker_availability=MORNING_ONLY))
    # Order 1 chains two 600-min operations: 1200 min shift-blind.
    assert blind['makespan_minutes'] == pytest.approx(1200.0, abs=1e-9)
    # Under one 8h band each stretches to 26h elapsed and they are chained.
    assert shifted['makespan_minutes'] > blind['makespan_minutes']
    assert shifted['makespan_minutes'] > 26 * 60


def test_case4_successor_cannot_start_before_predecessors_shift_aware_end():
    # 1_1 and 1_2 belong to the same order, so 1_2 is gated by precedence.
    op_details, gnn, kwargs = _problem()
    for k in op_details:
        op_details[k]['serial_processing_mins'] = 600.0
    det = _score(op_details, gnn, dict(kwargs, worker_availability=MORNING_ONLY))
    # Order 1 runs two chained 600-min operations, each stretching to 26h of
    # elapsed time, so its completion must exceed one operation's elapsed end.
    assert det['order_completion_minutes']['1'] > 26 * 60


def test_case5_and_6_machine_stays_occupied_across_the_pause():
    # One machine, two operations of the SAME order on it. The second cannot
    # begin during the first's off-duty pause, so the machine's elapsed
    # occupancy -- not its productive time -- gates the next job.
    op_details, gnn, kwargs = _problem()
    for k in op_details:
        op_details[k]['serial_processing_mins'] = 600.0
    kwargs['machine_pools'] = {k: {1: [1]} for k in OPS}      # single machine
    det = _score(op_details, gnn, dict(kwargs, worker_availability=MORNING_ONLY))
    # Three 600-min operations on one machine, each 26h elapsed: the last
    # cannot finish before 3 x 26h if occupancy spans the pauses. If the
    # machine were released at productive-end, it would finish far sooner.
    assert det['makespan_minutes'] > 2 * 26 * 60


def test_case7_makespan_uses_shift_aware_ends():
    op_details, gnn, kwargs = _problem()
    for k in op_details:
        op_details[k]['serial_processing_mins'] = 600.0
    blind = _score(op_details, gnn, dict(kwargs, worker_availability=None))
    shifted = _score(op_details, gnn, dict(kwargs, worker_availability=MORNING_ONLY))
    assert shifted['makespan_minutes'] > blind['makespan_minutes'] * 1.5


def test_case8_tardiness_uses_shift_aware_ends():
    # A deadline comfortably met under shift-blind timing must be breached
    # once the pause is modelled.
    op_details, gnn, kwargs = _problem(order_deadlines={'1': 1300.0, '2': 1300.0})
    for k in op_details:
        op_details[k]['serial_processing_mins'] = 600.0
    blind = _score(op_details, gnn, dict(kwargs, worker_availability=None))
    shifted = _score(op_details, gnn, dict(kwargs, worker_availability=MORNING_ONLY))
    assert blind['orders_late'] == 0
    assert shifted['orders_late'] > 0
    assert shifted['max_tardiness_hours'] > blind['max_tardiness_hours']


def test_case9_ga_and_edf_share_one_completion_semantics():
    # Both arms score through evaluate_schedule, so the semantics cannot
    # differ by construction. Pinned structurally: the baseline runner must
    # pass the same base_time/predicate the GA path does.
    import inspect
    from ga import rolling_horizon
    for fn in (rolling_horizon.run_rolling_horizon,
               rolling_horizon.run_rolling_horizon_baseline):
        src = inspect.getsource(fn)
        assert 'base_time=sched.now' in src, fn.__name__
        assert 'worker_availability=sched.worker_availability' in src, fn.__name__


def test_no_second_calendar_implementation_inside_ga_code():
    # Requirement 7/8: reuse utils/work_calendar, do not reimplement.
    import inspect
    from ga import fitness
    src = inspect.getsource(fitness)
    assert 'elapsed_completion_minutes' in src
    for forbidden in ('SHIFT_BOUNDARY_HOURS', 'def productive_segments',
                      'timedelta(hours=8)'):
        assert forbidden not in src, forbidden


# ---------------------------------------------------------------------------
# Case 12 / Part D -- fixed committed machine intervals
# ---------------------------------------------------------------------------

def test_case12_fixed_committed_machine_interval_blocks_a_new_operation():
    # The 49 in-flight commitments arrive as initial_machine_free in
    # minutes-from-origin. A new operation must not start before that.
    op_details, gnn, kwargs = _problem()
    busy_until = 500.0
    kwargs['machine_pools'] = {k: {1: [1]} for k in OPS}
    kwargs['initial_machine_free'] = {1: busy_until}
    det = _score(op_details, gnn, dict(kwargs, worker_availability=None))
    first_end = min(det['order_completion_minutes'].values())
    assert first_end >= busy_until + 120.0 - 1e-6


def test_case12_committed_interval_interacts_with_the_calendar_walk():
    # With a restrictive predicate the machine is busy until 500 min after a
    # 06:00 origin (14:20 -- past the band), so productive work cannot begin
    # until 06:00 the next day.
    op_details, gnn, kwargs = _problem()
    kwargs['machine_pools'] = {k: {1: [1]} for k in OPS}
    kwargs['initial_machine_free'] = {1: 500.0}
    det = _score(op_details, gnn, dict(kwargs, worker_availability=MORNING_ONLY))
    # Earliest completion must be after the overnight wait, not 500+120.
    assert min(det['order_completion_minutes'].values()) > 500.0 + 120.0


# ---------------------------------------------------------------------------
# Case 10 / 16 -- agreement with the simulator for matched inputs
# ---------------------------------------------------------------------------

def test_case10_shared_walk_matches_the_simulator_for_identical_input():
    from data_generation.simulation_calendar import work_on_calendar
    from utils.shift_rotation import SHIFT_BANDS, band_covers
    band = SHIFT_BANDS[0]
    for minutes in (60.0, 500.0, 1500.0):
        sim = work_on_calendar(ORIGIN, minutes, band=band)
        shared = productive_segments(ORIGIN, minutes, lambda t: band_covers(band, t))
        assert sim == shared, minutes


def test_case16_evaluator_elapsed_end_matches_the_simulator_planned_span():
    # Same start, same productive work, same calendar -> the evaluator's
    # elapsed end must equal the simulator's planned span end. The ONLY
    # documented difference is setup/changeover, which the simulator does not
    # model at all; this fixture sets setup to 0 so the comparison is clean.
    from data_generation.simulation_calendar import work_on_calendar
    from utils.shift_rotation import SHIFT_BANDS, band_covers
    band = SHIFT_BANDS[0]
    pred = lambda t: band_covers(band, t)
    for minutes in (120.0, 600.0, 1500.0):
        _s, sim_end, _segs = work_on_calendar(ORIGIN, minutes, band=band)
        evaluator_end = elapsed_completion_minutes(ORIGIN, 0.0, minutes, pred)
        assert evaluator_end == pytest.approx(
            (sim_end - ORIGIN).total_seconds() / 60.0, abs=1 / 60), minutes


def test_documented_divergence_is_setup_and_changeover_only():
    # The scheduler charges costs the training data does not contain. Pinned
    # so the divergence stays explicit and discoverable rather than folklore.
    import inspect
    src = inspect.getsource(split_operation_work)
    assert 'SIMULATOR MODELS NO SETUP AT ALL' in src
    assert 'TRAINING DATA DOES NOT CONTAIN' in src
    assert CHANGEOVER_MINUTES > 0
