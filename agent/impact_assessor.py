"""
Impact assessment for shop-floor disruptions (machine breakdowns, for now).

Purpose: decide WITHOUT running the full factory-wide GA whether a
disruption is (a) genuinely a non-event, (b) something a small local
reassignment can absorb, or (c) something that needs the full
trigger_rescheduling() pass — see agent/fuzzy_logic.py for the tier logic
itself, and the conversation this was designed in: the cushion between an
order's predicted completion and its customer-quoted date IS the shock
absorber budget for exactly this kind of check.

Also implements the donor-preemption pattern discussed for the A/B/C/D
scenario: if there's no idle alternate machine, look at who's currently
occupying the nearest slot on an eligible alternate machine, and — only if
that order has strictly MORE slack than the affected order, and isn't
manager-flagged — bump it back to make room, then re-verify the donor is
still safe under its own fuzzy check before committing to either change.

NOTE: this module has been logic-checked against the schema used elsewhere
in this codebase (database/queries.py, agent/monitor.py) but has not been
run against a live database — no DB is available in this environment. Test
it against a real breakdown scenario before relying on it.
"""

import os
import sys
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
from sqlalchemy import bindparam, text, table, column, and_, DateTime, select

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.logger import get_logger
from utils.schedule_status import OCCUPYING_STATUSES
from database.db import get_engine, get_db_session
from database.queries import fetch_machine_state
from utils.factory_ownership import FactoryOwnershipError, require_factory_ownership
from utils.machine_reserve import build_reserve, reserve_pool_by_type
from ga.schedule_utils import calculate_operation_work
from agent.fuzzy_logic import decide_tier, urgency_score, Tier

logger = get_logger("impact_assessor", "agent.log")

# How far into the future we're willing to look for a donor's blocking slot
# before giving up and just escalating to a full reschedule. Keeps this
# check cheap — it's meant to be fast, not exhaustive.
DONOR_SEARCH_HORIZON_HOURS = 72


def _get_affected_operations(broken_asset_id) -> pd.DataFrame:
    """Not-yet-finished operations that were sitting on the broken machine."""
    query = text("""
        SELECT *
        FROM Job_Shop_Schedules
        WHERE asset_id = :aid AND execution_status IN :occupying_statuses
    """).bindparams(bindparam('occupying_statuses', expanding=True))
    return pd.read_sql(query, get_engine(), params={'aid': broken_asset_id, 'occupying_statuses': OCCUPYING_STATUSES})


def _get_machine_operation_type(asset_id) -> str:
    query = text("SELECT operation_type FROM Machine_Capabilities WHERE asset_id = :aid LIMIT 1")
    row = pd.read_sql(query, get_engine(), params={'aid': asset_id})
    return row.iloc[0]['operation_type'] if not row.empty else None


def _get_alternate_machines(operation_type: str, exclude_asset_id,
                            factory_id) -> pd.DataFrame:
    """Active compatible assets in the affected order's owned factory."""
    query = text("""
        SELECT ma.asset_id, ma.line_id, ma.factory_id
        FROM Machinery_Assets ma
        JOIN Machine_Capabilities mc ON ma.asset_id = mc.asset_id
        WHERE mc.operation_type = :op_type
          AND ma.asset_id != :exclude_id
          AND ma.factory_id = :factory_id
          AND ma.line_id IS NOT NULL
          AND ma.current_status = 'Active'
    """)
    return pd.read_sql(
        query, get_engine(),
        params={'op_type': operation_type, 'exclude_id': exclude_asset_id,
                'factory_id': factory_id})


def _get_reserve_context():
    """
    The standby reserve, resolved from the same deterministic rule the GA used
    when it built the schedule (utils/machine_reserve.py). Both sides must
    agree: if this module thought a machine were reserved when the GA had
    already committed it to other work, a "free" replacement would be
    double-booked.
    """
    reserved, _caps = build_reserve(fetch_machine_state())
    pools = reserve_pool_by_type(fetch_machine_state(), reserved)
    return reserved, pools


def _get_machine_committed_schedule(asset_id) -> pd.DataFrame:
    """Currently committed (not-yet-done) operations on a given machine, for gap-finding."""
    query = text("""
        SELECT *
        FROM Job_Shop_Schedules
        WHERE asset_id = :aid AND execution_status IN :occupying_statuses
        ORDER BY planned_start_time
    """).bindparams(bindparam('occupying_statuses', expanding=True))
    return pd.read_sql(query, get_engine(), params={'aid': asset_id, 'occupying_statuses': OCCUPYING_STATUSES})


def _get_committed_schedules_bulk(asset_ids) -> dict:
    """
    Committed schedules for MANY machines in a single round-trip.

    Replaces calling _get_machine_committed_schedule() once per candidate
    machine inside the per-affected-operation loop. That pattern is
    O(affected_ops x alternate_machines) queries, and on real data those
    numbers are not small: asset 121 has 208 queued operations and 531
    active alternates of the same type, i.e. 110,448 round-trips for one
    breakdown — doubled when the idle-slot search finds nothing and the
    donor-preemption pass repeats the sweep. Measured: the original version
    had produced no output after 18 minutes and had to be killed, which
    defeats the entire purpose of a triage gate that exists to be "fast, not
    exhaustive" (see DONOR_SEARCH_HORIZON_HOURS above).

    Returns {asset_id: DataFrame sorted by planned_start_time}. Machines with
    nothing committed are absent from the dict; callers should treat a miss
    as an empty schedule.
    """
    asset_ids = [int(a) for a in asset_ids]
    if not asset_ids:
        return {}
    query = text("""
        SELECT *
        FROM Job_Shop_Schedules
        WHERE asset_id IN :aids AND execution_status IN :occupying_statuses
        ORDER BY asset_id, planned_start_time
    """).bindparams(bindparam('occupying_statuses', expanding=True)).bindparams(bindparam('aids', expanding=True))
    df = pd.read_sql(query, get_engine(), params={'aids': asset_ids, 'occupying_statuses': OCCUPYING_STATUSES})
    return {aid: grp.reset_index(drop=True) for aid, grp in df.groupby('asset_id')}


def _get_order_slack_days_bulk(order_ids, now=None) -> dict:
    """
    Slack in days for many orders in one round-trip — same motivation as
    _get_committed_schedules_bulk().

    SILENT-DEFAULT FIX (audit #5a): orders with no usable deadline are now
    OMITTED from the returned dict rather than reported as 999.0 ("fully
    relaxed"). Callers must treat a missing key as "slack unknown" and
    exclude the order, because 999.0 made an order nobody has data for into
    the most attractive donor to preempt.
    """
    order_ids = [str(o) for o in set(order_ids)]
    if not order_ids:
        return {}
    # delivery_deadline, matching get_order_slack_days() and the quantity the
    # fuzzy anchors are calibrated on (Batch-4 fix 3a). This read
    # scheduled_end_date while the seeder derived its quantiles from
    # delivery_deadline -- calibration and consumption measuring two
    # different distributions, which is precisely the class of defect this
    # batch exists to close.
    query = text("""
        SELECT po.production_order_id, pu.delivery_deadline
        FROM Production_Orders po
        LEFT JOIN Purchase_Orders pu ON pu.po_id = po.po_id
        WHERE po.production_order_id IN :oids
    """).bindparams(bindparam('oids', expanding=True))
    df = pd.read_sql(query, get_engine(), params={'oids': order_ids})
    now = now or datetime.now()
    out = {}
    for _, row in df.iterrows():
        d = row['delivery_deadline']
        if pd.isna(d):
            continue  # slack unknown -- absent, not "infinitely relaxed"
        out[str(row['production_order_id'])] = (
            pd.to_datetime(d) - now
        ).total_seconds() / 86400.0
    return out


def _find_slot(committed: pd.DataFrame, ready_after: datetime, duration: timedelta):
    """
    Finds the earliest open gap on a machine's committed schedule, at or
    after `ready_after`. Mirrors ga/schedule_utils.find_slot_and_insert's
    gap-finding logic, but works in absolute datetimes rather than
    minute-offsets from a single shared base_time — this runs standalone
    against live DB timestamps, not inside one GA simulation pass.

    Returns (candidate_start, blocking_row_or_None). blocking_row is the
    committed row that was still running/scheduled at `ready_after`, i.e.
    the row a donor-preemption attempt would need to bump — None if the
    slot was already idle and nothing needed to be stepped over.

    PERFORMANCE: this is the innermost operation of the whole assessment —
    it runs once per (affected_operation x alternate_machine) pair, which is
    208 x 531 = 110,448 times for a single breakdown on this dataset, each
    scanning up to ~200 committed rows. The original implementation used
    DataFrame.iterrows(), which materialises a fresh pandas Series per row;
    at ~22M row visits that never finished (killed at 600s even after the
    DB round-trips were batched away). Scanning pre-extracted numpy arrays
    instead keeps identical semantics — same traversal order, same break
    conditions — while removing the per-row Series construction. Only the
    single row that actually blocks is ever converted back to a pandas row,
    since that is all the donor-preemption path needs.
    """
    starts = committed['planned_start_time'].to_numpy()
    ends = committed['planned_end_time'].to_numpy()

    candidate_start = np.datetime64(ready_after)
    duration_td = np.timedelta64(int(duration.total_seconds()), 's')
    blocking_idx = -1

    for i in range(len(starts)):
        if candidate_start + duration_td <= starts[i]:
            break  # gap found before this row
        if candidate_start < ends[i]:
            blocking_idx = i
            candidate_start = ends[i]

    result_start = pd.Timestamp(candidate_start).to_pydatetime()
    blocking_row = committed.iloc[blocking_idx] if blocking_idx >= 0 else None
    return result_start, blocking_row


def _book_tentative_slot(committed_by_asset, empty_committed, asset_id, order_id, op_id, start, end,
                         schedule_item_id=None):
    """
    Records [start, end) as occupied on asset_id for the REMAINDER of this
    assessment call, by appending a synthetic row to committed_by_asset and
    re-sorting (mirrors _find_slot's own sorted-scan assumption).

    BUG FIX: committed_by_asset was fetched ONCE, before the per-operation
    loop, and never updated as the loop tentatively assigned candidate
    slots to earlier operations from the SAME breakdown. Confirmed for
    real, not just from reading the code: GA_AGENT_POSTMORTEM.md Section
    5c found the identical candidate_asset_id recommended for 5 different
    affected operations in one real assessment call. Every real assessment
    observed came back NO_ACTION (which never writes -- Section 5a), so
    this never actually double-booked a live schedule, but two or more
    LOCAL_PATCH verdicts from the same event competing for the same slot
    would have, and nothing before this fix would have caught it.
    """
    new_row = pd.DataFrame([{
        'schedule_item_id': schedule_item_id, 'is_tentative': True,
        'production_order_id': str(order_id), 'operation_id': str(op_id),
        'planned_start_time': start, 'planned_end_time': end,
    }])
    existing = committed_by_asset.get(asset_id, empty_committed)
    committed_by_asset[asset_id] = (
        pd.concat([existing, new_row], ignore_index=True)
        .sort_values('planned_start_time').reset_index(drop=True)
    )


def _release_tentative_slot(committed_by_asset, asset_id, order_id, op_id,
                            schedule_item_id=None):
    """Release one identified lot; never remove a set sharing order/operation."""
    existing = committed_by_asset.get(asset_id)
    if existing is None or existing.empty:
        return
    if schedule_item_id is not None:
        match = existing['schedule_item_id'] == schedule_item_id
    else:
        # Compatibility for isolated callers without persisted row IDs.
        # Ambiguous matches must fail rather than removing multiple lots.
        match = ((existing['production_order_id'].astype(str) == str(order_id)) &
                 (existing['operation_id'].astype(str) == str(op_id)))
    if int(match.sum()) > 1:
        raise ValueError("Ambiguous tentative release: a unique schedule_item_id is required")
    committed_by_asset[asset_id] = existing[~match].reset_index(drop=True)


def _schedule_snapshot(row):
    """Capture all persisted fields for optimistic concurrency checking."""
    snapshot = {}
    for key, value in dict(row).items():
        # owned_factory_id is assessment metadata from the authoritative
        # ownership artifact, not a Job_Shop_Schedules column.
        if key in {'is_tentative', 'owned_factory_id'}:
            continue
        if pd.isna(value):
            value = None
        elif hasattr(value, 'to_pydatetime'):
            value = value.to_pydatetime()
        elif hasattr(value, 'item'):
            value = value.item()
        snapshot[key] = value
    if snapshot.get('schedule_item_id') is None:
        raise ValueError("Assessment row is missing its unique schedule_item_id")
    snapshot['schedule_item_id'] = int(snapshot['schedule_item_id'])
    return snapshot


def get_order_slack_days(order_id, now=None):
    """
    Days between `now` and an order's CUSTOMER deadline, or None if unknown.

    RETURNS None, NOT 999.0 (Batch-4 fix 4). The old sentinel meant a lookup
    miss -- an order with no deadline row, or one the query simply failed to
    find -- scored as maximally relaxed, i.e. the single most attractive
    order in the book to preempt as a donor. The safest-looking default was
    the most dangerous one available. Callers must treat None as "cannot
    assess" and exclude the order from donor candidacy.

    QUANTITY (Batch-4 fix 3a): Purchase_Orders.delivery_deadline, the
    CUSTOMER commitment -- not Production_Orders.scheduled_end_date, which
    this read before. Three reasons, all from evidence already in this repo:
      * database/queries.py::fetch_active_orders_and_routings documents the
        same switch for the GA, having measured that scheduled_end_date "has
        almost always already elapsed" (1,111 of 1,410 active orders past it,
        versus 0 past their delivery_deadline).
      * Re-measured for this fix at one fixed timestamp over all 1,029 live
        orders: scheduled_end_date gives Q25 -34.8d / median 3.2d / Q75 60.2d
        with 487 orders (47%) already overdue; delivery_deadline gives
        Q25 -12.8d / median 26.2d / Q75 87.2d with 330 (32%) overdue. Triage
        on the internal milestone would classify nearly half the book as
        critically late against a date no customer ever sees.
      * The seeder derives the fuzzy slack anchors from delivery_deadline.
        Calibration and consumption MUST measure the same quantity; they did
        not, which is the defect this fixes.

    `now`: pass one timestamp for a whole assessment cycle so every
    comparison within it is mutually consistent. Defaults to datetime.now().

    NOT CACHED any more. The previous lru_cache had process lifetime, and
    its own docstring recommended calling cache_clear() each cycle -- no
    such call existed anywhere, so a long-lived monitor would compare fresh
    slack against values computed hours earlier. Passing an explicit `now`
    gives the intra-cycle consistency the cache was really there for.
    """
    now = now or datetime.now()
    query = text("""
        SELECT pu.delivery_deadline
        FROM Production_Orders po
        LEFT JOIN Purchase_Orders pu ON pu.po_id = po.po_id
        WHERE po.production_order_id = :oid
    """)
    row = pd.read_sql(query, get_engine(), params={'oid': order_id})
    if row.empty or pd.isna(row.iloc[0]['delivery_deadline']):
        return None
    deadline = pd.to_datetime(row.iloc[0]['delivery_deadline'])
    return (deadline - now).total_seconds() / 86400.0


def _placement_conflicts(committed, placements, moved_ids):
    """Check half-open proposed intervals against every unchanged reservation."""
    conflicts = []
    for start, end in placements:
        for row in committed.to_dict('records'):
            if row.get('schedule_item_id') in moved_ids:
                continue
            if start < row['planned_end_time'] and end > row['planned_start_time']:
                conflicts.append(int(row['schedule_item_id']))
    return sorted(set(conflicts))


def _attempt_donor_preemption(affected_order_id, affected_urgency, duration, alt_asset_id,
                               committed, ready_after, manager_forced_orders, rejections=None,
                               assessment_now=None):
    """
    Single-level donor preemption: find the order currently occupying the
    slot that's blocking our affected operation. If that donor has strictly
    MORE slack (lower urgency_score) than the affected order, and isn't
    manager-flagged, tentatively bump the donor back by `duration` and
    re-check the donor's OWN tier under the fuzzy gate. Only returns a
    proposal if the donor comes out safe (NO_ACTION or LOCAL_PATCH) after
    the bump — never hands back a preemption that would just move the
    problem onto someone else.
    """
    candidate_start, blocking_row = _find_slot(committed, ready_after, duration)
    if blocking_row is None:
        return candidate_start, None  # already idle, no donor needed

    # Tentative bookings are not independently committed donor rows.
    tentative = blocking_row.get('is_tentative', False)
    if pd.notna(tentative) and bool(tentative):
        return None, None
    if pd.isna(blocking_row.get('schedule_item_id')):
        return None, None
    donor_order_id = str(blocking_row['production_order_id'])
    if donor_order_id == str(affected_order_id) or donor_order_id in manager_forced_orders:
        return None, None

    # UNKNOWN SLACK => INELIGIBLE AS A DONOR (Batch-4 fix 4).
    # get_order_slack_days() used to return 999.0 here for a missing
    # deadline, which made exactly the orders we know nothing about into the
    # most attractive ones to preempt. It now returns None, and None means
    # "cannot assess safely", which is a refusal -- not a licence.
    donor_slack = get_order_slack_days(donor_order_id, now=assessment_now)
    if donor_slack is None:
        logger.warning(
            f"Donor preemption rejected: order {donor_order_id} has no usable "
            f"delivery deadline, so its slack cannot be established. An order "
            f"whose urgency is unknown is never a safe thing to bump."
        )
        return None, None
    donor_urgency = urgency_score(donor_slack, manager_forced=False)

    if donor_urgency >= affected_urgency:
        # Donor is equally or more urgent than the order we're trying to
        # help — never preempt sideways/upwards. Rule from the A/B/C/D
        # discussion: only ever take from strictly more slack.
        return None, None

    # Simulate bumping the donor by `duration` and re-check ITS safety.
    donor_new_delay_hours = duration.total_seconds() / 3600.0
    donor_decision = decide_tier(donor_slack, donor_new_delay_hours, manager_forced=False)
    if donor_decision['tier'] == Tier.FULL_RESCHEDULE:
        logger.info(
            f"Donor preemption rejected: order {donor_order_id} (slack {donor_slack:.1f}d) "
            f"would itself breach its cushion if bumped {donor_new_delay_hours:.1f}h — not safe to preempt."
        )
        return None, None

    # Safe to preempt: our op takes the donor's slot, donor moves to right after.
    #
    # Preserve the unique lot identity and original row for an atomic
    # compare-and-update; order/operation/machine is not unique per lot.
    donor_op = {
        'schedule_item_id': int(blocking_row['schedule_item_id']),
        'original_snapshot': _schedule_snapshot(blocking_row),
        'production_order_id': donor_order_id,
        'operation_id': str(blocking_row['operation_id']),
        'asset_id': alt_asset_id,
        'old_start': blocking_row['planned_start_time'],
        'old_end': blocking_row['planned_end_time'],
        'new_start': blocking_row['planned_start_time'] + duration,
        'new_end': blocking_row['planned_end_time'] + duration,
        'donor_slack_days': donor_slack,
        'donor_urgency_score': donor_urgency,
    }
    placements = [(ready_after, ready_after + duration),
                  (donor_op['new_start'], donor_op['new_end'])]
    conflicts = _placement_conflicts(committed, placements, {donor_op['schedule_item_id']})
    mutual_overlap = placements[0][0] < placements[1][1] and placements[0][1] > placements[1][0]
    if conflicts or mutual_overlap:
        rejection = {'code': 'PREEMPTION_RESERVATION_CONFLICT',
                     'asset_id': int(alt_asset_id),
                     'blocking_schedule_item_ids': conflicts,
                     'proposed_intervals_overlap': mutual_overlap}
        if rejections is not None:
            rejections.append(rejection)
        logger.warning(f"Donor preemption rejected: {rejection}")
        return None, None
    return ready_after, donor_op


def assess_machine_breakdown(broken_asset_id, manager_forced_orders=None,
                              op_work_details=None) -> list:
    """
    Main entry point. Given a broken machine, returns a list of per-affected-
    operation assessment dicts, each carrying a tier decision
    (NO_ACTION / LOCAL_PATCH / FULL_RESCHEDULE) plus everything needed to
    execute a local patch (see apply_local_patch) if that's the verdict.
    """
    assessment_now = datetime.now()
    manager_forced_orders = set(str(x) for x in (manager_forced_orders or []))
    # {op_key: {'setup_mins', 'serial_processing_mins'}} — only consulted when
    # a committed row has unusable planned times (see the loop below).
    op_work_details = op_work_details or {}
    preemption_rejections = []
    affected_ops = _get_affected_operations(broken_asset_id)

    if affected_ops.empty:
        logger.info(f"Machine {broken_asset_id} breakdown affects 0 queued/running operations — true no-op.")
        return []

    try:
        order_ids = affected_ops['production_order_id'].astype(str).unique().tolist()
        ownership = require_factory_ownership(order_ids)
    except (FactoryOwnershipError, KeyError, TypeError, ValueError) as exc:
        logger.error(
            f"Machine {broken_asset_id}: factory ownership cannot be resolved ({exc}); "
            "local patch is unsafe.")
        return [{
            'schedule_item_id': int(row['schedule_item_id']),
            'original_snapshot': _schedule_snapshot(row),
            'production_order_id': str(row['production_order_id']),
            'operation_id': str(row['operation_id']),
            'tier': Tier.FULL_RESCHEDULE.value,
            'reason': 'Missing or invalid factory ownership; local patch is unsafe.',
        } for _, row in affected_ops.iterrows()]
    affected_ops = affected_ops.copy()
    affected_ops['owned_factory_id'] = (
        affected_ops['production_order_id'].astype(str).map(ownership)
    )

    operation_type = _get_machine_operation_type(broken_asset_id)
    if operation_type is None:
        logger.error(f"No Machine_Capabilities row for asset {broken_asset_id} — cannot assess safely, escalating all affected orders.")
        return [{
            'schedule_item_id': int(row['schedule_item_id']),
            'original_snapshot': _schedule_snapshot(row),
            'production_order_id': str(row['production_order_id']),
            'operation_id': str(row['operation_id']),
            'tier': Tier.FULL_RESCHEDULE.value,
            'reason': 'Unknown machine capability — cannot estimate impact, escalating for safety.',
        } for _, row in affected_ops.iterrows()]

    owned_factories = {
        int(value) for value in affected_ops['owned_factory_id'].dropna().unique()
    }
    if not owned_factories or affected_ops['owned_factory_id'].isna().any():
        logger.error(
            f"Machine {broken_asset_id}: at least one affected order has no persisted "
            "factory ownership; cannot construct a physically valid local patch.")
        return [{
            'schedule_item_id': int(row['schedule_item_id']),
            'original_snapshot': _schedule_snapshot(row),
            'production_order_id': str(row['production_order_id']),
            'operation_id': str(row['operation_id']),
            'tier': Tier.FULL_RESCHEDULE.value,
            'reason': 'Missing factory ownership; local patch is unsafe.',
        } for _, row in affected_ops.iterrows()]
    alternates_by_factory = {
        factory_id: _get_alternate_machines(
            operation_type, broken_asset_id, factory_id)
        for factory_id in owned_factories
    }

    # STANDBY RESERVE (7% of each machine type, company-wide).
    #
    # Two distinct roles, and they must not be confused:
    #   - Reserved machines are EXCLUDED from the ordinary alternate search
    #     below, because they are deliberately not part of scheduled capacity.
    #   - But they are exactly what a breakdown is for, so they are consulted
    #     as an immediate replacement BEFORE any donor preemption is
    #     attempted. Taking a slot from another order's committed schedule is
    #     a last resort; that is the whole reason the reserve is held back.
    reserved_asset_ids, reserve_pools = _get_reserve_context()
    if reserved_asset_ids:
        alternates_by_factory = {
            factory_id: frame[~frame['asset_id'].isin(reserved_asset_ids)]
                .reset_index(drop=True)
            for factory_id, frame in alternates_by_factory.items()
        }
    all_reserve_candidates = list(
        reserve_pools.get(str(operation_type).strip().lower(), []))
    machine_state = fetch_machine_state()
    machine_factory = {
        int(row.asset_id): int(row.factory_id)
        for row in machine_state.dropna(subset=['factory_id']).itertuples(index=False)
    }
    machine_line = {
        int(row.asset_id): int(row.line_id)
        for row in machine_state.dropna(subset=['line_id']).itertuples(index=False)
    }
    for factory_id, frame in alternates_by_factory.items():
        reserve_n = sum(
            machine_factory.get(int(asset_id)) == factory_id
            for asset_id in all_reserve_candidates)
        logger.info(
            f"Machine {broken_asset_id} ({operation_type}), factory {factory_id}: "
            f"{len(frame)} local scheduled-capacity alternates, {reserve_n} local "
            "standby reserve machine(s)."
        )

    # Fetch every candidate machine's committed schedule ONCE, up front, and
    # every affected order's slack ONCE, instead of re-querying inside the
    # nested loops below. See _get_committed_schedules_bulk() for the
    # measured cost of the original per-iteration pattern.
    candidate_assets = {
        int(asset_id)
        for frame in alternates_by_factory.values()
        for asset_id in frame['asset_id'].tolist()
    }
    candidate_assets.update(int(asset_id) for asset_id in all_reserve_candidates)
    committed_by_asset = _get_committed_schedules_bulk(sorted(candidate_assets))
    # ONE timestamp for the whole assessment cycle (Batch-4 fix 4). Every
    # slack comparison in this pass is now measured against the same instant,
    # instead of each lookup calling datetime.now() independently and drifting
    # relative to the others -- which is the consistency the removed
    # process-lifetime lru_cache was really standing in for.
    slack_by_order = _get_order_slack_days_bulk(
        affected_ops['production_order_id'].tolist(), now=assessment_now)
    empty_committed = pd.DataFrame(
        columns=['production_order_id', 'operation_id', 'planned_start_time', 'planned_end_time']
    )
    logger.info(
        f"Assessing {len(affected_ops)} affected operation(s) against "
        f"{sum(len(frame) for frame in alternates_by_factory.values())} local alternate "
        "machine(s), factory-scoped by persisted order ownership."
    )

    assessments = []
    # How many times each spare has been committed during THIS assessment, so
    # repeated draws spread across the reserve rather than double-booking one
    # machine for several affected operations.
    reserve_usage = {}
    reserve_drawn = 0
    # Orders whose slack could not be established -- excluded from triage
    # rather than defaulted to maximally-relaxed (audit #5a).

    for _, op in affected_ops.iterrows():
        order_id = str(op['production_order_id'])
        planned_start = op['planned_start_time']
        planned_end = op['planned_end_time']

        # Duration comes from the COMMITTED schedule row, not from the work
        # formula: this operation has already been planned onto the floor and
        # the committed span is what a replacement machine has to reproduce.
        # Recomputing it from SAM here would silently disagree with the plan
        # everyone downstream is working to.
        #
        # But when those timestamps are unusable, fall back to the one shared
        # definition in ga/schedule_utils.py rather than inventing a fourth
        # local duration expression — that duplication is what produced the
        # 60x double-division bug. The fallback assumes a single machine,
        # which is the conservative reading (longest plausible duration) for a
        # decision about whether a breakdown can be absorbed.
        if pd.isna(planned_start) or pd.isna(planned_end) or planned_end <= planned_start:
            fallback_mins = calculate_operation_work(
                op_work_details.get(f"{op['production_order_id']}_{op['operation_id']}", {}),
                machine_count=1,
            )
            duration = timedelta(minutes=fallback_mins)
            planned_start = planned_start if not pd.isna(planned_start) else assessment_now
            logger.warning(
                f"Order {op['production_order_id']} op {op['operation_id']}: unusable planned "
                f"times on the committed row — falling back to the shared work formula "
                f"({fallback_mins:.0f} min on one machine)."
            )
        else:
            duration = planned_end - planned_start

        ready_after = max(planned_start, assessment_now)

        # SILENT-DEFAULT FIX (audit #5a). This was
        # `slack_by_order.get(order_id, 999.0)` -- a lookup miss produced
        # "999 days of slack", i.e. maximally relaxed, i.e. the single most
        # attractive order in the book to preempt as a donor. The
        # safest-LOOKING default was the most dangerous one available: a
        # database hiccup or a missing deadline turned an order the system
        # knows nothing about into the one it would sacrifice first.
        #
        # An order whose slack cannot be established is now EXCLUDED from
        # this pass rather than assigned a flattering number.
        if order_id not in slack_by_order:
            logger.warning(
                f"Order {order_id}: slack could not be determined; cannot assess safely; "
                f"returning a visible manual-review record."
            )
            assessments.append({
                'schedule_item_id': int(op['schedule_item_id']),
                'original_snapshot': _schedule_snapshot(op),
                'production_order_id': order_id, 'operation_id': str(op['operation_id']),
                'assessment_status': 'cannot_assess_safely',
                'reason_code': 'UNKNOWN_CUSTOMER_DEADLINE',
                'reason': 'Customer deadline missing; manual review required before moving this lot.',
                'requires_manual_review': True,
                'tier': Tier.FULL_RESCHEDULE.value,
                'slack_days': None, 'impact_hours': None, 'urgency_score': None,
                'candidate_asset_id': None, 'candidate_start_time': None,
                'original_asset_id': broken_asset_id, 'duration': duration,
                'donor_plan': None,
            })
            continue
        slack_days = slack_by_order[order_id]
        forced = order_id in manager_forced_orders
        affected_urgency = urgency_score(slack_days, manager_forced=forced)

        factory_id = int(op['owned_factory_id'])
        alt_machines = alternates_by_factory[factory_id]
        reserve_candidates = [
            int(asset_id) for asset_id in all_reserve_candidates
            if machine_factory.get(int(asset_id)) == factory_id
            and int(asset_id) in machine_line
        ]
        candidate_line_by_asset = {
            int(row.asset_id): int(row.line_id)
            for row in alt_machines.itertuples(index=False)
        }
        candidate_line_by_asset.update({
            asset_id: machine_line[asset_id] for asset_id in reserve_candidates
        })
        best_start, best_asset, donor_plan = None, None, None

        if alt_machines.empty and not reserve_candidates:
            impact_hours = float('inf')
        else:
            # Try every alternate eligible machine's pure idle slot first.
            for alt_asset_id in alt_machines['asset_id']:
                committed = committed_by_asset.get(alt_asset_id, empty_committed)
                candidate_start, blocking_row = _find_slot(committed, ready_after, duration)
                if blocking_row is None and (best_start is None or candidate_start < best_start):
                    best_start, best_asset, donor_plan = candidate_start, alt_asset_id, None

            # No scheduled-capacity machine had a pure idle slot. Draw on the
            # standby reserve BEFORE considering donor preemption — a reserve
            # machine carries no committed work by construction, so it is
            # available immediately and costs no other order anything. This is
            # the situation the 7% is held back for.
            #
            # Least-recently-drawn-on first, so repeated breakdowns spread
            # across the reserve instead of hammering one spare -- but each
            # candidate is still checked against committed_by_asset (which by
            # now may carry tentative bookings THIS assessment call already
            # made on that same asset, see _book_tentative_slot below), so two
            # affected operations can no longer both claim the same reserve
            # machine at an overlapping time just because neither had any
            # REAL committed history to conflict with.
            if best_start is None and reserve_candidates:
                for spare in sorted(reserve_candidates, key=lambda a: reserve_usage.get(a, 0)):
                    committed = committed_by_asset.get(spare, empty_committed)
                    candidate_start, blocking_row = _find_slot(committed, ready_after, duration)
                    if blocking_row is None:
                        reserve_usage[spare] = reserve_usage.get(spare, 0) + 1
                        best_start, best_asset, donor_plan = ready_after, spare, None
                        reserve_drawn += 1
                        logger.info(
                            f"Order {order_id} op {op['operation_id']}: no idle scheduled machine — "
                            f"covered immediately from the standby reserve (asset {spare}), "
                            f"no donor preemption needed."
                        )
                        break

            # Still nothing — only now consider taking a slot from another
            # order's committed schedule.
            if best_start is None:
                for alt_asset_id in alt_machines['asset_id']:
                    committed = committed_by_asset.get(alt_asset_id, empty_committed)
                    candidate_start, donor_op = _attempt_donor_preemption(
                        order_id, affected_urgency, duration, alt_asset_id,
                        committed, ready_after, manager_forced_orders,
                        rejections=preemption_rejections, assessment_now=assessment_now,
                    )
                    if candidate_start is not None and (best_start is None or candidate_start < best_start):
                        best_start, best_asset, donor_plan = candidate_start, alt_asset_id, donor_op

            # Impact is measured against ready_after (= max(planned_start,
            # now)), NOT against planned_start.
            #
            # BUG FIX: measuring from planned_start charged this breakdown for
            # every hour the operation was ALREADY overdue before the machine
            # failed. Most of this factory's committed schedule starts in the
            # past (the seeder lays down a plan beginning ~15 days ago), so a
            # breakdown whose work could be picked up by an idle alternate
            # machine *this second* was being scored at 339.5h of impact —
            # purely pre-existing tardiness. That pushed the fuzzy gate
            # straight past Severe into FULL_RESCHEDULE, which is why a real
            # run produced 78 FULL_RESCHEDULE / 130 NO_ACTION and never once
            # reached LOCAL_PATCH: the middle tier was unreachable.
            #
            # The question this number has to answer is "how much delay does
            # THIS EVENT add", so the baseline is the earliest the operation
            # could have started had the machine not broken, which is
            # ready_after.
            impact_hours = float('inf') if best_start is None else max(0.0, (best_start - ready_after).total_seconds() / 3600.0)

            # BUG FIX (GA_AGENT_POSTMORTEM.md Section 5c, confirmed for real
            # before this fix: the identical candidate_asset_id was
            # recommended for 5 different affected operations in one real
            # assessment call): book this operation's claim on best_asset
            # NOW, before the loop moves to the next affected operation, so a
            # later operation in the SAME breakdown sees this slot as taken
            # instead of independently "discovering" the same idle gap. A
            # donor preemption additionally moves the donor's OWN slot, so
            # its old booking is released and its new one recorded too.
            if best_asset is not None:
                if donor_plan is not None:
                    _release_tentative_slot(
                        committed_by_asset, best_asset,
                        donor_plan['production_order_id'], donor_plan['operation_id'],
                        schedule_item_id=donor_plan['schedule_item_id'])
                    _book_tentative_slot(
                        committed_by_asset, empty_committed, best_asset,
                        donor_plan['production_order_id'], donor_plan['operation_id'],
                        donor_plan['new_start'], donor_plan['new_end'],
                        schedule_item_id=donor_plan['schedule_item_id'])
                _book_tentative_slot(
                    committed_by_asset, empty_committed, best_asset,
                    order_id, op['operation_id'], best_start, best_start + duration,
                    schedule_item_id=int(op['schedule_item_id']))

        effective_impact = 999.0 if impact_hours == float('inf') else impact_hours
        decision = decide_tier(slack_days, effective_impact, manager_forced=forced)
        if impact_hours == float('inf'):
            decision['tier'] = Tier.FULL_RESCHEDULE
            decision['reason'] = "No alternate eligible machine (idle or via safe donor preemption) available — escalating."

        assessments.append({
            'schedule_item_id': int(op['schedule_item_id']),
            'original_snapshot': _schedule_snapshot(op),
            'production_order_id': order_id,
            'operation_id': str(op['operation_id']),
            'tier': decision['tier'].value if hasattr(decision['tier'], 'value') else decision['tier'],
            'reason': decision['reason'],
            'urgency_score': affected_urgency,
            'slack_days': slack_days,
            'impact_hours': None if impact_hours == float('inf') else impact_hours,
            'candidate_asset_id': best_asset,
            'candidate_line_id': (
                candidate_line_by_asset.get(int(best_asset))
                if best_asset is not None else None
            ),
            'candidate_start_time': best_start,
            'duration': duration,
            'original_asset_id': broken_asset_id,
            'from_standby_reserve': best_asset in reserved_asset_ids if best_asset is not None else False,
            'donor_plan': donor_plan,
            'preemption_rejections': list(preemption_rejections),
        })

    if reserve_drawn:
        logger.info(
            f"{reserve_drawn} of {len(assessments)} affected operation(s) covered from the standby "
            f"reserve without preempting any other order."
        )
    return assessments


def apply_local_patch(assessment: dict):
    """
    Executes a LOCAL_PATCH tier decision: reassigns the affected operation to
    its candidate alternate machine, and — if a donor preemption was part of
    the plan — bumps the donor operation to its new slot too. Both writes
    happen in one transaction so a failure doesn't leave a half-applied patch.
    """
    if (assessment.get('candidate_asset_id') is None
            or assessment.get('candidate_line_id') is None
            or assessment.get('candidate_start_time') is None):
        logger.error(
            f"Cannot apply local patch for order {assessment['production_order_id']}: "
            "candidate asset, line and slot are all required.")
        return False

    new_start = assessment['candidate_start_time']
    new_end = new_start + assessment['duration']
    donor = assessment.get('donor_plan')

    session_source = get_db_session()
    session = next(session_source)
    try:
        def update_one(item, values):
            snapshot = item.get('original_snapshot')
            row_id = item.get('schedule_item_id')
            if not snapshot or row_id is None or snapshot.get('schedule_item_id') != row_id:
                raise ValueError("Patch lacks a unique assessed row identity and original snapshot")
            # SQLAlchemy renders null-safe equality for the actual database
            # dialect. The original values and ID are checked atomically by
            # the UPDATE; no check-then-write race and no matched-set update.
            schedule = table('Job_Shop_Schedules', *(column(k, DateTime()) if isinstance(v, datetime) else column(k)
                                                       for k, v in snapshot.items()))
            if any(k not in schedule.c for k in values):
                raise ValueError("Patch snapshot is missing updated columns")
            conditions = [schedule.c[k].is_not_distinct_from(v) for k, v in snapshot.items()]
            result = session.execute(schedule.update().where(and_(*conditions)).values(**values))
            if result.rowcount != 1:
                raise RuntimeError(f"Schedule row {row_id} disappeared or changed since assessment; patch aborted")

        if donor is not None and donor.get('schedule_item_id') == assessment.get('schedule_item_id'):
            raise ValueError("Affected lot and donor cannot be the same schedule row")
        # Revalidate under resource reservation locks, since assessment is a
        # proposal and another writer may have committed a reservation since.
        reservations = table('Job_Shop_Schedules', column('schedule_item_id'),
            column('asset_id'), column('execution_status'),
            column('planned_start_time', DateTime()), column('planned_end_time', DateTime()))
        current = session.execute(select(reservations).where(
            reservations.c.asset_id == assessment['candidate_asset_id'],
            reservations.c.execution_status.in_(OCCUPYING_STATUSES)
        ).with_for_update()).mappings().all()
        moved_ids = {assessment.get('schedule_item_id')}
        placements = [(new_start, new_end)]
        if donor is not None:
            moved_ids.add(donor.get('schedule_item_id'))
            placements.append((donor['new_start'], donor['new_end']))
        conflicts = _placement_conflicts(pd.DataFrame(current), placements, moved_ids)
        mutual_overlap = len(placements) == 2 and placements[0][0] < placements[1][1] and placements[0][1] > placements[1][0]
        if conflicts or mutual_overlap:
            assessment['patch_rejection'] = {
                'code': 'PREEMPTION_RESERVATION_CONFLICT',
                'blocking_schedule_item_ids': conflicts,
                'proposed_intervals_overlap': mutual_overlap}
            raise ValueError(str(assessment['patch_rejection']))
        update_one(assessment, {
            'asset_id': assessment['candidate_asset_id'],
            'line_id': assessment['candidate_line_id'],
            'planned_start_time': new_start, 'planned_end_time': new_end,
        })
        if donor is not None:
            update_one(donor, {
                'planned_start_time': donor['new_start'],
                'planned_end_time': donor['new_end'],
            })

        session.commit()

        msg = (
            f"✅ Local patch: Order {assessment['production_order_id']} op {assessment['operation_id']} "
            f"moved {assessment['original_asset_id']} -> {assessment['candidate_asset_id']}, "
            f"new start {new_start} (impact {assessment['impact_hours']:.1f}h, within cushion)."
        )
        if donor is not None:
            msg += (
                f" Donor preemption: order {donor['production_order_id']} (slack "
                f"{donor['donor_slack_days']:.1f}d) bumped {donor['old_start']} -> {donor['new_start']}."
            )
        logger.info(msg)
        return True
    except Exception as e:
        session.rollback()
        logger.error(f"❌ Local patch failed for order {assessment['production_order_id']}: {e}")
        return False
    finally:
        session.close()
        session_source.close()
