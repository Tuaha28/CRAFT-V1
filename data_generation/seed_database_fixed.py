"""Generate resampled synthetic records and planned-calendar dispatches.

The 1,029 record count is calibrated from 139 genuine PO groups plus 890
StyleId+Color+BuyMonth proxy groups. Generated attributes are independently
sampled; original order tuples are not preserved. Machine/worker availability
and routing precedence have separate planned and simulated-actual calendars.
Actual overruns cascade; productive segments explicitly pause off shift.
These are synthetic scenarios, never observed company outcomes.
"""

import os
import sys
import pandas as pd
import mysql.connector
import random
import json
import math
from data_generation.simulation_calendar import split_quantity, work_on_calendar
from data_generation.operating_regimes import get_operating_regime
# Imported under an alias: this module DERIVES its own calibration factor
# from the source spreadsheets below and shadows the name locally. The
# config value is what ga/scheduler.py plans with, and is compared against
# the derived one so the two cannot drift apart silently.
from utils.config import (
    DURATION_CALIBRATION_FACTOR as CONFIG_DURATION_CALIBRATION_FACTOR,
    OUTPUT_DIR,
)
import heapq
from collections import defaultdict
from typing import NamedTuple
from faker import Faker
from datetime import datetime, timedelta, time
from dotenv import load_dotenv

# Run as `python data_generation/seed_database_fixed.py` from the project root:
# Python puts THIS file's directory on sys.path, not the working directory, so
# the project root has to be added explicitly for `utils` to import.
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.shift_rotation import (
    SHIFT_BANDS,
    assign_round_robin,
    batch_available_at,
    shift_band_for_batch,
)
from data_generation.routing_tables import (routing_for, routing_group_name, sam_by_step, sam_per_step,
                                            SHORT_SLEEVE_SHARE)

load_dotenv()
fake = Faker()

# --- FIX (security): no hardcoded password. Matches utils/config.py's
# pattern elsewhere in the project — DB_PASS must come from a local .env
# file (git-ignored), never committed source. ---
DB_PASS = os.getenv("DB_PASS")
if not DB_PASS:
    raise RuntimeError("DB_PASS is not set. Add it to your .env file — see .env.example.")

db_config = {
    'host': os.getenv("DB_HOST", "localhost"),
    'user': os.getenv("DB_USER", "root"),
    'password': DB_PASS,
    'database': os.getenv("DB_NAME", "garment_fyp_db")
}

# THREE SHIFTS, 8 HOURS EACH, BACK TO BACK -> CONTINUOUS 24h COVERAGE.
#
# This factory runs round the clock. Every employee is rostered to exactly one
# of these bands, and the bands tile the day with no gap, so the machines are
# available continuously and only the PEOPLE rotate.
#
# Previously every employee was given a single fixed "06:00-22:00" block and
# the machines were treated as idle for the other 8 hours. That was wrong in
# two directions at once: it understated machine availability by a third, and
# it made the company-wide labour utilisation ceiling 33.6% (8,302 rostered
# against 2,790 usable machines) when in reality only about a third of the
# workforce is ever on duty at the same moment.
# The bands themselves live in utils/shift_rotation.py, shared with the
# scheduler. Two copies of a rotation calendar would drift, and the drift
# would only show up as crews quietly rostered to the wrong shift.
SHIFTS = SHIFT_BANDS

# DATA v2 (opt-in; v1 stays the default and reproduces garment_fyp_db):
#   * order volume set by a target LOAD -- required worker-minutes over the
#     available operator worker-minutes of an ORDER WINDOW centred on the
#     reference date -- instead of the grid.xls extract's order count, which
#     loads the 8,500 operators to under 1%;
#   * per-step SAM split by published operation-bulletin weights
#     (routing_tables.sam_by_step), garment totals unchanged;
#   * machines placed on lines in proportion to line worker capacity;
#   * the company's real location (Ad-Dulayl Industrial Park QIZ);
#   * phase A streamed into phase B so the larger order book never holds
#     every lot row in memory (same decisions, same order).
SEED_DATA_VERSION = os.getenv("SEED_DATA_VERSION", "v1").strip().lower()
if SEED_DATA_VERSION not in ("v1", "v2"):
    raise ValueError(f"Unsupported SEED_DATA_VERSION={SEED_DATA_VERSION!r}")
DATA_V2 = SEED_DATA_VERSION == "v2"
SEED_LOAD_FACTOR = float(os.getenv("SEED_LOAD_FACTOR", "0.6"))
SEED_ORDER_WINDOW_DAYS = int(os.getenv("SEED_ORDER_WINDOW_DAYS", "56"))
if DATA_V2 and not (0 < SEED_LOAD_FACTOR <= 1.5 and 7 <= SEED_ORDER_WINDOW_DAYS <= 365):
    raise ValueError("SEED_LOAD_FACTOR must be in (0, 1.5] and SEED_ORDER_WINDOW_DAYS in [7, 365]")
COMPANY_LOCATION = "Ad-Dulayl Industrial Park (QIZ), Zarqa, Jordan"
# DURATION_CALIBRATION_FACTOR used to be a hardcoded 1.3 here, with a
# separate undocumented /50.0 divisor at its one use site -- both invented,
# no cited derivation. DATA_AUDIT.md Module 5 replaced it with a value
# computed fresh from real PPC-1.xlsx/Plan.xlsx/grid.xls data inside
# execute_complete_seed() (see the calibration block there), which shadows
# this module-level name. No longer defined here.


def shift_for_employee(employee_id):
    """
    A FLOATER's fixed band, written to Work_Schedules.

    Only meaningful for operators who end up in no batch. Batch members get a
    Work_Schedules row too (the table is the company roster and other queries
    read it), but for them it is NOT authoritative: a crew follows its batch's
    weekly rotation, so their real shift changes every week and cannot be
    represented by a single static row. See utils/shift_rotation.py.
    """
    return SHIFTS[employee_id % len(SHIFTS)]


def shift_covers(shift_entry, when):
    """
    Is this employee on duty at the given moment?

    shift_entry is (name, start_time, end_time) from employee_shift_lookup.
    Handles the night band, which wraps past midnight: 22:00-06:00 is on duty
    when the clock is at or after 22:00 OR before 06:00, not between them.
    Getting that backwards would silently roster the night crew to days.
    """
    if not shift_entry:
        return False
    _name, start, end = shift_entry
    t = when.time()
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def get_next_valid_shift_start(dt):
    """
    No-op under 24h coverage.

    The three shift bands tile the whole day, so there is no hour at which a
    machine is unavailable and nothing to snap forward to. Kept as a named
    function rather than deleted at the call sites: it documents that the
    question was considered, and it is the single place to reintroduce a
    restriction if the factory ever stops running nights.
    """
    return dt


def calculate_shift_aware_end_time(curr, duration_mins):
    """
    Also a straight addition now, for the same reason: work is no longer
    interrupted by an overnight close.

    This used to push any work extending past 22:00 to 06:00 the next morning,
    which inflated every long operation's elapsed span by 8 hours per day it
    ran. Operations here routinely run for days, so that was a substantial
    distortion of the seeded schedule.

    Operators still change over at the shift boundaries — that is handled
    where operators are ASSIGNED, not here, because the machine keeps running
    across the handover.
    """
    return curr + timedelta(minutes=duration_mins)


# --- Execution variance (real plan-vs-actual signal for the GNN delay label,
# see PROJECT_STATUS.txt Section 3 / 0D) --------------------------------
EXECUTION_VARIANCE_MEAN = 1.0
EXECUTION_VARIANCE_SD = 0.15
EXECUTION_VARIANCE_MIN = 0.65
EXECUTION_VARIANCE_MAX = 1.6

# Delay-label fix (see DELAY_LABEL_FIX_PROPOSAL.md, approved before this was
# written -- three independent methods confirmed the OLD pure-noise version
# of this function left the GNN delay label unlearnable: near-zero raw
# correlation against every feature, ~zero permutation importance on the
# trained model across all three heads, and delay loss flat for 143 of 150
# training epochs, unmoved even by the one LR cut). Each causal factor below
# is a bounded SHIFT to the draw's distribution parameters, never a direct
# assignment of lateness -- noise stays on top of every causal factor, an
# operation with a bad-history machine and an on-leave employee is MORE
# LIKELY to run long, not guaranteed to.
BREAKDOWN_EFFECT_CAP = 0.08       # OEE speed-loss ceiling (see proposal, EMQ)
BREAKDOWN_EFFECT_PER_COUNT = 0.01  # reasoned scaling, not independently cited
LEAVE_EFFECT = 0.10                # reasoned, within apparel-planning literature's ~25% total-friction envelope
QUEUE_SD_WIDEN_CAP = 0.5           # up to +50% SD at very high queue delay
QUEUE_SD_WIDEN_SCALE_MINS = 10000.0  # matched to this database's own measured queue-delay range


def sample_execution_variance_multiplier(
    rng=random,
    machine_breakdown_count_at_dispatch: float = 0.0,
    on_leave: bool = False,
    queue_delay_mins: float = 0.0,
    regime="stress",
):
    """
    Synthetic execution-speed multiplier for an operation
    than its own planned duration -- independent of where in the schedule (or
    relative to "now") that operation sits, which is exactly what the old
    execution_status-derived delay label was accidentally keyed to.

    Three causal factors, each shifting the draw's distribution parameters
    rather than assigning an outcome directly (DELAY_LABEL_FIX_PROPOSAL.md
    Section 2 has the full reasoning and citations; summarized here):

      - machine_breakdown_count_at_dispatch (this machine's PRIOR breakdown
        history, not this lot's own dynamic_breakdown roll -- that already
        has its own separate, larger, discrete effect via repair_time_mins
        at the call site): shifts the MEAN up, +1% per prior breakdown,
        capped at +8% -- OEE literature's typical speed-loss figure for
        "performance rate" losses distinct from breakdown time itself.
      - on_leave: shifts the MEAN up by a flat +10% when true. The current
        frozen-plan dataset excludes known-on-leave workers during Phase A, so
        Phase B passes False and this channel contributes zero rows. The
        parameter remains available for an explicit future absence scenario;
        it must not be described as active evidence in the current dataset.
      - queue_delay_mins (how long this lot waited before starting):
        widens the SD, NOT the mean, up to +50% at very high queue delay.
        Deliberately different in kind from the other two: queue delay
        already shifts planned and actual start together (see the call
        site), so a mean-shift on top would double-count the same
        congestion. WIP/queueing literature supports increased
        UNPREDICTABILITY under congestion specifically, not a directional
        slowdown -- this encodes that distinction rather than ignoring it.

    All defaults are 0/False/0, reproducing the exact old pure-noise
    behaviour for any caller that doesn't pass them -- this is an additive
    generalization, not a breaking change to the function's contract.

    Clipped normal, not unbounded: an operation finishing at 3x or 0.1x its
    planned duration isn't "variance", it's a different, already-modelled
    event. EXECUTION_VARIANCE_MIN/MAX are unchanged by this fix -- checked
    against the maximum realistic stacking (breakdown 8% + leave 10% =
    effective mean 1.18, SD up to 0.225) before leaving them as-is: the
    existing clip bounds still contain the large majority of the shifted
    distribution, only genuinely extreme draws get clipped.
    """
    profile = get_operating_regime(regime)
    breakdown_effect = min(profile.breakdown_effect_cap, machine_breakdown_count_at_dispatch * profile.breakdown_effect_per_count)
    leave_effect = LEAVE_EFFECT if on_leave else 0.0
    effective_mean = profile.execution_variance_mean + breakdown_effect + leave_effect

    queue_sd_multiplier = 1.0 + min(profile.queue_sd_widen_cap, queue_delay_mins / profile.queue_sd_widen_scale_mins * profile.queue_sd_widen_cap)
    effective_sd = profile.execution_variance_sd * queue_sd_multiplier

    return min(profile.execution_variance_max, max(profile.execution_variance_min,
        rng.gauss(effective_mean, effective_sd)))


def compute_queue_delay_mins(earliest_possible, prev_end):
    """
    How long a lot waited for a busy machine or employee to free up, beyond
    when it was otherwise ready to start (its own previous operation's
    completion, prev_end). This is the genuine queueing-congestion signal.

    REPLACES `start_t - earliest_possible`, the original formula, which is
    IDENTICALLY ZERO for every lot, always: start_t =
    get_next_valid_shift_start(earliest_possible), and that function has been
    a documented no-op (`return dt`) since the 3-shift/24h-availability model
    was built, so start_t == earliest_possible by construction. The original
    has_causal_delay check built on that formula (`if queue_delay_mins >
    duration_mins * 1.5`) has therefore been silently dead code since that
    migration too -- discovered only when this quantity was persisted as a
    GNN feature and measured to have zero variance across 7,500 operations.
    See PROJECT_STATUS.txt Section 0E / reversal 8.

    earliest_possible = max(machine_free_time, employee_free_time, prev_end)
    already contains the real congestion: if the assigned machine or
    employee wasn't free the moment this lot was precedence-ready
    (prev_end), the gap between them IS the wait. Floored at 0 defensively
    (earliest_possible can never be earlier than prev_end by construction,
    but a wrong caller order should read as "no wait" rather than a negative
    number).
    """
    return max(0.0, (earliest_possible - prev_end).total_seconds() / 60.0)


class FrozenPlanRow(NamedTuple):
    """One immutable lot-operation assignment produced by planning phase A."""

    production_order_id: int
    lot_index: int
    lot_quantity: int
    operation_index: int
    operation_id: int
    operation_type: str
    machine_type: str
    factory_id: int
    line_id: int
    asset_id: int
    employee_id: int
    batch_id: object
    crew: object
    band: object
    efficiency_rating: float
    duration_minutes: float
    order_start_time: datetime
    delivery_deadline: datetime
    is_final_operation: bool
    planned_start_time: datetime
    planned_end_time: datetime
    planned_work_segments: tuple


class ActualSimulationRow(NamedTuple):
    """Synthetic phase-B outcome for one already-frozen plan row."""

    plan: FrozenPlanRow
    actual_start_time: datetime
    actual_end_time: datetime
    actual_work_segments: tuple
    queue_delay_mins_at_dispatch: float
    machine_status_at_dispatch: str
    machine_breakdown_count_at_dispatch: int
    employee_on_leave_at_dispatch: bool
    dynamic_breakdown: bool
    execution_status: str


def build_frozen_planned_schedule(
    *,
    order_ids,
    prod_order_details,
    product_routing_map,
    base_date,
    base_time,
    factory_capacity,
    capable_machines_by_type,
    asset_to_line_map,
    line_to_factory_map,
    qualified_operators_by_op_factory,
    employee_leave_lookup,
    employee_batch_lookup,
    line_to_batches,
    batch_offsets,
    employee_shift_lookup,
    employee_skill_lookup,
    order_deadlines,
    duration_calibration_factor,
    planning_rng,
    target_lot_size=20,
    max_lots_per_order=60,
):
    """Build the complete planned schedule without consulting actual state.

    Phase A owns every planning decision: factory, machine, worker, line,
    order, start, end, and predecessor readiness. Phase B receives these
    immutable rows and may delay their execution, but may never choose a
    different resource or feed an outcome back into this function.
    """
    ownership, stats = {}, {"batch": 0, "factory": 0, "global": 0, "off_shift": 0}
    plans = list(iter_frozen_planned_schedule(
        order_ids=order_ids, prod_order_details=prod_order_details,
        product_routing_map=product_routing_map, base_date=base_date, base_time=base_time,
        factory_capacity=factory_capacity, capable_machines_by_type=capable_machines_by_type,
        asset_to_line_map=asset_to_line_map, line_to_factory_map=line_to_factory_map,
        qualified_operators_by_op_factory=qualified_operators_by_op_factory,
        employee_leave_lookup=employee_leave_lookup, employee_batch_lookup=employee_batch_lookup,
        line_to_batches=line_to_batches, batch_offsets=batch_offsets,
        employee_shift_lookup=employee_shift_lookup, employee_skill_lookup=employee_skill_lookup,
        order_deadlines=order_deadlines, duration_calibration_factor=duration_calibration_factor,
        planning_rng=planning_rng, target_lot_size=target_lot_size,
        max_lots_per_order=max_lots_per_order, ownership=ownership, stats=stats))
    return plans, ownership, stats


def iter_frozen_planned_schedule(
    *,
    order_ids,
    prod_order_details,
    product_routing_map,
    base_date,
    base_time,
    factory_capacity,
    capable_machines_by_type,
    asset_to_line_map,
    line_to_factory_map,
    qualified_operators_by_op_factory,
    employee_leave_lookup,
    employee_batch_lookup,
    line_to_batches,
    batch_offsets,
    employee_shift_lookup,
    employee_skill_lookup,
    order_deadlines,
    duration_calibration_factor,
    planning_rng,
    target_lot_size,
    max_lots_per_order,
    ownership,
    stats,
):
    """Phase A as a stream: identical decisions, one plan row at a time.

    ``ownership`` and ``stats`` are filled in place as orders are planned, so
    a v2 order book far larger than v1 never holds every row in memory.
    """
    eligible_workers = {
        key: [e for e in pool if not employee_leave_lookup[e]]
        for key, pool in qualified_operators_by_op_factory.items()
    }
    planned_machine_heaps = {}
    for machine_type, machines in capable_machines_by_type.items():
        for asset_id in machines:
            line_id = asset_to_line_map[asset_id]
            factory_id = line_to_factory_map[line_id]
            planned_machine_heaps.setdefault((machine_type, factory_id), []).append(
                (base_time, asset_id)
            )
    for heap in planned_machine_heaps.values():
        heapq.heapify(heap)

    planned_employee_free = {
        employee_id: base_time
        for workers in eligible_workers.values()
        for employee_id in workers
    }
    order_arrival = sorted(
        order_ids,
        key=lambda oid: (prod_order_details[oid].get("start_date", base_date), oid),
    )

    for po_id in order_arrival:
        po_data = prod_order_details[po_id]
        operations_chain = product_routing_map[po_data["product_id"]]
        order_start = datetime.combine(
            po_data.get("start_date", base_date), datetime.min.time()
        )
        order_start = max(order_start, base_time)
        lot_quantities = split_quantity(
            po_data["target_qty"], target_lot_size, max_lots_per_order
        )
        feasible_factories = [
            factory_id
            for factory_id in factory_capacity
            if all(
                planned_machine_heaps.get((op["machine_type"], factory_id))
                and eligible_workers.get((op["operation_type"], factory_id))
                for op in operations_chain
            )
        ]
        if not feasible_factories:
            raise ValueError(
                f"Order {po_id}: no factory has all required machines and "
                "qualified present workers"
            )
        weights = [factory_capacity[f] for f in feasible_factories]
        if sum(weights) <= 0:
            order_factory = planning_rng.choice(feasible_factories)
        else:
            order_factory = planning_rng.choices(
                feasible_factories, weights=weights, k=1
            )[0]
        ownership[str(po_id)] = int(order_factory)
        deadline = order_deadlines[po_id]

        for lot_index, stage_qty in enumerate(lot_quantities):
            planned_prev_end = order_start
            for op_index, op_info in enumerate(operations_chain):
                heap_key = (op_info["machine_type"], order_factory)
                _free_at, asset_id = heapq.heappop(planned_machine_heaps[heap_key])
                line_id = asset_to_line_map[asset_id]
                candidates = eligible_workers[
                    (op_info["operation_type"], order_factory)
                ]
                employee_id = min(
                    candidates, key=lambda e: (planned_employee_free[e], e)
                )
                batch_id = employee_batch_lookup.get(employee_id)
                if batch_id in line_to_batches.get(line_id, ()):
                    stats["batch"] += 1
                else:
                    stats["factory"] += 1
                crew = batch_offsets.get(batch_id)
                band = employee_shift_lookup[employee_id]
                efficiency = employee_skill_lookup[employee_id][
                    op_info["operation_type"]
                ]
                duration = (
                    duration_calibration_factor
                    * stage_qty
                    * op_info["sam"]
                    / (efficiency / 100.0)
                )
                ready = max(
                    _free_at,
                    planned_employee_free[employee_id],
                    planned_prev_end,
                )
                start, end, segments = work_on_calendar(
                    ready, duration, crew, band
                )
                plan = FrozenPlanRow(
                    production_order_id=int(po_id),
                    lot_index=int(lot_index),
                    lot_quantity=int(stage_qty),
                    operation_index=int(op_index),
                    operation_id=int(op_info["op_id"]),
                    operation_type=str(op_info["operation_type"]),
                    machine_type=str(op_info["machine_type"]),
                    factory_id=int(order_factory),
                    line_id=int(line_id),
                    asset_id=int(asset_id),
                    employee_id=int(employee_id),
                    batch_id=batch_id,
                    crew=crew,
                    band=band,
                    efficiency_rating=float(efficiency),
                    duration_minutes=float(duration),
                    order_start_time=order_start,
                    delivery_deadline=deadline,
                    is_final_operation=(op_index == len(operations_chain) - 1),
                    planned_start_time=start,
                    planned_end_time=end,
                    planned_work_segments=tuple(segments),
                )
                planned_employee_free[employee_id] = end
                planned_prev_end = end
                heapq.heappush(planned_machine_heaps[heap_key], (end, asset_id))
                yield plan


def simulate_actual_from_frozen_plan(
    frozen_plan,
    *,
    base_time,
    now_time,
    actual_seed,
    asset_status_lookup,
    regime="stress",
):
    """Yield phase-B outcomes while preserving every phase-A assignment."""
    rng = random.Random(actual_seed)
    profile = get_operating_regime(regime)
    machine_free = {}
    employee_free = {}
    machine_runtime = {}
    breakdown_count = {}
    predecessor_end = {}

    for plan in frozen_plan:
        asset_id = plan.asset_id
        employee_id = plan.employee_id
        lot_key = (plan.production_order_id, plan.lot_index)
        previous_end = predecessor_end.get(lot_key, plan.order_start_time)
        ready = max(
            machine_free.get(asset_id, base_time),
            employee_free.get(employee_id, base_time),
            previous_end,
            plan.planned_start_time,
        )
        dispatch, _one_minute_end, _dispatch_segments = work_on_calendar(
            ready, 1, plan.crew, plan.band
        )
        queue_delay = compute_queue_delay_mins(dispatch, previous_end)
        prior_breakdowns = breakdown_count.get(asset_id, 0)
        runtime = machine_runtime.get(asset_id, 0.0) + plan.duration_minutes
        dynamic_breakdown = (
            runtime > profile.dynamic_breakdown_runtime_threshold_mins
            and rng.random() < profile.dynamic_breakdown_probability
        )
        repair_minutes = (
            rng.randint(profile.repair_min_minutes, profile.repair_max_minutes)
            if dynamic_breakdown else 0
        )
        if dynamic_breakdown:
            runtime = 0.0
            breakdown_count[asset_id] = prior_breakdowns + 1
        else:
            breakdown_count.setdefault(asset_id, prior_breakdowns)
        machine_runtime[asset_id] = runtime
        on_leave = False
        actual_work = plan.duration_minutes * sample_execution_variance_multiplier(
            rng=rng,
            machine_breakdown_count_at_dispatch=prior_breakdowns,
            on_leave=on_leave,
            queue_delay_mins=queue_delay,
            regime=profile,
        )
        actual_start, actual_end, segments = work_on_calendar(
            dispatch + timedelta(minutes=repair_minutes),
            actual_work,
            plan.crew,
            plan.band,
        )
        machine_free[asset_id] = actual_end
        employee_free[employee_id] = actual_end
        predecessor_end[lot_key] = actual_end

        causal_delay = (
            asset_status_lookup.get(asset_id, "Active")
            in {"Breakdown", "Under_Maintenance"}
            or dynamic_breakdown
            or on_leave
            or queue_delay > plan.duration_minutes * 1.5
            or (
                plan.is_final_operation
                and plan.planned_end_time > plan.delivery_deadline
            )
        )
        if actual_end <= now_time:
            status = "Done"
        elif actual_start > now_time:
            status = "Scheduled"
        else:
            status = "Delayed" if causal_delay else "Running"

        yield ActualSimulationRow(
            plan=plan,
            actual_start_time=actual_start,
            actual_end_time=actual_end,
            actual_work_segments=tuple(segments),
            queue_delay_mins_at_dispatch=float(queue_delay),
            machine_status_at_dispatch="Active",
            machine_breakdown_count_at_dispatch=int(prior_breakdowns),
            employee_on_leave_at_dispatch=bool(on_leave),
            dynamic_breakdown=bool(dynamic_breakdown),
            execution_status=status,
        )

SEED = 42  # DATA_AUDIT.md module-by-module rebuild: fixed so consecutive
# reseeds differ ONLY by whatever code changed between them, not by fresh
# randomness on top of it. Previously unseeded -- every prior reseed (this
# session's routing fix included) mixed a real code-change effect with
# random re-draw noise, which is exactly the "combined result I can't
# attribute" the module-by-module process is meant to avoid.


def execute_complete_seed(*, reference_time=None, planning_seed=SEED, actual_seed=SEED, operating_regime="stress"):
    # Capture immutable provenance BEFORE any generation or destructive DDL.
    import hashlib
    import uuid
    from pathlib import Path
    source_root = Path(__file__).resolve().parent
    input_paths = [source_root / n for n in ('grid.xls', 'Plan.xlsx', 'PPC-1.xlsx', 'WorkStation.xlsx')]
    source_paths = [source_root / n for n in ('seed_database_fixed.py', 'simulation_calendar.py', 'validate_seed.py', 'operating_regimes.py', 'routing_tables.py')]
    source_hashes = {str(p.relative_to(source_root)): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in input_paths + source_paths}
    dataset_version = str(uuid.uuid4())
    regime_profile = get_operating_regime(operating_regime)
    print("Deploying Production-Grade Constrained 27-Table Data Layer...")
    random.seed(planning_seed)
    fake.seed_instance(planning_seed)
    reference_time = reference_time or datetime.now().replace(microsecond=0)
    conn = None
    cursor = None

    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor()

        cursor.execute("CREATE TABLE IF NOT EXISTS Dataset_Calibration (singleton_id INT PRIMARY KEY, payload LONGTEXT NOT NULL) ENGINE=InnoDB")
        # ORDER -> FACTORY OWNERSHIP (Batch-4 fix 5b).
        #
        # THE single source of truth for which branch produces an order.
        # ga/chromosome.py used to re-derive this on every construction, and
        # graph/edge_builder.py will need the same answer to scope contention
        # cliques -- two independent derivations of one quantity is the exact
        # drift risk this batch exists to remove. Written once here, at seed
        # time, and read by both.
        #
        # NOT inferrable from Job_Shop_Schedules: the pre-fix seeder spread
        # every one of the 1,029 orders across ~5.76 factories, so deriving
        # ownership from existing schedule rows would just re-import the
        # violation. It has to be recorded at generation time.
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS Order_Factory_Ownership ("
            "production_order_id VARCHAR(64) PRIMARY KEY, "
            "factory_id INT NOT NULL, "
            "assigned_at DATETIME NOT NULL"
            ") ENGINE=InnoDB"
        )
        cursor.execute("SET FOREIGN_KEY_CHECKS = 0;")
        cursor.execute("SET UNIQUE_CHECKS = 0;")

        tables_to_reset = [
            'Job_Shop_Schedules', 'WIP_Tracking', 'Quality_Logs', 'Machine_Status_Logs',
            'Operator_Skill_Matrix', 'Machinery_Assets', 'batch_members', 'employee_batches', 'Employees', 'Routing_Operations',
            'Products', 'Production_Lines', 'Departments', 'Factories', 'Companies',
            'Purchase_Orders', 'Production_Orders', 'Optimization_Metrics_Log',
            'Machine_Capabilities', 'Work_Schedules', 'Product_Variants', 'Order_Factory_Ownership',
            'Bill_of_Materials', 'Raw_Materials', 'Suppliers', 'Inventory_Stock', 'Delivery_Logistics'
        ]
        # 1. Read input Excel files BEFORE touching the database.
        #
        # ORDERING MATTERS: TRUNCATE is DDL and auto-commits in MySQL, so the
        # `conn.rollback()` in the exception handler at the bottom of this
        # function cannot undo it. Reading these files after truncating meant
        # that any problem with them — a missing file, a missing engine
        # package — emptied all 26 tables and then failed, with the rollback
        # reporting success while leaving nothing behind. That happened: a
        # missing openpyxl wiped a fully seeded database.
        #
        # Anything that can fail and does not need the database now happens
        # first, so the truncate is only reached once the inputs are known good.
        #
        # Paths are resolved relative to THIS FILE, not the working directory.
        # They were bare relative paths, so the seeder only ran correctly when
        # invoked from inside data_generation/ and silently failed from the
        # project root — which is where every other entry point runs from.
        data_dir = os.path.dirname(os.path.abspath(__file__))
        ws_path = os.path.join(data_dir, "WorkStation.xlsx")
        grid_path = os.path.join(data_dir, "grid.xls")
        # Plan.xlsx: the source of real per-garment-type GarmentSmv (routing_
        # spec.md Section 5) -- previously loaded nowhere in this file.
        plan_path = os.path.join(data_dir, "Plan.xlsx")
        # PPC-1.xlsx: real DAILY per-line production output -- DATA_AUDIT.md
        # Module 5, previously loaded nowhere in this file at all.
        ppc_path = os.path.join(data_dir, "PPC-1.xlsx")
        for path in (ws_path, grid_path, plan_path, ppc_path):
            if not os.path.exists(path):
                raise FileNotFoundError(
                    f"Required seed input not found: {path}. Refusing to truncate the database."
                )
        df_ws = pd.read_excel(ws_path, engine='openpyxl')
        df_grid = pd.read_excel(grid_path, engine='xlrd')
        df_plan = pd.read_excel(plan_path, engine='openpyxl')
        df_ppc = pd.read_excel(ppc_path, engine='openpyxl')
        df_ws = df_ws[df_ws['IsEnabled'] == 1]

        if '_seed_' not in db_config['database']:
            raise ValueError("Seeder requires an isolated database containing '_seed_' in its name; live reset refused")
        required = [(df_ws, ['Department', 'LineName', 'Worker Capacity']),
                    (df_grid, ['StyleId', 'GarmentType', 'PO', 'PlanTarget', 'StartDate']),
                    (df_plan, ['GarmentType', 'GarmentSmv'])]
        for frame, columns in required:
            if frame.empty or any(column not in frame for column in columns):
                raise ValueError(f"Missing/empty calibration data: {columns}")
        if df_ws['Department'].nunique() != 6:
            raise ValueError("Expected six company factory branches")
        print("Truncating isolated staging tables to reset state...")
        for t in tables_to_reset:
            cursor.execute(f"TRUNCATE TABLE {t};")

        # Hoisted from Section 4 (Employees), where it used to be first
        # defined -- Module 1/3's real-data-distribution setup below needs
        # it earlier, and it's a pure, dependency-free statement, safe to
        # compute this early.
        base_date = reference_time.date()
        # Synthetic crew target per line, independent of the confirmed
        # company-wide headcount. Unfilled crew slots do not add employees.
        CREWS_PER_LINE = 3

        real_styles = df_grid['StyleId'].dropna().unique().tolist()
        real_categories = df_grid['GarmentType'].dropna().unique().tolist()
        if not real_categories:
            real_categories = ['Activewear Pant', 'Performance Short', 'Polo Blouse', 'U Neck Tee', 'Sport Jacket']

        # Real per-category frequency, for weighted (not uniform) garment-
        # type assignment below -- routing_spec.md Section 6.3: "Assign
        # garment type from the real distribution in grid.xls (Pant 2,140,
        # U Neck 1,099, ...) rather than uniformly at random." Computed
        # fresh from the file rather than hardcoded, so it stays faithful
        # to whatever grid.xls actually contains.
        category_counts = df_grid['GarmentType'].dropna().value_counts()
        category_weights = [float(category_counts.get(c, 1)) for c in real_categories]

        # Real per-garment-type total SMV (routing_spec.md Section 5), mean
        # across Plan.xlsx's rows for that GarmentType. Four real grid.xls
        # categories (Helmet, Jump Suit, Tube, Full Zip Hoodie) have no
        # Plan.xlsx SMV rows at all -- routing_spec.md Section 6.3 calls for
        # using the real GarmentSmv directly, so those four fall back to
        # their own ROUTING GROUP's average among the types that do have
        # one (closer than a single dataset-wide number), and finally to
        # the dataset-wide mean if even that is somehow empty.
        smv_by_type_raw = df_plan.groupby('GarmentType')['GarmentSmv'].mean().to_dict()
        _dataset_wide_smv_mean = float(df_plan['GarmentSmv'].mean())
        smv_by_type = {}
        for cat in real_categories:
            if cat in smv_by_type_raw:
                smv_by_type[cat] = float(smv_by_type_raw[cat])
        for cat in real_categories:
            if cat not in smv_by_type:
                group = routing_group_name(cat)
                group_vals = [
                    smv_by_type_raw[c] for c in real_categories
                    if c in smv_by_type_raw and routing_group_name(c) == group
                ]
                smv_by_type[cat] = float(sum(group_vals) / len(group_vals)) if group_vals else _dataset_wide_smv_mean

        # Real efficiency values (Plan.xlsx.Efficency) -- DATA_AUDIT.md
        # Module 1, corrected mid-implementation after checking the join
        # key: Plan.xlsx.LineId CANNOT be trusted to identify a specific
        # real WorkStation.xlsx line. Verified directly: Plan.xlsx's own
        # LineName/Line text columns are a single placeholder value
        # ("Dummy Line" / "LINE-DUMMY") for EVERY one of 1,382 rows, and
        # the same numeric LineId appears under up to 10 different BrId
        # (branch) values -- impossible for a real physical line, which
        # belongs to exactly one branch. A per-line join on this column
        # would have silently attributed real numbers to the wrong lines.
        #
        # Correct granularity instead: real efficiency VALUES, sampled
        # from the true observed distribution (mean 77.8%, std 5.6%,
        # n=1,382), assigned per line WITHOUT a false per-line identity
        # claim -- this is real data at the precision it actually
        # supports (company-wide distribution), not per-line attribution
        # the source can't back up. Replaces the previous hardcoded 85.00
        # constant either way.
        real_efficiency_values = df_plan['Efficency'].dropna().tolist()
        _dataset_wide_efficiency_mean = float(df_plan['Efficency'].mean())

        # Real order-level dates/quantities (grid.xls) -- DATA_AUDIT.md
        # Module 3, replacing Purchase_Orders/Production_Orders' invented
        # ~25-30-day release-date spread (PROJECT_STATUS.txt Section 1.2.2)
        # and the invented 1,500-12,000 quantity range.
        #
        # grid.xls.PO is mostly missing (139 of 5,132 rows have a real PO;
        # BalanceQty is non-numeric and not usable), so quantity is sourced
        # from grouping the rows that DO have a real PO -- per-PO total
        # PlanTarget, n=139, real mean ~452, median 330, range 52-3,300.
        # This is a MUCH smaller real scale than the invented 1,500-12,000
        # range -- a genuine, expected finding (the standing caution: real
        # data was expected to make numbers look different, not better).
        _po_groups = df_grid.dropna(subset=['PO']).groupby('PO').agg(
            start_date=('StartDate', 'min'), end_date=('EndDate', 'max'),
            exfty_date=('ExFtyDate', 'max'), total_target=('PlanTarget', 'sum'),
        )
        real_po_quantities = _po_groups['total_target'].dropna().tolist()

        # Real ORDER COUNT (Module 6, DATA_REBUILD_LOG.md) -- Module 3
        # grounded what's inside each order (dates, quantity) but left the
        # order COUNT itself at an invented flat 1,500, the same category
        # of gap employee headcount had before Module 4.
        #
        # grid.xls's PO is a genuine real order identifier (verified: PO
        # 5324 is one StyleId+Color split across 33 Size rows, summing to
        # a clean 3,300 -- the exact "one order, split by size" shape
        # expected), but only 334 of 5,132 rows (6.5%) carry a non-null PO.
        # The other 93.5% supply proxy groups for count calibration only.
        #
        # StyleId+Color ALONE is demonstrably too coarse: checked against
        # the 139 rows that DO have a real PO, 130 of 448 StyleId+Color
        # groups among the no-PO rows would span more than one BuyMonth --
        # and BuyMonth is NEVER split within a single known-real PO (139/139
        # constant), so a grouping that spans multiple BuyMonths is
        # provably merging separate real orders. StyleId+Color+StartDate is
        # too fine in the other direction: StartDate genuinely varies
        # WITHIN a single known-real PO for 31 of 139 orders (up to 14
        # distinct dates in one order -- different colour/size splits
        # starting on different lines/days), so using it as a boundary
        # over-splits real orders that share one PO into several.
        # BuyMonth is the one field checked and confirmed to never split a
        # real order -- adopted as the disambiguator for the unlabeled 93.5%.
        _no_po_rows = df_grid[df_grid['PO'].isna()]
        _no_po_order_groups = _no_po_rows.groupby(['StyleId', 'Color', 'BuyMonth']).size()
        if DATA_V2:
            # v2 resamples a much larger order book, so draw quantities from
            # all 1,029 real order-equivalents: the 139 PO totals plus the 890
            # StyleId+Color+BuyMonth proxy groups' real PlanTarget sums
            # (the same grouping v1 already trusts for the order COUNT).
            _proxy_quantities = _no_po_rows.groupby(['StyleId', 'Color', 'BuyMonth'])['PlanTarget'].sum()
            real_po_quantities = real_po_quantities + [q for q in _proxy_quantities.tolist() if q > 0]
        # Count calibration only: all records resample attributes independently;
        # this does not copy the 139 genuine or 890 proxy source tuples.
        TOTAL_ORDERS = len(_po_groups) + len(_no_po_order_groups)
        print(
            f"Real order-equivalent count (grid.xls, PO groups + "
            f"StyleId+Color+BuyMonth groups for the unlabeled majority): "
            f"{TOTAL_ORDERS} (was an invented flat 1,500)."
        )

        # Row-level (not PO-grouped) real StartDate distribution -- n=5,131,
        # real span 2024-01-02 to 2025-02-04 (~399 days), steeply front-
        # loaded (median 2024-02-25, only 54 days after the min -- half the
        # real orders sit in that first 54-day burst, the other half spread
        # across the remaining ~345-day tail). RE-ANCHORED, not used at its
        # literal historical date.
        #
        # ANCHOR CHOICE, measured before deciding: first tried mapping the
        # real dataset's LATEST observed date to today (the most literally
        # "faithful to the file's own as-of point" choice). Measured
        # consequence, checked before adopting it: 1,492 of 1,500 simulated
        # orders ended up with a deadline already in the past relative to
        # "today" -- because the whole 399-day, front-loaded real spread
        # then lands almost entirely BEFORE today, this anchor makes the
        # order book degenerately, uniformly overdue before the simulation
        # even starts. Rejected. Anchoring the real MEDIAN start date to
        # today instead gives a live-snapshot mix much closer to a real
        # rolling factory: 49.9% of orders already started, 32.4% already
        # past deadline (real, bounded, not degenerate -- some genuinely
        # short-lead-time orders that started recently ARE already late,
        # which is a plausible state for a live factory, not a modelling
        # artifact). Adopted.
        _real_dates = df_grid.dropna(subset=['StartDate']).copy()
        _real_median_start = _real_dates['StartDate'].median()
        _date_reanchor_offset = base_date - _real_median_start.date()
        # Cast to plain date objects (not pandas Timestamp) -- matches the
        # type every downstream consumer (datetime.combine, timedelta
        # arithmetic, the DATE columns these values are inserted into)
        # already expects from this code path.
        real_start_dates = [(d.date() + _date_reanchor_offset) for d in _real_dates['StartDate'].tolist()]

        # Real gap distributions (row-level, n~5,131), for scheduled_end_
        # date and delivery_deadline -- both measured directly rather than
        # kept at the previous invented ranges (end: random 5-15 days;
        # deadline: random 30-60 days after base_date, unrelated to the
        # order's own start). ETADate was checked and rejected as a
        # deadline proxy: it PRECEDES StartDate for most rows (mean -26
        # days), consistent with being a materials-arrival date, not a
        # customer delivery commitment. ExFtyDate (ex-factory / ship date)
        # is the plausible one: mostly positive gap from StartDate (mean
        # 23.8 days), filtered to positive values only here since a
        # deadline before an order's own start is not representable in
        # this schema.
        _end_gaps = (_real_dates['EndDate'] - _real_dates['StartDate']).dt.days
        real_end_gap_days = [int(g) for g in _end_gaps.dropna().tolist() if g >= 0]
        _lead_gaps = (_real_dates['ExFtyDate'] - _real_dates['StartDate']).dt.days
        real_deadline_lead_days = [int(g) for g in _lead_gaps.dropna().tolist() if g > 0]

        # 2. Seed Corporate & Factory Structure
        cursor.execute(
            "INSERT INTO Companies (company_id, name, registration_number, headquarters_address) "
            "VALUES (1, 'Needle Craft Jordan', 'NC-JOR-AMM-2000', %s)",
            (COMPANY_LOCATION if DATA_V2 else 'Amman Industrial Estate, Jordan',)
        )

        factories_detected = df_ws['Department'].dropna().unique()
        fact_map = {}
        for idx, f_name in enumerate(factories_detected, start=1):
            estate = COMPANY_LOCATION if DATA_V2 else "Al-Hassan Industrial Estate, Jordan"
            cursor.execute(
                "INSERT INTO Factories (factory_id, company_id, name, location) VALUES (%s, 1, %s, %s)",
                (idx, f"{f_name} Branch", f"{f_name} Manufacturing Block, {estate}")
            )
            fact_map[f_name] = idx

        # --- FIX (root cause of the 84% machine/line factory mismatch):
        # every line's TRUE home factory needs to be recorded so machines
        # can inherit it later instead of being assigned a random one. ---
        line_to_factory_map = {}
        # Real per-line crew capacity (WorkStation.xlsx's Worker Capacity
        # column -- PROJECT_STATUS.txt Section 1.2.4), replacing the
        # uniform random.choice([32, 48, 64]) crew-size draw below. Read as
        # a PER-SHIFT figure (the magnitude matches the old per-crew draw
        # range, not 3x it) -- interpretation documented rather than
        # assumed silently, since the column itself doesn't say. Shift
        # COUNT per line stays the existing CREWS_PER_LINE=3 for now (that
        # fix -- 2x12h vs 3x8h -- was assessed and deliberately deferred,
        # see Section 1.2.3); this only makes crew SIZE real.
        worker_capacity_by_line = {}

        # layout_type / line_type: real WorkStation.xlsx columns (P/H/W and
        # E/L respectively) stored for completeness -- DATA_AUDIT.md Module
        # 1 investigated whether either gates which garment types run on a
        # line (cross-tabbed against Plan.xlsx's real GarmentType-by-LineId
        # history) and found no discriminating signal: LineType is 79 "E"
        # vs 20 "L" among enabled lines, and virtually every real production
        # row in Plan.xlsx ran on an "E" line regardless of garment type.
        # Stored as real data (bucket a) but NOT wired into any functional
        # dependency (e.g. product-to-line routing) that this investigation
        # doesn't support -- that would be inventing a correlation the data
        # doesn't show, not using real data.
        for col, ddl in (
            ('layout_type', "ALTER TABLE Production_Lines ADD COLUMN layout_type VARCHAR(5) NULL"),
            ('line_type', "ALTER TABLE Production_Lines ADD COLUMN line_type VARCHAR(5) NULL"),
        ):
            cursor.execute("""
                SELECT COUNT(*) FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'Production_Lines' AND COLUMN_NAME = %s
            """, (col,))
            if cursor.fetchone()[0] == 0:
                print(f"Adding Production_Lines.{col} column...")
                cursor.execute(ddl)

        line_ids = []
        for _, row in df_ws.iterrows():
            f_id = fact_map.get(row['Department'], 1)
            cursor.execute(
                "INSERT IGNORE INTO Departments (department_id, factory_id, name, floor_number) VALUES (%s, %s, %s, 1)",
                (f_id, f_id, f"{row['Department']} Floor")
            )
            l_id_int = int(row['ID'])
            # Drawn from the real observed efficiency distribution (see
            # real_efficiency_values above) -- NOT a per-line lookup, since
            # that join key was verified unreliable. Falls back to the
            # dataset-wide mean only if the real value list is somehow
            # empty.
            real_efficiency = random.choice(real_efficiency_values) if real_efficiency_values else _dataset_wide_efficiency_mean
            layout_type = str(row.get('LayoutType')) if pd.notna(row.get('LayoutType')) else None
            line_type = str(row.get('LineType')) if pd.notna(row.get('LineType')) else None
            cursor.execute(
                "INSERT INTO Production_Lines (line_id, department_id, line_number, target_efficiency_percentage, is_active, layout_type, line_type) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (l_id_int, f_id, str(row['LineName']), round(real_efficiency, 2), True, layout_type, line_type)
            )
            line_ids.append(l_id_int)
            line_to_factory_map[l_id_int] = f_id  # FIX: record it
            wc = row.get('Worker Capacity')
            worker_capacity_by_line[l_id_int] = int(wc) if pd.notna(wc) and wc > 0 else 48

        # 3. Seed Products & Routing Operations
        print("Seeding product specifications and routings...")
        machine_types = ["Single-Needle Lockstitch", "Overlock Serger (4-Thread)", "Flatlock Coverstitch", "Automated Pattern Tacker", "Feed-off-the-Arm Special"]
        operation_types = ["Body Assembly", "Seam Edge Overlock", "Flatlock Hemming", "Bar-Tack Pocketing", "Tubular Sleeve Join"]
        # machine_type -> operation_type, the same canonical machine-class
        # <-> skill-category pairing utils/machine_taxonomy.py mirrors.
        # LOOKED UP BY NAME below, not by position: routing_tables.py's
        # per-garment-type tables no longer place these five types in one
        # fixed order or count (routing_spec.md / PROJECT_STATUS.txt
        # Section 1.2.1 -- the old `machine_types[step-1]` positional
        # pairing is exactly the invented-template defect this replaces).
        optype_by_machine_type = dict(zip(machine_types, operation_types))

        # Machine-type DEMAND share, for machine allocation in Section 5
        # (DATA_AUDIT.md Module 2). BUCKET (b), not (a) -- no file gives a
        # real machine-count-by-type; this is a principled DERIVATION from
        # real data (routing_tables.py's literature-derived routings x
        # grid.xls's real per-category population share), not a directly-
        # sourced real number. Computed structurally, from routing STEP
        # COUNTS per type rather than actual per-order work-minutes,
        # because Machinery_Assets is seeded (Section 5) before Production_
        # Orders' real quantities exist (Section 7) -- reordering the
        # seeder to use economically-weighted demand would be a bigger
        # change for a small refinement. Cross-checked directly against the
        # routing-fix run's measured real operation-count shares
        # (PROJECT_STATUS.txt Section 1.3's supply/demand table) before
        # adopting this: structural 29.6/30.1/15.0/13.4/11.9% vs measured
        # 31.0/29.3/14.0/13.9/11.8% for SNLS/Overlock/Flatlock/Tacker/FOA
        # respectively -- close enough (the gap is real per-order quantity
        # weighting, not a modelling error) to use as the allocation basis.
        _demand_total_weight = sum(category_weights)
        machine_type_demand = {m: 0.0 for m in machine_types}

        def _step_shares(cat, routing):
            # v1: equal per step (structural step counts). v2: the same
            # bulletin work shares that split each garment's SAM below, so
            # machine allocation follows where the work actually is.
            if DATA_V2:
                from data_generation.routing_tables import step_weight_fractions
                return step_weight_fractions(routing, routing_group_name(cat))
            return [1 / len(routing)] * len(routing)
        for cat, w in zip(real_categories, category_weights):
            share = w / _demand_total_weight
            if cat in ("Blouse", "Dress"):
                # Blend both sleeve-length variants at SHORT_SLEEVE_SHARE --
                # the per-product routing loop below (Section 3, products
                # loop) draws the same variant randomly per product, this
                # just keeps the pre-computed aggregate demand consistent
                # with what actually gets seeded rather than assuming the
                # long-sleeve base for 100% of Blouse/Dress.
                for routing, variant_share in (
                    (routing_for(cat, short_sleeve=False), 1 - SHORT_SLEEVE_SHARE),
                    (routing_for(cat, short_sleeve=True), SHORT_SLEEVE_SHARE),
                ):
                    for (_, m_type), step_share in zip(routing, _step_shares(cat, routing)):
                        machine_type_demand[m_type] += share * variant_share * step_share
            else:
                routing = routing_for(cat)
                for (_, m_type), step_share in zip(routing, _step_shares(cat, routing)):
                    machine_type_demand[m_type] += share * step_share
        _demand_sum = sum(machine_type_demand.values())

        # Real duration-formula calibration (DATA_AUDIT.md Module 5, the
        # single largest fidelity gain flagged in the audit -- the previous
        # DURATION_CALIBRATION_FACTOR=1.3 and its accompanying /50.0
        # divisor, used below, were both invented with no cited derivation).
        #
        # PPC-1.xlsx's `Output` column is ambiguous, investigated and
        # documented rather than assumed: it caps hard at exactly 100.0 for
        # 72.7% of rows, with the rest clustering on clean fractions (50,
        # 25, 75, 33.33, 66.67...) -- the signature of a percentage/
        # achievement-rate field, not a literal unit count. `TotalQty` also
        # turned out NOT to be constant within a PO (~9.7 distinct values
        # per PO on average), ruling out a clean per-PO "total real hours /
        # total real quantity" grouping. Resolution adopted (documented,
        # not asserted as certain): treat Output as literal daily units at
        # ROW level (the simpler, still-defensible reading), giving a real
        # WorkHrs*60/Output "minutes per unit" distribution, n=4,052 valid
        # rows, median 2.51 min/unit -- used as the calibration target.
        #
        # SCALE NOTE: this real figure reflects a whole PRODUCTION LINE's
        # aggregate daily rate (many operators working different operations
        # in parallel), not one operator's single-operation SMV -- using it
        # to directly replace GarmentSmv would conflate the two. Instead it
        # calibrates the formula's OVERALL SCALE against the mean real
        # per-step SAM and mean real efficiency (both already computed
        # above), so a typical lot's synthetic duration lands at the real
        # observed per-unit rate rather than an asserted constant:
        #   FACTOR = real_median_min_per_unit * mean_efficiency / mean_sam_per_step
        _ppc_valid = df_ppc[(df_ppc['Output'] > 0) & (df_ppc['WorkHrs'] > 0)]
        _real_median_min_per_unit = float((_ppc_valid['WorkHrs'] * 60 / _ppc_valid['Output']).median())
        _mean_real_efficiency = (sum(real_efficiency_values) / len(real_efficiency_values) / 100.0) if real_efficiency_values else 0.85
        def _expected_routing_len(cat):
            # Same SHORT_SLEEVE_SHARE blend as the demand computation above,
            # for Blouse/Dress; every other category has one fixed length.
            if cat in ("Blouse", "Dress"):
                return (
                    (1 - SHORT_SLEEVE_SHARE) * len(routing_for(cat, short_sleeve=False))
                    + SHORT_SLEEVE_SHARE * len(routing_for(cat, short_sleeve=True))
                )
            return len(routing_for(cat))

        _mean_sam_per_step = sum(
            (w / _demand_total_weight) * (smv_by_type[cat] / _expected_routing_len(cat))
            for cat, w in zip(real_categories, category_weights)
        )
        DURATION_CALIBRATION_FACTOR = _real_median_min_per_unit * _mean_real_efficiency / _mean_sam_per_step
        # DRIFT CHECK against utils/config.py, which is what ga/scheduler.py
        # plans with. This factor is re-derived from the spreadsheets on
        # every seed run, so it can move; if it moves and config does not,
        # the GA silently goes back to planning against a different
        # calibration than the database was generated with -- the exact
        # divergence that made GA durations 1.21x the trained-on ones.
        # Warned loudly rather than auto-corrected: changing what the
        # scheduler plans with is a decision, not a side effect of reseeding.
        if abs(DURATION_CALIBRATION_FACTOR - CONFIG_DURATION_CALIBRATION_FACTOR) > 1e-4:
            print(
                f"  *** WARNING: derived DURATION_CALIBRATION_FACTOR="
                f"{DURATION_CALIBRATION_FACTOR:.4f} differs from "
                f"utils/config.py's {CONFIG_DURATION_CALIBRATION_FACTOR:.4f}. "
                f"ga/scheduler.py plans with the config value, so update it to "
                f"{DURATION_CALIBRATION_FACTOR:.4f} or the scheduler will plan "
                f"against a different calibration than this database. ***"
            )
        print(
            f"Duration formula calibrated against PPC-1.xlsx: real median "
            f"{_real_median_min_per_unit:.2f} min/unit -> "
            f"DURATION_CALIBRATION_FACTOR={DURATION_CALIBRATION_FACTOR:.4f} (was 1.3, with a "
            f"separate undocumented /50.0 divisor now folded in and removed)."
        )

        product_ids = []
        variant_ids = []
        product_routing_map = {}
        product_total_smv = {}
        op_counter = 1
        var_counter = 1

        # DELIBERATE SYNTHETIC CHOICE, documented per DATA_AUDIT.md's
        # rebuild scope decision: Product_Variants (fed by sizes/colors
        # below) is checked and confirmed to have ZERO downstream
        # consumers -- not graph/, ga/, agent/, models/, or the dashboard.
        # A real source DOES exist for this specific table (grid.xls's
        # Size/Color columns -- real values include a genuinely varied
        # size vocabulary: XS-6XL with petite/regular/tall/long modifiers,
        # youth sizes, and 148 distinct numeric/named colour codes), but
        # it is sparse per real style (most of the 485 real styles have no
        # usable Size+Color pairing at all, and the ones that do range
        # from 1 to 359 distinct combinations with no consistent pattern).
        # Grounding it would be real, non-trivial effort for a table that
        # affects no measured project outcome -- explicitly not done, in
        # favour of the tables that do matter. Kept as an invented, fixed
        # 4x4 grid.
        sizes = ['S', 'M', 'L', 'XL']
        colors = ['Black', 'Navy', 'Grey', 'White']

        for idx, style in enumerate(real_styles, start=1):
            # Weighted by grid.xls's real per-category frequency (routing_
            # spec.md Section 6.3), not uniform -- GarmentType is now
            # LOAD-BEARING (it selects a real routing table and a real SMV
            # below), not decorative.
            cat = random.choices(real_categories, weights=category_weights, k=1)[0]
            cursor.execute(
                "INSERT INTO Products (product_id, style_code, name, category) VALUES (%s, %s, %s, %s)",
                (idx, str(style), f"NeedleCraft Elite {cat} Style {style}", cat)
            )
            product_ids.append(idx)
            product_routing_map[idx] = []

            for size in sizes:
                for color in colors:
                    sku = f"SKU-{style}-{size}-{color[:3].upper()}"
                    cursor.execute(
                        "INSERT INTO Product_Variants (variant_id, product_id, size, color, sku) VALUES (%s, %s, %s, %s, %s)",
                        (var_counter, idx, size, color, sku)
                    )
                    variant_ids.append(var_counter)
                    var_counter += 1

            # Literature-derived per-garment-type routing (routing_tables.py),
            # replacing the invented `for step in range(1, 6)` uniform
            # 5-step template. Step count now varies 6-10 by garment type
            # (routing_spec.md Section 3); total SAM comes from this
            # category's REAL GarmentSmv (Plan.xlsx), split evenly across
            # its own routing's steps -- not a random per-step draw.
            #
            # Blouse/Dress: routing_spec.md 3.3's short-sleeve variant
            # (8 steps, cuff attach omitted) applied per-product at
            # SHORT_SLEEVE_SHARE -- no real field exists to key which
            # specific garments are short-sleeved (see routing_tables.py's
            # WOVEN_SHORT_SLEEVE comment), so this is a documented bucket
            # (c) synthetic choice, not a bucket (a) real assignment.
            is_short_sleeve = cat in ("Blouse", "Dress") and random.random() < SHORT_SLEEVE_SHARE
            routing = routing_for(cat, short_sleeve=is_short_sleeve)
            total_smv = smv_by_type.get(cat, _dataset_wide_smv_mean)
            if DATA_V2:
                step_sams = sam_by_step(total_smv, routing, routing_group_name(cat))
            else:
                step_sams = [round(sam_per_step(total_smv, len(routing)), 2)] * len(routing)
            product_total_smv[idx] = sum(step_sams)

            for step, (op_name, m_type) in enumerate(routing, start=1):
                op_type = optype_by_machine_type[m_type]
                per_step_sam = step_sams[step - 1]

                cursor.execute("""
                    INSERT INTO Routing_Operations
                    (operation_id, product_id, operation_sequence_number, operation_name, standard_allowed_minutes, required_machine_type)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (op_counter, idx, step, op_name, per_step_sam, m_type))

                product_routing_map[idx].append({
                    'op_id': op_counter,
                    'sequence': step,
                    'sam': per_step_sam,
                    'machine_type': m_type,
                    'operation_type': op_type
                })
                op_counter += 1

        # 4. Seed Employees
        print("Seeding personnel files and multi-skill efficiency matrix...")

        # Company-confirmed total: 9,000 employees across all six factories.
        # Line Worker Capacity describes staffing capacity, not headcount;
        # multiplying it by shift crews must not create extra employees.
        # Per-factory capacity remains a proxy for distributing this fixed
        # total until an actual factory-level employee roster is available.
        factory_capacity = defaultdict(float)
        for l_id, capacity in worker_capacity_by_line.items():
            f_id = line_to_factory_map.get(l_id)
            if f_id is not None:
                factory_capacity[f_id] += capacity
        TOTAL_EMPLOYEES = 9000
        _factory_ids_weighted = list(factory_capacity.keys())
        _factory_weights = [factory_capacity[f] for f in _factory_ids_weighted]
        # Preserves the previous Operator:Supervisor/Mechanic ratio
        # (8,500:500 of 9,000, ~94.4%) -- no real source distinguishes role
        # mix, so the RATIO stays a documented synthetic choice (bucket c),
        # applied to the company-confirmed total (including support roles).
        OPERATOR_COUNT = int(round(TOTAL_EMPLOYEES * (8500 / 9000)))
        print(f"Company-confirmed workforce total: {TOTAL_EMPLOYEES} ({OPERATOR_COUNT} operators; role mix synthetic)")

        employee_batch = []
        work_schedules_batch = []
        skills_batch = []
        skill_counter = 1

        employee_skill_lookup = {}
        employee_leave_lookup = {}
        employee_factory_lookup = {}  # FIX: needed to filter candidates by factory later
        # employee_id -> (shift_name, start_time, end_time). Used when picking
        # an operator for a lot so the person assigned is actually on duty at
        # the hour the lot runs.
        employee_shift_lookup = {}
        qualified_operators_by_op = {op: [] for op in operation_types}
        # FIX: same pool, but split by factory too, so we can prefer
        # same-factory candidates without scanning the global pool every time.
        qualified_operators_by_op_factory = defaultdict(list)

        for i in range(1, TOTAL_EMPLOYEES + 1):
            emp_role = 'Operator' if i <= OPERATOR_COUNT else random.choice(['Line_Supervisor', 'Mechanic'])
            # Weighted by real per-factory total Worker Capacity, replacing
            # the previous uniform random.choice across factories.
            emp_factory = random.choices(_factory_ids_weighted, weights=_factory_weights, k=1)[0]
            employee_batch.append((i, emp_factory, fake.first_name(), fake.last_name(), emp_role, base_date - timedelta(days=random.randint(30, 730)), True))
            employee_factory_lookup[i] = emp_factory

            is_on_leave = random.random() < 0.08
            employee_leave_lookup[i] = is_on_leave
            # Real per-employee shift assignment, evenly split across the
            # three 8-hour bands (see SHIFTS above). Was a single hardcoded
            # "06:00:00"-"22:00:00" for all 9,000 employees.
            shift_name, shift_start, shift_end = shift_for_employee(i)
            employee_shift_lookup[i] = (shift_name, shift_start, shift_end)
            work_schedules_batch.append(
                (i, i, base_date, shift_start.strftime("%H:%M:%S"),
                 shift_end.strftime("%H:%M:%S"), is_on_leave)
            )

            if emp_role == 'Operator':
                employee_skill_lookup[i] = {}
                assigned_ops = random.sample(operation_types, k=random.choice([1, 2]))
                for op_t in assigned_ops:
                    efficiency = round(random.uniform(70.0, 98.0), 2)
                    employee_skill_lookup[i][op_t] = efficiency
                    skills_batch.append((skill_counter, i, op_t, efficiency, base_date - timedelta(days=5)))
                    skill_counter += 1
                    qualified_operators_by_op[op_t].append(i)
                    qualified_operators_by_op_factory[(op_t, emp_factory)].append(i)  # FIX

        cursor.executemany("INSERT INTO Employees (employee_id, factory_id, first_name, last_name, role, hire_date, is_active) VALUES (%s, %s, %s, %s, %s, %s, %s)", employee_batch)
        cursor.executemany("INSERT INTO Work_Schedules (schedule_id, employee_id, shift_date, start_time, end_time, is_on_leave) VALUES (%s, %s, %s, %s, %s, %s)", work_schedules_batch)
        cursor.executemany("INSERT INTO Operator_Skill_Matrix (skill_id, employee_id, operation_type, efficiency_rating, last_evaluated_date) VALUES (%s, %s, %s, %s, %s)", skills_batch)

        # 4.5 SEED EMPLOYEE BATCHES
        print("Forming Employee Batches and Team Memberships...")
        batches_data = []
        batch_members_data = []
        # employee_id -> batch_id, so the scheduling loop can tell a crew
        # member (availability from the batch rotation) from a floater
        # (availability from Work_Schedules).
        employee_batch_lookup = {}
        batch_counter = 1

        supervisors_by_factory = defaultdict(list)
        for employee in employee_batch:
            if employee[4] == 'Line_Supervisor':
                supervisors_by_factory[employee[1]].append(employee[0])
        operators = [e[0] for e in employee_batch if e[4] == 'Operator']
        random.shuffle(operators)

        # A BATCH IS A WHOLE CREW THAT MOVES BETWEEN SHIFTS TOGETHER.
        #
        # Batches were previously drawn from one global operator pool, and
        # once employees were split across three shift bands a single ~48
        # person batch was scattered across all three. That made a batch
        # useless as a crew — the line's own team could rarely cover its own
        # work at any given hour, and operator sourcing collapsed from
        # 6,547 batch / 68 factory to 718 batch / 5,882 factory.
        #
        # Now: operators are partitioned by FACTORY, each batch takes its whole
        # team from one factory's pool, and the crew rotates through the three
        # shifts as a unit (utils/shift_rotation.py). A batch is therefore
        # shift-pure at any moment in time without being pinned to one band
        # forever.
        #
        # Up to three batches per line, so a line with a full complement has
        # one crew on each band in any given week — the round-robin offsets
        # guarantee the three sit on three different shifts. Lines whose
        # factory has run out of operators get fewer, or none, and are crewed
        # by the floater tiers at scheduling time. Pool exhaustion still
        # happens because three crews per line is an assumed staffing target,
        # while company-wide employee inventory is capped at 9,000. Empty
        # slots remain unfilled; crew targets never increase headcount.
        pool_by_factory = defaultdict(list)
        for emp_id in operators:
            pool_by_factory[employee_factory_lookup[emp_id]].append(emp_id)
        pool_cursor = {f: 0 for f in pool_by_factory}

        # (line_id, crew_slot) -> batch_id. The crew slot is just which of the
        # line's up-to-three teams it is; which SHIFT that team works depends
        # on the date, via its rotation offset.
        line_to_batches = defaultdict(list)
        batch_offsets = {}          # batch_id -> (rotation_offset, rest_day_offset)
        batches_skipped_no_pool = 0

        for l_id in line_ids:
            factory_of_line = line_to_factory_map.get(l_id, 1)
            pool = pool_by_factory.get(factory_of_line, [])
            for _crew_slot in range(CREWS_PER_LINE):
                start_idx = pool_cursor.get(factory_of_line, 0)
                # THIS line's real per-shift crew capacity (WorkStation.xlsx
                # Worker Capacity -- PROJECT_STATUS.txt Section 1.2.4),
                # replacing the old uniform random.choice([32, 48, 64])
                # draw. Falls back to 48 (that draw's own midpoint) for any
                # line missing from the lookup, which shouldn't happen since
                # line_ids is itself derived from the same df_ws rows.
                team_size = worker_capacity_by_line.get(l_id, 48)
                members = pool[start_idx:start_idx + team_size]
                if not members:
                    batches_skipped_no_pool += 1
                    continue

                # Round-robin on batch creation order, NOT random: keeps shift
                # coverage balanced across crews and spreads rest days evenly
                # over all seven weekdays.
                rotation_offset, rest_day_offset = assign_round_robin(batch_counter - 1)

                eligible_supervisors = supervisors_by_factory[factory_of_line]
                if not eligible_supervisors:
                    raise ValueError(
                        f"Factory {factory_of_line}, line {l_id}: no line supervisor available"
                    )
                sup_id = random.choice(eligible_supervisors)
                primary_skill = random.choice(operation_types)
                batch_name = f"Line {l_id} - Crew {_crew_slot + 1} - {fake.word().capitalize()}"

                batches_data.append((
                    batch_counter, batch_name, l_id, sup_id, primary_skill, 1,
                    rotation_offset, rest_day_offset
                ))
                line_to_batches[l_id].append(batch_counter)
                batch_offsets[batch_counter] = (rotation_offset, rest_day_offset)
                for emp_id in members:
                    batch_members_data.append((batch_counter, emp_id))
                    employee_batch_lookup[emp_id] = batch_counter
                pool_cursor[factory_of_line] = start_idx + len(members)
                batch_counter += 1

        lines_with_full_cover = sum(1 for l in line_ids if len(line_to_batches[l]) == CREWS_PER_LINE)
        print(
            f"Formed {len(batches_data)} rotating crews across {len(line_ids)} lines "
            f"({lines_with_full_cover} lines have all {CREWS_PER_LINE}; "
            f"{batches_skipped_no_pool} crew slots had no operators left in that factory). "
            f"Batch members: {len(batch_members_data)}."
        )

        # rotation_offset / rest_day_offset make a crew's calendar queryable
        # directly from its row. Added here rather than in a separate migration
        # because the schema is created outside this script; guarded so
        # re-running against an already-migrated database is safe.
        for col, ddl in (
            ('rotation_offset', "ALTER TABLE employee_batches ADD COLUMN rotation_offset INT NULL"),
            ('rest_day_offset', "ALTER TABLE employee_batches ADD COLUMN rest_day_offset INT NULL"),
        ):
            cursor.execute("""
                SELECT COUNT(*) FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'employee_batches' AND COLUMN_NAME = %s
            """, (col,))
            if cursor.fetchone()[0] == 0:
                print(f"Adding employee_batches.{col} column...")
                cursor.execute(ddl)

        cursor.executemany("INSERT INTO employee_batches (batch_id, batch_name, line_id, supervisor_id, primary_skill, is_active, rotation_offset, rest_day_offset) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", batches_data)
        cursor.executemany("INSERT INTO batch_members (batch_id, employee_id) VALUES (%s, %s)", batch_members_data)

        # FIX: precompute, per (batch_id, operation_type), which of that
        # batch's members actually hold the needed skill. This is what lets
        # the scheduling loop below prefer "a real member of this line's
        # team who can do this job" instead of picking a random employee
        # from anywhere in the company and slapping a batch_id on the row.
        batch_qualified_by_batch_op = defaultdict(list)
        for b_id, emp_id in batch_members_data:
            for op_t in employee_skill_lookup.get(emp_id, {}):
                batch_qualified_by_batch_op[(b_id, op_t)].append(emp_id)

        # 5. Seed Machinery & Asset-Line Mapping
        print("Deploying machinery assets and mapping line relationships...")
        # Allocated proportional to real routing-derived demand
        # (machine_type_demand above), replacing the previous uniform 600
        # machines per type regardless of how often that type is actually
        # used. DATA_AUDIT.md Module 2 / bucket (b): a principled
        # derivation from real data, not a directly-sourced real machine
        # count (no such source exists in any of the four files).
        TOTAL_MACHINES = 3000
        machine_count_by_type = {
            m: round(TOTAL_MACHINES * machine_type_demand[m] / _demand_sum) for m in machine_types
        }
        # Rounding can leave the total a few machines off 3000; correct on
        # the largest-share type so the total stays exactly TOTAL_MACHINES
        # (every downstream capacity/reserve calculation assumes this).
        _rounding_gap = TOTAL_MACHINES - sum(machine_count_by_type.values())
        _largest_type = max(machine_count_by_type, key=machine_count_by_type.get)
        machine_count_by_type[_largest_type] += _rounding_gap
        print(f"Machine allocation by type (demand-proportional, was uniform 600 each): {machine_count_by_type}")

        machine_pool = []
        for m_type in machine_types:
            machine_pool.extend([m_type] * machine_count_by_type[m_type])

        machine_batch = []
        capabilities_batch = []
        status_logs_batch = []
        cap_counter = 1
        log_counter = 1

        # DELIBERATE SYNTHETIC CHOICE (Machine_Status_Logs, populated via
        # status_logs_batch below and again during the Job_Shop_Schedules
        # simulation): checked and confirmed zero downstream consumers --
        # not read by database/queries.py or any of graph/, ga/, agent/,
        # models/, or the dashboard. None of the four real company files
        # contain machine-status/maintenance-log data either, so this
        # would be bucket (c) even if it mattered. A pure audit trail;
        # not worth grounding for a table nothing reads.
        machine_statuses = ['Active', 'Under_Maintenance', 'Idle', 'Breakdown']
        machine_status_weights = [90, 5, 3, 2]

        asset_status_lookup = {}
        asset_to_line_map = {}
        capable_machines_by_type = {m: [] for m in machine_types}

        coverage_lines = {}
        if DATA_V2:
            # Every branch keeps at least one Active machine of each type, so
            # a small 2-line branch is not left unable to run any routing by
            # line-weighted chance (v2 at 30% gave branch 6 zero orders).
            # No source assigns machine types to branches; this is the
            # minimum that keeps every branch able to produce.
            for m_type in machine_types:
                indices = [i for i, t in enumerate(machine_pool, start=1) if t == m_type]
                for f_id, asset_index in zip(sorted(set(line_to_factory_map.values())), indices):
                    lines_f = [l for l in line_ids if line_to_factory_map[l] == f_id]
                    coverage_lines[asset_index] = random.choices(
                        lines_f, weights=[worker_capacity_by_line[l] for l in lines_f], k=1)[0]

        for i in range(1, 3001):
            serial = f"SYNTH-SN-{i:08d}"
            m_type = machine_pool[i-1]
            m_status = random.choices(machine_statuses, weights=machine_status_weights, k=1)[0]
            if i in coverage_lines:
                m_status = 'Active'
                assigned_line = coverage_lines[i]
            elif DATA_V2:
                # A 34-worker half line gets half the machines of a 68-worker
                # line (v1 drew lines uniformly: 1.5-4.1 employees/machine).
                assigned_line = random.choices(
                    line_ids, weights=[worker_capacity_by_line[l] for l in line_ids], k=1)[0]
            else:
                assigned_line = random.choice(line_ids)
            # FIX (root cause of the 84% mismatch): the machine's factory is
            # now DERIVED from its assigned line's true home factory, not
            # picked independently at random. A machine can't belong to a
            # different branch than the line it's physically bolted to.
            assigned_factory = line_to_factory_map.get(assigned_line, 1)

            asset_status_lookup[i] = m_status
            asset_to_line_map[i] = assigned_line

            machine_batch.append((i, assigned_factory, assigned_line, f"Juki-{m_type[:4].upper()}-{random.randint(10,99)}", serial, base_date - timedelta(days=random.randint(100, 1000)), m_status))

            corr_op = operation_types[machine_types.index(m_type)]
            capabilities_batch.append((cap_counter, i, corr_op, random.choice([3500, 4000, 4500, 5000])))
            cap_counter += 1

            if m_status == 'Active':
                capable_machines_by_type[m_type].append(i)

            status_reason = {
                'Active': 'Initial routing verification log',
                'Under_Maintenance': 'Scheduled preventive maintenance',
                'Idle': 'Awaiting line reassignment',
                'Breakdown': 'Unplanned mechanical failure reported'
            }[m_status]
            status_logs_batch.append((log_counter, i, m_status, status_reason))
            log_counter += 1

        cursor.executemany("INSERT INTO Machinery_Assets (asset_id, factory_id, line_id, machine_model, serial_number, purchase_date, current_status) VALUES (%s, %s, %s, %s, %s, %s, %s)", machine_batch)
        cursor.executemany("INSERT INTO Machine_Capabilities (capability_id, asset_id, operation_type, max_rpm) VALUES (%s, %s, %s, %s)", capabilities_batch)
        cursor.executemany("INSERT INTO Machine_Status_Logs (log_id, asset_id, status_changed_to, reason) VALUES (%s, %s, %s, %s)", status_logs_batch)
        status_logs_batch = []

        # 6. Seed Inventory, Raw Materials, and BOM
        #
        # DELIBERATE SYNTHETIC CHOICE, all four tables in this section
        # (Suppliers, Raw_Materials, Inventory_Stock, Bill_of_Materials):
        # checked and confirmed zero downstream consumers -- not
        # database/queries.py's active pipeline queries, not graph/, ga/,
        # agent/, models/, or the dashboard. Inventory_Stock/Raw_Materials
        # DO have one query function, fetch_inventory_state()
        # (database/queries.py), but its only caller anywhere in this
        # codebase is that same file's own __main__ diagnostic block --
        # written "for the RAG-Enabled Conversational Stock Intelligence
        # Agent," a feature this project's own status record confirms is
        # configured but does not exist. Nothing in the live pipeline
        # calls it. Separately, none of the four real company files
        # contain supply-chain/inventory/BOM data at all, so this would be
        # bucket (c) regardless. Kept fully invented -- fictional supplier
        # names/emails, a fixed 5-material list, fabricated consumption
        # ratios -- not worth grounding for tables nothing reads.
        print("Seeding massive supply chain inventory nodes and BOM details...")
        suppliers_batch = [
            (1, "Jordan Active Trims Ltd", "supply@activejordan.jo", 10, 98.50),
            (2, "Amman Textures Corp", "orders@ammantextures.com", 14, 95.00),
            (3, "Aqaba Thread Specialists", "info@aqabathread.jo", 7, 99.10)
        ]
        cursor.executemany("INSERT INTO Suppliers (supplier_id, name, contact_email, lead_time_days, reliability_score) VALUES (%s, %s, %s, %s, %s);", suppliers_batch)

        raw_materials_batch = [
            (1, "Performance Polyester Knit Fabric", "Fabric", "Meters"),
            (2, "High-Tensile Elastic Sewing Thread", "Thread", "Kgs"),
            (3, "Ergonomic Sport Snap Buttons", "Button", "Pieces"),
            (4, "YKK Heavy Duty Outerwear Zippers", "Zipper", "Pieces"),
            (5, "Woven Eco-Friendly Brand Labels", "Label", "Pieces")
        ]
        cursor.executemany("INSERT INTO Raw_Materials (material_id, name, material_type, unit_of_measurement) VALUES (%s, %s, %s, %s);", raw_materials_batch)

        inventory_tracker = {}
        inventory_batch = []
        stock_counter = 1
        warehouse_zones = ["WH-Alpha-ZoneA", "WH-Beta-Row1", "WH-Gamma-Rack3", "WH-Delta-Bulk"]
        
        for mat_id, _, mat_type, _ in raw_materials_batch:
            for supp_id in [1, 2, 3]:
                for _ in range(5):
                    qty = random.uniform(20000.0, 80000.0) if mat_type == "Fabric" else random.uniform(50000.0, 200000.0)
                    inventory_batch.append((stock_counter, mat_id, supp_id, round(qty, 4), random.choice(warehouse_zones)))
                    inventory_tracker[stock_counter] = {'mat_id': mat_id, 'qty': qty}
                    stock_counter += 1

        cursor.executemany(
            "INSERT INTO Inventory_Stock (stock_id, material_id, supplier_id, current_quantity, warehouse_location) "
            "VALUES (%s, %s, %s, %s, %s);",
            inventory_batch
        )

        bom_batch = []
        bom_map = {}
        bom_counter = 1
        for p_id in product_ids:
            bom_map[p_id] = []
            for mat_id in [1, 2, 5]:
                req_qty = random.uniform(1.2, 2.5) if mat_id == 1 else random.uniform(0.02, 0.08)
                bom_batch.append((bom_counter, p_id, mat_id, round(req_qty, 4), 5.00))
                bom_map[p_id].append({'material_id': mat_id, 'req_qty': req_qty})
                bom_counter += 1
        cursor.executemany("INSERT INTO Bill_of_Materials (bom_id, product_id, material_id, required_quantity, wastage_allowance_percentage) VALUES (%s, %s, %s, %s, %s)", bom_batch)

        # 7. Generate Initial Order Metadatas
        print("Preparing order contexts...")
        prod_order_details = {}
        order_meta_cache = {}
        load_calibration = None
        if DATA_V2:
            # Size the order book to a target LOAD of the available operator
            # time in an order window centred on the reference date. Start
            # dates keep grid.xls's real (re-anchored) distribution inside the
            # window; quantities, products, end gaps and deadline leads stay
            # the same real distributions as v1. Only the COUNT is derived.
            half = timedelta(days=SEED_ORDER_WINDOW_DAYS / 2)
            window_starts = [d for d in real_start_dates if base_date - half <= d < base_date + half]
            if not window_starts:
                raise ValueError("No real start dates fall inside the order window")
            real_start_dates = window_starts
            present_operators = sum(1 for e in employee_batch
                                    if e[4] == 'Operator' and not employee_leave_lookup[e[0]])
            available_minutes = present_operators * 8 * 60 * (6 / 7) * SEED_ORDER_WINDOW_DAYS
            efficiencies = [v for skills in employee_skill_lookup.values() for v in skills.values()]
            mean_efficiency = sum(efficiencies) / len(efficiencies)
            mean_qty = sum(real_po_quantities) / len(real_po_quantities)
            mean_product_smv = sum(product_total_smv.values()) / len(product_total_smv)
            minutes_per_order = (DURATION_CALIBRATION_FACTOR * mean_qty * mean_product_smv
                                 / (mean_efficiency / 100.0))
            TOTAL_ORDERS = max(1, round(SEED_LOAD_FACTOR * available_minutes / minutes_per_order))
            load_calibration = {
                "load_factor": SEED_LOAD_FACTOR, "order_window_days": SEED_ORDER_WINDOW_DAYS,
                "window_start": (base_date - half).isoformat(), "window_end": (base_date + half).isoformat(),
                "present_operators": present_operators, "available_worker_minutes": round(available_minutes),
                "expected_worker_minutes_per_order": round(minutes_per_order, 1),
                "mean_order_quantity": round(mean_qty, 1), "mean_product_smv": round(mean_product_smv, 3),
                "mean_operator_efficiency": round(mean_efficiency, 2),
                "orders": TOTAL_ORDERS, "real_start_dates_in_window": len(window_starts),
            }
            print(f"DATA v2 load calibration: {load_calibration}")

        for i in range(1, TOTAL_ORDERS + 1):
            # Real start date (re-anchored grid.xls StartDate) -- replaces
            # the previous ~25-30-day invented spread with the real
            # ~399-day, front-loaded distribution. This is the direct fix
            # for PROJECT_STATUS.txt Section 1.2.2.
            start_date = random.choice(real_start_dates)

            # order_date (when the CUSTOMER placed the PO, distinct from
            # when production starts): no real source distinguishes this
            # from start_date in any of the four files. STAYS SYNTHETIC,
            # documented rather than left unexplained -- bucket (c) -- but
            # now offset from a real start_date instead of a synthetic one.
            order_d = start_date - timedelta(days=random.randint(5, 20))

            # Real quantity (per-PO total PlanTarget, grid.xls) -- replaces
            # the invented 1,500-12,000 range with the real ~52-3,300
            # range. Falls back to the invented range only if the real
            # list is somehow empty.
            target_qty = int(random.choice(real_po_quantities)) if real_po_quantities else random.randint(1500, 12000)

            chosen_product = random.choice(product_ids)

            # Real end-date gap (EndDate - StartDate, grid.xls) -- replaces
            # the invented random 5-15 day range.
            end_gap = random.choice(real_end_gap_days) if real_end_gap_days else random.randint(5, 15)
            end_date = start_date + timedelta(days=end_gap)

            # Real deadline lead time (ExFtyDate - StartDate, grid.xls) --
            # replaces the invented random 30-60-days-after-base_date range
            # (which ignored the order's own start date entirely).
            deadline_lead = random.choice(real_deadline_lead_days) if real_deadline_lead_days else random.randint(30, 60)
            deadline_d = start_date + timedelta(days=deadline_lead)

            prod_order_details[i] = {'product_id': chosen_product, 'target_qty': target_qty, 'start_date': start_date}
            order_meta_cache[i] = {
                'client_name': fake.company(),
                'po_number': f"PO-JOR-NC-{100000 + i}",
                'order_date': order_d,
                'delivery_deadline': deadline_d,
                'target_quantity': target_qty,
                'scheduled_start_date': start_date,
                'scheduled_end_date': end_date,
                'product_id': chosen_product
            }

            for bom_item in bom_map[chosen_product]:
                consumed_qty = bom_item['req_qty'] * target_qty * 1.05
                matching_stocks = [s_id for s_id, data in inventory_tracker.items() if data['mat_id'] == bom_item['material_id']]
                if matching_stocks:
                    best_stock_id = max(matching_stocks, key=lambda s_id: inventory_tracker[s_id]['qty'])
                    inventory_tracker[best_stock_id]['qty'] = max(0.0, inventory_tracker[best_stock_id]['qty'] - consumed_qty)

        updated_inventory = [(round(data['qty'], 4), s_id) for s_id, data in inventory_tracker.items()]
        cursor.executemany("UPDATE Inventory_Stock SET current_quantity = %s WHERE stock_id = %s;", updated_inventory)

        # actual_start_time / actual_end_time: synthetic perturbed timing,
        # distinct from planned_start_time/planned_end_time
        # (the committed schedule floor everything else in this codebase reads
        # -- decision_engine, impact_assessor's machine-gap search, GAScheduler
        # -- and which deliberately keeps its existing meaning unchanged here).
        # Added so the GNN delay label (graph/labels.py) can be computed from
        # genuine plan-vs-actual lateness instead of the execution_status
        # column, which only reflects a snapshot-time-gated causal-delay flag
        # -- see PROJECT_STATUS.txt Section 3 / 0D. Guarded so re-running
        # against an already-migrated database is safe (schema lives outside
        # this script, same pattern as employee_batches.rotation_offset above).
        # machine_status_at_dispatch / employee_on_leave_at_dispatch /
        # queue_delay_mins_at_dispatch: causal delay-risk factors captured at
        # the moment each lot was actually dispatched -- i.e. BEFORE that
        # lot's own execution-variance/breakdown roll, not after. This is
        # what makes them safe GNN input features rather than a third leak:
        # each is causally upstream of the lot's own outcome, not derived
        # from it. All three are values the simulation loop below already
        # computes locally (machine_state[asset_id] right after the repair
        # check but before this lot's own breakdown roll; on_leave;
        # queue_delay_mins) -- this just persists them instead of discarding
        # them once execution_status is decided. See PROJECT_STATUS.txt
        # Section 0E and graph/node_features.py for how these feed the GNN.
        #
        # machine_breakdown_count_at_dispatch -- ADDED after measuring that
        # machine_status_at_dispatch reads 'Breakdown' for a scheduled
        # machine NEVER: machine_free_time already accounts for repair
        # completion before a machine re-enters the assignment pool, so the
        # repair-check-and-reset-to-Active always fires before any dispatch
        # can observe it mid-repair. A machine's PERSISTENT breakdown-count
        # history (how many times it has failed so far, prior to and NOT
        # including whatever may happen on this exact dispatch) is a real,
        # non-constant reliability signal that instantaneous status can never
        # provide here. See PROJECT_STATUS.txt Section 0E/reversal 8.
        #
        # queue_delay_mins_at_dispatch's COMPUTATION also changed (not its
        # column) -- see the capture site below for why the original
        # `start_t - earliest_possible` was identically zero.
        for col, ddl in (
            ('lot_index', 'ALTER TABLE Job_Shop_Schedules ADD COLUMN lot_index INT NULL'),
            ('lot_quantity', 'ALTER TABLE Job_Shop_Schedules ADD COLUMN lot_quantity INT NULL'),
            ('planned_work_segments', 'ALTER TABLE Job_Shop_Schedules ADD COLUMN planned_work_segments LONGTEXT NULL'),
            ('actual_work_segments', 'ALTER TABLE Job_Shop_Schedules ADD COLUMN actual_work_segments LONGTEXT NULL'),
            ('actual_start_time', "ALTER TABLE Job_Shop_Schedules ADD COLUMN actual_start_time DATETIME NULL"),
            ('actual_end_time', "ALTER TABLE Job_Shop_Schedules ADD COLUMN actual_end_time DATETIME NULL"),
            ('machine_status_at_dispatch',
             "ALTER TABLE Job_Shop_Schedules ADD COLUMN machine_status_at_dispatch VARCHAR(50) NULL"),
            ('employee_on_leave_at_dispatch',
             "ALTER TABLE Job_Shop_Schedules ADD COLUMN employee_on_leave_at_dispatch TINYINT(1) NULL"),
            ('queue_delay_mins_at_dispatch',
             "ALTER TABLE Job_Shop_Schedules ADD COLUMN queue_delay_mins_at_dispatch FLOAT NULL"),
            ('machine_breakdown_count_at_dispatch',
             "ALTER TABLE Job_Shop_Schedules ADD COLUMN machine_breakdown_count_at_dispatch INT NULL"),
        ):
            cursor.execute("""
                SELECT COUNT(*) FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'Job_Shop_Schedules' AND COLUMN_NAME = %s
            """, (col,))
            if cursor.fetchone()[0] == 0:
                print(f"Adding Job_Shop_Schedules.{col} column...")
                cursor.execute(ddl)

        # 8. Seed Constrained Job Shop Schedules
        print("Streaming independent resource-constrained planned and synthetic actual calendars...")
        schedule_rows = []
        wip_rows = []
        quality_rows = []

        base_time = reference_time - timedelta(days=15)
        now_time = reference_time

        # 8a. Build and freeze the COMPLETE planned schedule. This phase has
        # no actual clocks, breakdown state, execution RNG, or outcome data in
        # scope. That exclusion is the DGP contract, not an optimization.
        TARGET_LOT_SIZE = 20
        MAX_LOTS_PER_ORDER = 60
        prod_order_ids = list(range(1, TOTAL_ORDERS + 1))
        order_deadlines = {
            po_id: datetime.combine(
                order_meta_cache[po_id]["delivery_deadline"], time(23, 59, 59)
            )
            for po_id in prod_order_ids
        }
        # v2 streams phase A into phase B (identical decisions and order); v1
        # keeps the materialized list it was validated with.
        planner = iter_frozen_planned_schedule if DATA_V2 else build_frozen_planned_schedule
        streamed = {"ownership": {}, "stats": {"batch": 0, "factory": 0, "global": 0, "off_shift": 0}} if DATA_V2 else {}
        planned = (
            planner(
                **streamed,
                order_ids=prod_order_ids,
                prod_order_details=prod_order_details,
                product_routing_map=product_routing_map,
                base_date=base_date,
                base_time=base_time,
                factory_capacity=factory_capacity,
                capable_machines_by_type=capable_machines_by_type,
                asset_to_line_map=asset_to_line_map,
                line_to_factory_map=line_to_factory_map,
                qualified_operators_by_op_factory=qualified_operators_by_op_factory,
                employee_leave_lookup=employee_leave_lookup,
                employee_batch_lookup=employee_batch_lookup,
                line_to_batches=line_to_batches,
                batch_offsets=batch_offsets,
                employee_shift_lookup=employee_shift_lookup,
                employee_skill_lookup=employee_skill_lookup,
                order_deadlines=order_deadlines,
                duration_calibration_factor=DURATION_CALIBRATION_FACTOR,
                planning_rng=random,
                target_lot_size=TARGET_LOT_SIZE,
                max_lots_per_order=MAX_LOTS_PER_ORDER,
            )
        )
        if DATA_V2:
            frozen_plan = planned
            order_factory_ownership, plan_assignment_stats = streamed["ownership"], streamed["stats"]
        else:
            frozen_plan, order_factory_ownership, plan_assignment_stats = planned

        # 8b. Simulate actual execution only after every phase-A assignment
        # and planned timestamp is frozen. The independent actual seed may
        # change outcomes, but it cannot alter any planned field.
        machine_state = {
            asset_id: asset_status_lookup.get(asset_id, "Active")
            for asset_id in range(1, 3001)
        }
        order_schedule_summary = {
            po_id: {"statuses": [], "max_end_time": None}
            for po_id in prod_order_ids
        }
        wip_count = 0
        quality_rng = random.Random(actual_seed ^ 0x514C)
        for outcome in simulate_actual_from_frozen_plan(
            frozen_plan,
            base_time=base_time,
            now_time=now_time,
            actual_seed=actual_seed,
            asset_status_lookup=asset_status_lookup,
            regime=regime_profile,
        ):
            plan = outcome.plan
            schedule_rows.append((
                plan.production_order_id,
                plan.line_id,
                plan.asset_id,
                plan.operation_id,
                plan.employee_id,
                plan.batch_id,
                plan.planned_start_time,
                plan.planned_end_time,
                outcome.execution_status,
                outcome.actual_start_time,
                outcome.actual_end_time,
                outcome.machine_status_at_dispatch,
                outcome.employee_on_leave_at_dispatch,
                outcome.queue_delay_mins_at_dispatch,
                outcome.machine_breakdown_count_at_dispatch,
                plan.lot_index,
                plan.lot_quantity,
                json.dumps(plan.planned_work_segments, default=str),
                json.dumps(outcome.actual_work_segments, default=str),
            ))

            summary = order_schedule_summary[plan.production_order_id]
            summary["statuses"].append(outcome.execution_status)
            if (
                summary["max_end_time"] is None
                or outcome.actual_end_time > summary["max_end_time"]
            ):
                summary["max_end_time"] = outcome.actual_end_time

            if outcome.execution_status == "Done":
                wip_rows.append((
                    plan.line_id,
                    plan.production_order_id,
                    plan.operation_id,
                    plan.lot_quantity,
                ))
                inspected = min(
                    plan.lot_quantity, quality_rng.randint(50, 150)
                )
                skill_factor = max(
                    0.0, (100.0 - plan.efficiency_rating) / 100.0
                )
                base_defect_rate = 0.01 + skill_factor * 0.12
                defects = sum(
                    1
                    for _ in range(max(0, inspected))
                    if quality_rng.random() < base_defect_rate
                )
                defect_type = quality_rng.choice([
                    "Stitch skipping",
                    "Thread tension error",
                    "Fabric puckering",
                    "Skipped bartack",
                ])
                quality_rows.append((
                    plan.production_order_id,
                    plan.line_id,
                    plan.operation_id,
                    inspected,
                    defects,
                    defect_type,
                ))
                wip_count += 1

            if len(schedule_rows) >= 10000:
                cursor.executemany(
                    "INSERT INTO Job_Shop_Schedules (production_order_id, line_id, asset_id, operation_id, employee_id, batch_id, planned_start_time, planned_end_time, execution_status, actual_start_time, actual_end_time, machine_status_at_dispatch, employee_on_leave_at_dispatch, queue_delay_mins_at_dispatch, machine_breakdown_count_at_dispatch, lot_index, lot_quantity, planned_work_segments, actual_work_segments) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    schedule_rows,
                )
                schedule_rows = []

            if len(wip_rows) >= 10000:
                cursor.executemany(
                    "INSERT INTO WIP_Tracking (line_id, production_order_id, operation_id, pieces_completed) VALUES (%s, %s, %s, %s);",
                    wip_rows,
                )
                cursor.executemany(
                    "INSERT INTO Quality_Logs (production_order_id, line_id, operation_id, inspected_quantity, defective_quantity, defect_type_details) VALUES (%s, %s, %s, %s, %s, %s);",
                    quality_rows,
                )
                wip_rows = []
                quality_rows = []

        if schedule_rows:
            cursor.executemany("INSERT INTO Job_Shop_Schedules (production_order_id, line_id, asset_id, operation_id, employee_id, batch_id, planned_start_time, planned_end_time, execution_status, actual_start_time, actual_end_time, machine_status_at_dispatch, employee_on_leave_at_dispatch, queue_delay_mins_at_dispatch, machine_breakdown_count_at_dispatch, lot_index, lot_quantity, planned_work_segments, actual_work_segments) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", schedule_rows)
        if wip_rows:
            cursor.executemany("INSERT INTO WIP_Tracking (line_id, production_order_id, operation_id, pieces_completed) VALUES (%s, %s, %s, %s);", wip_rows)
            cursor.executemany("INSERT INTO Quality_Logs (production_order_id, line_id, operation_id, inspected_quantity, defective_quantity, defect_type_details) VALUES (%s, %s, %s, %s, %s, %s);", quality_rows)
        if status_logs_batch:
            cursor.executemany("INSERT INTO Machine_Status_Logs (log_id, asset_id, status_changed_to, reason) VALUES (%s, %s, %s, %s)", status_logs_batch)

        print(
            "Planned employee assignment source breakdown: "
            f"batch member: {plan_assignment_stats['batch']}, "
            f"same-factory fallback: {plan_assignment_stats['factory']}, "
            f"global fallback: {plan_assignment_stats['global']}"
        )
        print(
            "Planned shift coverage - lots assigned off shift: "
            f"{plan_assignment_stats['off_shift']}"
        )

        # Sync final machine statuses to asset table
        print("Synchronizing final machine states...")
        updated_statuses = [(status, a_id) for a_id, status in machine_state.items()]
        cursor.executemany("UPDATE Machinery_Assets SET current_status = %s WHERE asset_id = %s", updated_statuses)

        # 9. Derive and Synchronize Production, Purchase, and Delivery Statuses
        #
        # DELIBERATE SYNTHETIC CHOICE (Delivery_Logistics, populated into
        # logistics_batch below): checked and confirmed zero downstream
        # consumers. Purely post-production shipping (carrier, tracking
        # number, ship/arrival dates) -- doesn't touch scheduling, and this
        # project already measures on-time performance directly against
        # the customer deadline (Purchase_Orders.delivery_deadline), not a
        # separate shipping-lag concept. None of the four real files
        # contain logistics/carrier data either. Kept invented: carrier
        # fixed to "Aramex Logistics", tracking number fabricated, ship/
        # arrival dates derived from each order's own simulated completion.
        print("Synchronizing hierarchical order statuses and delivery logistics...")
        po_batch = []
        prod_order_batch = []
        logistics_batch = []

        for po_id in prod_order_ids:
            meta = order_meta_cache[po_id]
            summary = order_schedule_summary[po_id]
            statuses = summary['statuses']
            max_end = summary['max_end_time']
            deadline = datetime.combine(meta['delivery_deadline'], time(23, 59, 59))
            
            fail_count = sum(1 for s in statuses if s == 'Failed')
            done_count = sum(1 for s in statuses if s == 'Done')
            delayed_count = sum(1 for s in statuses if s == 'Delayed')
            total_ops = len(statuses)
            
            fail_frac = fail_count / total_ops
            done_frac = done_count / total_ops
            delayed_frac = delayed_count / total_ops

            # CORRECTED ORDER STATUS THRESHOLDS
            if fail_frac > 0.15:
                prod_status = 'Cancelled'
            elif delayed_frac > 0.15:
                prod_status = 'Delayed'
            elif done_count == total_ops and total_ops > 0:
                prod_status = 'Completed'
            elif any(s == 'Running' for s in statuses) or done_frac > 0:
                prod_status = 'Processing'
            elif all(s == 'Scheduled' for s in statuses):
                prod_status = 'Pending'
            else:
                prod_status = 'Processing'

            if prod_status == 'Completed':
                po_status = 'Shipped' if max_end + timedelta(days=1) <= now_time else 'In_Production'
            elif prod_status == 'Processing':
                po_status = 'In_Production'
            elif prod_status == 'Delayed':
                po_status = 'Delayed'
            elif prod_status == 'Cancelled':
                po_status = 'Cancelled'
            else:
                po_status = 'Confirmed'

            if po_status == 'Delivered':
                delivery_status = 'Delivered'
                act_ship = max_end + timedelta(days=1)
                est_arr = act_ship + timedelta(days=random.randint(3, 10))
            elif po_status == 'Shipped':
                delivery_status = 'In_Transit'
                act_ship = max_end + timedelta(days=1)
                est_arr = act_ship + timedelta(days=random.randint(3, 10))
            elif po_status == 'In_Production' or po_status == 'Delayed':
                delivery_status = 'Packing'
                act_ship = None
                est_arr = meta['delivery_deadline']
            elif po_status == 'Cancelled':
                delivery_status = 'Cancelled'
                act_ship = None
                est_arr = meta['delivery_deadline']
            else:
                delivery_status = 'Awaiting_Dispatch'
                act_ship = None
                est_arr = meta['delivery_deadline']

            po_batch.append((po_id, meta['client_name'], meta['po_number'], meta['order_date'], meta['delivery_deadline'], po_status))
            prod_order_batch.append((po_id, po_id, meta['product_id'], meta['target_quantity'], meta['scheduled_start_date'], meta['scheduled_end_date'], prod_status))
            logistics_batch.append((po_id, po_id, "Aramex Logistics", f"SYNTH-TRK-{po_id:08d}", act_ship, est_arr, delivery_status))

        cursor.executemany("INSERT INTO Purchase_Orders (po_id, client_name, po_number, order_date, delivery_deadline, po_status) VALUES (%s, %s, %s, %s, %s, %s);", po_batch)
        cursor.executemany("INSERT INTO Production_Orders (production_order_id, po_id, product_id, target_quantity, scheduled_start_date, scheduled_end_date, status) VALUES (%s, %s, %s, %s, %s, %s, %s);", prod_order_batch)
        cursor.executemany("INSERT INTO Delivery_Logistics (delivery_id, po_id, carrier_name, tracking_number, actual_shipment_date, estimated_arrival_date, delivery_status) VALUES (%s, %s, %s, %s, %s, %s, %s)", logistics_batch)

        # PERSIST WHAT THIS RUN DERIVED, so runtime consumers read the values
        # that describe THIS database instead of static constants measured
        # against an older one (see utils/derived_constants.py for the why).
        #
        # DURATION_CALIBRATION_FACTOR: previously recomputed here on every
        # run while ga/scheduler.py read a frozen 0.8255 from config, so a
        # reseed could silently leave the scheduler planning against a
        # different calibration than the data it was planning over.
        #
        # SLACK ANCHORS: the fuzzy triage's membership functions
        # (agent/fuzzy_logic.py) were fixed day-counts that had no
        # relationship to this order book's actual slack spread. Derived here
        # as real quantiles of the seeded deadlines so the three membership
        # bands actually partition the distribution.
        _slack_days = sorted(
            (order_meta_cache[po_id]['delivery_deadline'] - base_date).days
            for po_id in prod_order_ids
            if order_meta_cache.get(po_id, {}).get('delivery_deadline') is not None
        )

        def _q(p):
            if not _slack_days:
                return None
            idx = min(len(_slack_days) - 1, max(0, int(round(p / 100.0 * (len(_slack_days) - 1)))))
            return float(_slack_days[idx])

        _derived = {
            "artifact_version": 1,
            "dataset_version": dataset_version,
            "source_hashes": source_hashes,
            "reference_time": reference_time.isoformat(),
            "generated_at": datetime.now().isoformat(),
            "source_database": db_config.get("database"),
            "planning_seed": int(planning_seed),
            "actual_execution_seed": int(actual_seed),
            "operating_regime": regime_profile.manifest(),
            "timeline_generation_contract": "frozen-plan-then-actual-v1",
            "duration_calibration_factor": round(float(DURATION_CALIBRATION_FACTOR), 6),
            "duration_calibration_provenance": (
                "real_median_min_per_unit * mean_real_efficiency / mean_sam_per_step, "
                "from PPC-1.xlsx (DATA_AUDIT.md Module 5)"
            ),
            "order_count": len(prod_order_ids),
            "data_version": SEED_DATA_VERSION,
            "load_calibration": load_calibration,
            "slack_days_quantiles": {
                "p25": _q(25), "p50": _q(50), "p75": _q(75),
            },
            "slack_anchor_provenance": (
                "quantiles of (delivery_deadline - seed base_date) across every seeded "
                "order; consumed by agent/fuzzy_logic.py"
            ),
        }
        # Database identity and calibration activate together when this dataset
        # is selected. Staging never writes a live runtime calibration file.
        cursor.execute("REPLACE INTO Dataset_Calibration (singleton_id,payload) VALUES (1,%s)",
                       (json.dumps(_derived),))
        # Persist factory ownership in the same transaction as the rows it
        # describes, so a dataset can never exist without its ownership map.
        if order_factory_ownership:
            _now = datetime.now()
            cursor.executemany(
                "REPLACE INTO Order_Factory_Ownership "
                "(production_order_id, factory_id, assigned_at) VALUES (%s,%s,%s)",
                [(oid, fid, _now) for oid, fid in sorted(order_factory_ownership.items())],
            )
            from collections import Counter as _Counter
            _dist = dict(sorted(_Counter(order_factory_ownership.values()).items()))
            print(f"Order->factory ownership persisted for "
                  f"{len(order_factory_ownership)} orders (capacity-weighted, "
                  f"synthetic policy): {_dist}")
        conn.commit()
        print(f"Committed dataset {dataset_version} and its calibration in {db_config['database']}")

        print("\nDatabase successfully seeded, committed, and structurally verified!")

    except Exception as e:
        if conn is not None:
            try:
                conn.rollback()
            except Exception as rollback_error:
                e.add_note(f"Rollback also failed: {rollback_error!r}")
        print("\nCritical Failure: seeding aborted. Earlier TRUNCATE/DDL changes "
              "cannot be rolled back; restore the backup before reuse.")
        raise
    finally:
        # Always attempt every cleanup action; retain the original failure.
        active_error = sys.exception()
        cleanup_errors = []
        actions = []
        if cursor is not None:
            actions.extend([
                lambda: cursor.execute("SET FOREIGN_KEY_CHECKS = 1;"),
                lambda: cursor.execute("SET UNIQUE_CHECKS = 1;"),
                cursor.close,
            ])
        if conn is not None:
            actions.append(conn.close)
        for action in actions:
            try:
                action()
            except Exception as cleanup_error:
                cleanup_errors.append(cleanup_error)
        if cleanup_errors:
            if active_error is not None:
                for error in cleanup_errors:
                    active_error.add_note(f"Cleanup also failed: {error!r}")
            else:
                raise RuntimeError("Seeder database cleanup failed") from cleanup_errors[0]

if __name__ == "__main__":
    execute_complete_seed()
