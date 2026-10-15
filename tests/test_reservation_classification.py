"""
Committed reservations must carry identity and be classified fixed/replaceable.

THE DEFECT: fetch_committed_operator_reservations() retained only employee,
start and end. With schedule and order identity discarded, nothing downstream
could tell an interval belonging to work being RESCHEDULED from one belonging
to work already under way -- so the exporter reserved 7,820 workers for the
old plan while trying to staff the replacement of that same plan. Measured on
the authoritative snapshot, 100% of reserved (order, operation) pairs (7,349
of 7,349) also appeared as non-Done remaining demand.

Tests are written to FALSIFY the classifier: a rule that marked everything
fixed, everything replaceable, or keyed on actual_start_time would each fail
at least one of these.
"""

from datetime import datetime, timedelta

import pandas as pd
import pytest

from database.queries import (RESERVATION_FIXED, RESERVATION_REPLACEABLE,
                              ReservationRecord, classify_reservation,
                              reservation_composition)
from utils.schedule_status import IN_FLIGHT_STATUSES, OCCUPYING_STATUSES

NOW = datetime(2026, 9, 11, 12, 39, 33)


# ---------------------------------------------------------------------------
# classify_reservation
# ---------------------------------------------------------------------------

def test_future_scheduled_work_is_replaceable():
    # The whole point: work that has not started is what this run reschedules,
    # so holding a worker for it double-books the labour against itself.
    assert classify_reservation(NOW + timedelta(hours=3), 'Scheduled', NOW) \
        == RESERVATION_REPLACEABLE


def test_work_already_under_way_is_fixed():
    for status in IN_FLIGHT_STATUSES:
        assert classify_reservation(NOW + timedelta(hours=3), status, NOW) \
            == RESERVATION_FIXED, status


def test_interval_straddling_the_origin_is_fixed_even_when_labelled_scheduled():
    # THE stale-label case. The status is frozen at SEED time; on the
    # authoritative snapshot the seeder's now_time is 2026-09-09 23:59:59
    # while the scheduling origin is 2026-09-11 12:39:33, and 44 rows carry
    # 'Scheduled' despite having begun before that origin. Trusting the label
    # alone would free genuinely in-flight work.
    assert classify_reservation(NOW - timedelta(hours=2), 'Scheduled', NOW) \
        == RESERVATION_FIXED


def test_classifier_does_not_mark_everything_one_way():
    # A rule that returned a constant would pass any single-case test.
    labels = {
        classify_reservation(NOW + timedelta(hours=1), 'Scheduled', NOW),
        classify_reservation(NOW - timedelta(hours=1), 'Scheduled', NOW),
        classify_reservation(NOW + timedelta(hours=1), 'Running', NOW),
    }
    assert labels == {RESERVATION_FIXED, RESERVATION_REPLACEABLE}


def test_classifier_ignores_actual_start_time_entirely():
    # actual_start_time is non-null on ALL 143,456 reservation rows, because
    # the seeder writes actuals across its whole historical simulation. Any
    # classifier consulting it would mark 100% of reservations fixed and
    # reproduce the original defect exactly. The signature has no parameter
    # for it, which is the structural guarantee.
    import inspect
    params = set(inspect.signature(classify_reservation).parameters)
    assert 'actual_start' not in params and 'actual_start_time' not in params
    assert params == {'planned_start', 'execution_status', 'now'}


def test_missing_planned_start_falls_back_to_replaceable_not_fixed():
    # Absent evidence of having begun, the safe classification is
    # replaceable: treating unknown work as immovable is what over-reserved.
    for missing in (None, pd.NaT):
        assert classify_reservation(missing, 'Scheduled', NOW) == RESERVATION_REPLACEABLE


# ---------------------------------------------------------------------------
# identity is actually carried
# ---------------------------------------------------------------------------

def test_record_carries_schedule_and_order_identity():
    fields = set(ReservationRecord._fields)
    for required in ('schedule_item_id', 'production_order_id', 'operation_id',
                     'asset_id', 'execution_status', 'classification',
                     'segments'):
        assert required in fields, required


def test_loader_selects_the_identity_columns_from_sql():
    # Identity has to come out of the QUERY; adding fields to the NamedTuple
    # without selecting them would leave them unpopulated.
    import inspect
    from database import queries
    src = inspect.getsource(queries.fetch_operator_reservation_records)
    for col in ('schedule_item_id', 'production_order_id', 'operation_id',
                'asset_id', 'planned_work_segments'):
        assert col in src, col


# ---------------------------------------------------------------------------
# composition accounting
# ---------------------------------------------------------------------------

def _rec(emp, start_off, end_off, cls, order='1', op='1', sid=1):
    return ReservationRecord(
        employee_id=emp, start=NOW + timedelta(hours=start_off),
        end=NOW + timedelta(hours=end_off), schedule_item_id=sid,
        production_order_id=order, operation_id=op, asset_id=7,
        execution_status='Scheduled', classification=cls,
        segments=((NOW + timedelta(hours=start_off),
                   NOW + timedelta(hours=end_off)),))


def test_composition_partitions_the_total_exactly():
    recs = [_rec('a', 0, 1, RESERVATION_FIXED, order='1'),
            _rec('b', 0, 2, RESERVATION_REPLACEABLE, order='2'),
            _rec('c', 0, 3, RESERVATION_REPLACEABLE, order='2')]
    c = reservation_composition(recs)
    assert c[RESERVATION_FIXED]['rows'] + c[RESERVATION_REPLACEABLE]['rows'] == c['total']['rows']
    assert (c[RESERVATION_FIXED]['worker_minutes']
            + c[RESERVATION_REPLACEABLE]['worker_minutes']) == pytest.approx(
                c['total']['worker_minutes'])
    assert c[RESERVATION_FIXED]['worker_minutes'] == pytest.approx(60.0)
    assert c[RESERVATION_REPLACEABLE]['worker_minutes'] == pytest.approx(300.0)
    # Orders are counted DISTINCTLY, not summed -- two rows of order '2' is
    # one order, and the total must not double-count it.
    assert c[RESERVATION_REPLACEABLE]['orders'] == 1
    assert c['total']['orders'] == 2


def test_composition_of_an_empty_set_is_all_zeros():
    c = reservation_composition([])
    for cls in (RESERVATION_FIXED, RESERVATION_REPLACEABLE, 'total'):
        assert c[cls]['rows'] == 0 and c[cls]['worker_minutes'] == 0


# ---------------------------------------------------------------------------
# the real caller, against the real snapshot
# ---------------------------------------------------------------------------

SNAPSHOT = 'garment_fyp_db_ga_snapshot_authoritative_20260911'


def _snapshot_available():
    try:
        import mysql.connector
        from data_generation.seed_database_fixed import db_config
        conn = mysql.connector.connect(**{k: v for k, v in db_config.items() if k != 'database'})
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s", (SNAPSHOT,))
        n = cur.fetchone()[0]
        conn.close()
        return n > 0
    except Exception:
        return False


@pytest.mark.skipif(not _snapshot_available(), reason='authoritative snapshot not present')
def test_real_loader_on_the_snapshot_keeps_only_in_flight_work():
    # THE REAL CALLER, not a stub. Pins the measured composition so a
    # regression that reinstates the over-reservation fails loudly.
    import os
    os.environ['DB_NAME'] = SNAPSHOT
    import database.ga_snapshot as gs
    from database.queries import (fetch_operator_reservation_records,
                                  fetch_committed_operator_reservations)
    origin = gs.get_snapshot_origin('authoritative_20260911')
    recs = fetch_operator_reservation_records(origin)
    comp = reservation_composition(recs)
    assert comp['total']['rows'] == 143456
    assert comp['total']['orders'] == 889
    assert comp[RESERVATION_FIXED]['rows'] == 49
    assert comp[RESERVATION_REPLACEABLE]['rows'] == 143407
    # The fixed share is a fraction of a percent -- the artefact dominated.
    assert comp[RESERVATION_FIXED]['worker_minutes'] / comp['total']['worker_minutes'] < 0.01
    kept = fetch_committed_operator_reservations(origin)
    fixed_segments = sum(
        len(r.segments) for r in recs if r.classification == RESERVATION_FIXED)
    assert sum(len(v) for v in kept.values()) == fixed_segments
    every = fetch_committed_operator_reservations(origin, keep=None)
    assert sum(len(v) for v in every.values()) == sum(
        len(r.segments) for r in recs)


@pytest.mark.skipif(not _snapshot_available(), reason='authoritative snapshot not present')
def test_machine_and_worker_sides_agree_on_what_is_fixed():
    # The consistency requirement. Machine state used to start empty while
    # worker state started fully booked; both must now hold the same set.
    import os
    os.environ['DB_NAME'] = SNAPSHOT
    import database.ga_snapshot as gs
    from database.queries import (fetch_committed_machine_reservations,
                                  fetch_committed_machine_state,
                                  fetch_operator_reservation_records)
    origin = gs.get_snapshot_origin('authoritative_20260911')
    recs = [r for r in fetch_operator_reservation_records(origin)
            if r.classification == RESERVATION_FIXED]
    mach = fetch_committed_machine_reservations(origin)
    assert set(mach) == {int(r.asset_id) for r in recs if r.asset_id is not None}
    assert all(v >= 0 for v in mach.values())
    free, last_order = fetch_committed_machine_state(origin)
    assert free == mach
    assert set(last_order) == set(mach)
    assert all(isinstance(v, str) and v for v in last_order.values())


def test_machine_commitment_reader_does_not_depend_on_employee_id(monkeypatch):
    """An unstaffed in-flight row still occupies its physical machine."""
    from database import queries
    rows = pd.DataFrame([{
        'schedule_item_id': 7,
        'production_order_id': 91,
        'operation_id': 12,
        'asset_id': 44,
        'planned_start_time': NOW - timedelta(minutes=30),
        'planned_end_time': NOW + timedelta(minutes=90),
        'execution_status': 'Running',
    }])
    monkeypatch.setattr(queries.pd, 'read_sql', lambda *a, **k: rows.copy())
    free, last_order = queries.fetch_committed_machine_state(NOW)
    assert free[44] == pytest.approx(90.0)
    assert last_order == {44: '91'}


def test_all_production_schedulers_thread_machine_free_and_last_order():
    import inspect
    from agent.decision_engine import AIDecisionEngine
    from ga import rolling_horizon

    direct = inspect.getsource(AIDecisionEngine.trigger_rescheduling)
    assert 'fetch_committed_machine_state(cycle_origin)' in direct
    assert 'initial_machine_free=committed_machine_free' in direct
    assert 'initial_machine_last_order=committed_machine_last_order' in direct

    for fn in (rolling_horizon.run_rolling_horizon,
               rolling_horizon.run_rolling_horizon_baseline):
        source = inspect.getsource(fn)
        assert 'fetch_committed_machine_state(origin)' in source
        assert 'machine_last_order.update(committed_machine_last_order or {})' in source


# ---------------------------------------------------------------------------
# Reservation KEY TYPE -- the defect a string-id stub hid
# ---------------------------------------------------------------------------

def test_reservations_bind_when_keyed_the_way_the_database_keys_them():
    # THE defect: _build_operator_index yields employee ids as INT, while
    # fetch_committed_operator_reservations keys its map with str(). _is_free
    # looked the int up against string keys, never matched, and every
    # DB-loaded committed reservation was silently ignored -- raw key overlap
    # measured at 0 of 3, and reserving 19,783 operators still returned a
    # full crew.
    #
    # The pre-existing regression test passed throughout because it builds its
    # index with STRING ids ('e1'), so its write and read agreed and the
    # production type mismatch was never exercised. This test uses INT ids,
    # like the real index, with STRING reservation keys, like the real loader.
    from agent.decision_engine import AIDecisionEngine
    index = {
        'by_line': {(10, 'body assembly'): [1, 701, 1681]},
        'by_factory': {(1, 'body assembly'): [1, 701, 1681]},
        'by_skill': {'body assembly': [1, 701, 1681]},
        'line_factory': {10: 1},
        'shift_of': {}, 'crew_of': {},
    }
    start = datetime(2026, 9, 11, 6, 0)
    # Keyed as the DATABASE keys them: strings.
    reservations = {'1': [(start - timedelta(hours=1), start + timedelta(hours=5))],
                    '701': [(start - timedelta(hours=1), start + timedelta(hours=5))],
                    '1681': [(start - timedelta(hours=1), start + timedelta(hours=5))]}
    crew, _tier, under = AIDecisionEngine._assign_operators(
        index, 'single-needle lockstitch', 10, 3, {}, 120.0,
        start_dt=start, operator_reservations=reservations)
    assert crew == [], 'string-keyed committed reservations were ignored'
    assert under is not None and under['limiting_factor'] == 'already-reserved'


def test_within_run_reservations_are_written_under_the_same_key_type():
    # The write site must use the same normalisation as the read, or the two
    # halves of the mechanism drift apart again -- this time silently in the
    # other direction.
    from agent.decision_engine import AIDecisionEngine
    index = {
        'by_line': {(10, 'body assembly'): [1]},
        'by_factory': {(1, 'body assembly'): [1]},
        'by_skill': {'body assembly': [1]},
        'line_factory': {10: 1}, 'shift_of': {}, 'crew_of': {},
    }
    start = datetime(2026, 9, 11, 6, 0)
    reservations = {}
    first, _t, _u = AIDecisionEngine._assign_operators(
        index, 'single-needle lockstitch', 10, 1, {}, 120.0,
        start_dt=start, operator_reservations=reservations)
    assert first == [1]
    # Written under a STRING key, matching the loader's namespace.
    assert set(reservations) == {'1'}, reservations
    # And it must now block an overlapping operation.
    second, _t2, under = AIDecisionEngine._assign_operators(
        index, 'single-needle lockstitch', 10, 1, {}, 120.0,
        start_dt=start + timedelta(minutes=60), operator_reservations=reservations)
    assert second == []
    assert under is not None
