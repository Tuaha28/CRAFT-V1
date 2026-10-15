import random
from datetime import timedelta

# Fallbacks used when an op_details entry is missing a field. Defined once
# here rather than repeated as inline literals at each call site — the
# previous inline `details.get('setup_mins', 15.0)` pattern appeared in three
# files, so a change to the default would have had to be made in three places.
DEFAULT_SETUP_MINS = 15.0
DEFAULT_SERIAL_MINS = 30.0

# How much of an operation's duration is added as a risk cushion at a delay
# probability of 1.0. At delay_prob=0.5 an operation would get a 25% cushion.
#
# HISTORY: originally disabled (0.0) because a three-arm ablation showed the
# GNN contributed no per-operation discrimination under the OLD (pre-fix)
# delay label -- real predictions and two hardcoded constants set to the
# model's own near-constant mean produced schedules within 0.35% of each
# other, and broadly applying the cushion was measured as a flat ~19.5% tax
# with no risk-targeting benefit (see git history / PROJECT_STATUS.txt for
# the original arms).
#
# RE-ENABLED, GATED, 2026-08-24 -- go/no-go decision made after the delay
# label fix (PROJECT_STATUS.txt / DELAY_LABEL_FIX_PROPOSAL.md) landed and the
# split-encoder architecture was promoted to production. A fresh 3-arm
# ablation against that checkpoint (scratch/gated_buffer_ablation.py,
# scratch_gated_buffer_ablation.log) tested disabled vs. broadly-enabled vs.
# gated (cushion applied only to operations at/above RISK_BUFFER_GATE_THRESHOLD):
#
#   arm            on-time  makespan  throughput  mean-tard  labour  margin
#   A_disabled       60.3%     32.2d      32.0/d        68h    5.2%   +5.6%
#   B_broad_0.5      60.2%     33.2d      31.0/d        72h    5.3%   +5.0%
#   C_gated_0.5      60.2%     31.4d      32.8/d        70h    5.4%   +5.9%
#
# Broadly-enabled (B) is worse than disabled (A) on every metric except
# on-time (a wash) -- confirming the OLD diagnosis still holds: the model's
# real-but-modest signal (AP ~0.16, most predictions clustered in a narrow
# low-confidence band) is not discriminative enough to pad indiscriminately.
# Gated (C) is the one arm that beats disabled on makespan, throughput, AND
# margin, at a negligible on-time cost (-0.1pp, within single-seed noise) and
# a small labour/tardiness cost -- i.e. gating on confidence, not the buffer
# concept itself, is what was missing. Flagged at the time as a SINGLE-SEED
# result, not yet repeated across seeds -- direction consistent with the
# diagnosed mechanism, magnitudes provisional until re-confirmed.
#
# RE-DISABLED, 2026-09-01 -- that re-confirmation was run: 3 seeds (42, 101,
# 202) x 3 arms on today's corrected pipeline (post ORDER BY fix 08-27, post
# release-date-enforcement fix 08-24 -- the original single-seed run above
# predates BOTH and ran under an obsolete ~32-day-makespan regime that no
# longer exists). Whole-book AND per-window (scratch/gated_buffer_ablation_
# multiseed.py, outputs/gated_buffer_ablation_multiseed.json): on-time,
# makespan, throughput and margin all move LESS between arms than between
# seeds of the same arm -- no reliable winner anywhere. The one seed-
# consistent signal is mean tardiness, and it favours DISABLED (lowest in
# all 3 seeds) -- the opposite direction from the 2026-08-24 rationale above.
# Per-window dense-window (w1-2) ranges fully overlap across all three arms
# too, so the crisp whole-book ordering that motivated gating turns out to
# have been a single-seed artefact of a near-invariant slack tail, not a
# real per-window effect. See SESSION_HANDOFF.md Thread 5 for the full
# numbers. RISK_BUFFER_GATE_THRESHOLD is left in place below (harmless at
# 0.0 -- the gate check still runs, it just never has anything to scale) so
# re-enabling later is a one-line change with the calibration already done.
RISK_BUFFER_AT_CERTAINTY = 0.0

# Only operations whose predicted delay probability is at or above this are
# cushioned at all; everyone else gets zero cushion regardless of
# RISK_BUFFER_AT_CERTAINTY. 0.14 is the midpoint of the ~0.13-0.15 band
# identified in scratch/delay_calibration_and_confidence_gate.py as roughly
# the top 10-20% most confident delay predictions for the current production
# (split-encoder / Option B) checkpoint -- gates in ~14.7% of operations on
# that checkpoint. This threshold is a property of THIS checkpoint's
# predicted-probability distribution, not a universal constant: re-run that
# script's confidence-gate analysis after any delay-head retrain and update
# this value if the distribution has shifted.
RISK_BUFFER_GATE_THRESHOLD = 0.14

# Fraction of a bottleneck (protected) machine type's per-line pool a single
# operation is allowed to claim. 1.0 = no cap, the long-standing default
# behaviour ("take the whole bounded pool" -- ga/heuristics.py::
# build_chromosome's mc_strategy='max', and the GA's own MC gene, which is
# always pushed toward its ceiling because more machines only ever helps
# fitness). Read live (module-qualified, not imported by value) from
# ga/chromosome.py::ChromosomeFactory._max_machines_for and
# ga/operators.py::repair_line_and_machine_count -- the two places an
# operation's MC gene gets bounded -- so a test can monkeypatch this one
# attribute the same way buffer_reenable_test.py monkeypatched
# RISK_BUFFER_AT_CERTAINTY above.
#
# WHY THIS EXISTS: PROJECT_STATUS.txt Section 0H.8 tried softening MACHINE
# SELECTION for the window's most-congested type (Tubular Sleeve Join) and
# measured zero effect -- instrumentation showed the selection function
# never had slack to redistribute, because MC already saturates the entire
# per-line pool on every call (0 of 1,200 protected calls had len(pool) >
# take). This knob creates that slack directly, by capping what a single
# operation may claim, at the cost of that operation itself running slower
# (calculate_operation_work divides work by machine count, so fewer
# machines means more minutes). See PROJECT_STATUS.txt Section 0H.9 for the
# measured result of enabling this.
MC_PROTECTION_FRACTION = 1.0


def calculate_operation_work(op_detail: dict, machine_count) -> float:
    """
    THE duration formula. One definition, called everywhere.

    duration = setup + (total single-machine work content / machines used)

    op_detail: {'setup_mins': float, 'serial_processing_mins': float} as built
        in ga/scheduler.py. serial_processing_mins is the operation's TOTAL
        work expressed as minutes on one machine (sam * remaining quantity /
        line efficiency); it is NOT per-lot and must not be pre-divided by any
        parallelism factor.

    WHY THIS EXISTS: this expression previously lived, spelled out inline, in
    ga/fitness.py and twice in agent/decision_engine.py. That is exactly the
    shape of the bug that cost the most in this project: the quantity was
    being divided by the lot count in one place and by the machine count in
    another, double-counting the same parallelism and making every duration
    roughly 60x too fast. Two places computing the same quantity will drift;
    the only reliable fix is for there to be one place.

    Setup is charged ONCE per operation, not per machine — it represents
    preparing the operation, and per-machine re-rigging is handled separately
    as changeover in select_machines_and_start().
    """
    setup, productive = split_operation_work(op_detail, machine_count)
    return setup + productive


def split_operation_work(op_detail, machine_count):
    """
    (setup_elapsed_mins, productive_work_mins) -- the two halves of a duration,
    which the shift-aware evaluator must treat differently.

    PRODUCTIVE work is what utils/work_calendar walks: it is done by a worker
    and therefore pauses when that worker is off duty.

    SETUP is ELAPSED MACHINE OCCUPANCY and stays outside the calendar. The
    reason is evidential, not stylistic: the SIMULATOR MODELS NO SETUP AT ALL.
    data_generation/seed_database_fixed.py computes
        duration = DURATION_CALIBRATION_FACTOR * qty * sam / (eff/100)
    and hands exactly that to work_on_calendar -- there is no setup or
    changeover term anywhere in it. So there is no simulator semantics for
    setup to align to; the scheduler invented the cost, and being
    machine-attached rather than worker-productive it belongs in occupancy.

    CONTRACT MISMATCH, RECORDED NOT FIXED: the scheduler therefore charges a
    setup cost (DEFAULT_SETUP_TIME_MINS per operation) and a changeover cost
    (ga/fitness.CHANGEOVER_MINUTES per machine on an order switch) that the
    TRAINING DATA DOES NOT CONTAIN. Every planned span the GNN's completion
    head learned from was generated without either. That is a real divergence
    between the scheduler and the data used to train the model it consumes;
    it is noted here deliberately rather than papered over, and it is not
    addressed by the shift-alignment work.

    Kept as ONE definition alongside calculate_operation_work -- which is now
    just their sum -- because two places computing this split would drift, and
    that is the exact failure this module's docstring already records once.
    """
    setup = float(op_detail.get('setup_mins', DEFAULT_SETUP_MINS))
    serial = float(op_detail.get('serial_processing_mins', DEFAULT_SERIAL_MINS))
    machines = max(1, int(machine_count))
    return setup, serial / machines


def apply_risk_buffer(duration_mins: float, delay_prob: float) -> float:
    """
    Pads a duration by the GNN's predicted delay risk, so a riskier operation
    books more of the machine's time and downstream work is not planned
    against an optimistic finish. GATED: operations below
    RISK_BUFFER_GATE_THRESHOLD get no cushion at all (see that constant's
    comment for why -- broadly-applied cushioning was measured worse than
    disabled, gated cushioning was not).

    Shared for the same reason as calculate_operation_work: this expression
    was written out at three separate call sites, and a schedule scored with
    one cushion but exported with another would silently be a different
    schedule.

    delay_prob is clamped defensively: it should already be a real sigmoid
    probability, but a raw logit leaking in here would otherwise silently
    inflate every duration.
    """
    p = min(max(float(delay_prob), 0.0), 1.0)
    if p < RISK_BUFFER_GATE_THRESHOLD:
        return duration_mins
    return duration_mins * (1.0 + (p * RISK_BUFFER_AT_CERTAINTY))

def fuzzy_urgency_priority(urgency_score: float) -> float:
    """
    FUZZY LOGIC ENGINE: Evaluates deadline urgency exclusively when a manager 
    explicitly flags an order for prioritization from the frontend UI.
    
    Zero loyalty is measured, modeled, or guessed.
    
    Args:
        urgency_score: 0 to 10 (10 = deadline is extremely close / critical)
        
    Returns:
        Priority Multiplier (1.5x up to 3.0x boost)
    """
    # 1. FUZZIFICATION (Triangular Membership for Urgency)
    urgency_low = max(0.0, min(1.0, (5.0 - urgency_score) / 5.0))
    urgency_high = max(0.0, min(1.0, (urgency_score - 5.0) / 5.0))
    
    # 2. RULE EVALUATION
    # Rule 1: IF Urgency is High THEN Priority Boost is Strong (3.0)
    # Rule 2: IF Urgency is Low THEN Priority Boost is Moderate (1.5)
    
    # 3. DEFUZZIFICATION (Weighted Average)
    numerator = (urgency_high * 3.0) + (urgency_low * 1.5)
    denominator = urgency_high + urgency_low
    
    if denominator == 0:
        return 2.0
        
    return numerator / denominator

def find_slot_and_insert(intervals, ready_time, duration):
    sorted_intervals = sorted(intervals)
    cursor = ready_time
    result = []
    chosen_start = None

    for (s, e) in sorted_intervals:
        if chosen_start is None:
            if e <= cursor:
                result.append((s, e))
                continue
            if s > cursor and (s - cursor) >= duration:
                chosen_start = cursor
                result.append((chosen_start, chosen_start + duration))
                result.append((s, e))
                continue
            cursor = max(cursor, e)
            result.append((s, e))
        else:
            result.append((s, e))

    if chosen_start is None:
        chosen_start = cursor
        result.append((chosen_start, chosen_start + duration))

    return chosen_start, result

def select_machines_and_start(pool_ids, machine_free, machine_last_order,
                               order_id, machine_count, ready_floor, changeover_mins,
                               is_protected_type=False, top_k=3, rng=random):
    """
    Machine-level placement: pick which physical machines run an operation and
    when it can start.

    This is the single source of truth for that decision. ga/fitness.py uses it
    to score a chromosome and agent/decision_engine.py uses it to decode the
    winning chromosome into the exported schedule; if the two computed start
    times differently, the exported plan would not be the plan that was scored
    — a defect this codebase has already had once, via the post-hoc high-risk
    reroute.

    Replaces the previous model in which a production LINE was one serial
    resource. That treated a line as capable of exactly one operation at a
    time while its other ~29 machines idled, capping the whole factory at
    roughly 350 concurrently busy machines out of 3,000, and it made the MC
    gene almost free: adding machines shortened an operation at no cost,
    because the line was blocked either way. With per-machine tracking, MC
    genuinely competes for a finite shared pool.

    Selection: the `machine_count` least-loaded machines in the pool. The
    operation cannot start until the last of them is free, so the start time
    is the largest free-time among those chosen, floored by `ready_floor`
    (the order's own precedence constraint).

    CHANGEOVER is charged per MACHINE, not per line as before. A sewing
    machine switching to a different production order needs re-rigging, and
    with several orders now running concurrently on one line, "the line's
    previous order" is no longer well defined. Per-machine is both better
    defined and closer to what actually happens on the floor.

    NOTE: this is list scheduling — a machine takes its next job when it comes
    free, and there is no back-filling of an earlier idle gap. The previous
    line-level implementation did gap-fill, but gap-filling across a set of
    `machine_count` machines that must be simultaneously free is a materially
    harder problem, and picking the least-loaded machines already spreads work
    the way gap-filling was there to achieve.

    Args:
        pool_ids: usable machine IDs of the right type on the assigned line
            (already excludes the standby reserve).
        machine_free: {asset_id: minutes-from-base when it next comes free}.
            Mutated by the caller, not here.
        machine_last_order: {asset_id: production_order_id of its last job}.
        machine_count: the MC gene, already bounded by the caller.
        ready_floor: earliest start permitted by the order's own precedence.
        changeover_mins: re-rigging penalty when a machine switches order.
        is_protected_type: True if this operation's machine type is the
            CURRENT window's single most-congested type (see
            ga/fitness.py's protected_machine_type / PROJECT_STATUS.txt
            Section 0H.8). When True, selection softens from strict
            least-loaded-first to a randomised top_k pick per slot -- the
            exact pattern ga/operators.py::choose_line_work_aware already
            uses for LINE choice, applied here to MACHINE choice instead.
            Every other (unprotected) type keeps the original strict
            greedy behaviour untouched.
        top_k: band width for the randomised pick when is_protected_type
            (ignored otherwise). Matches choose_line_work_aware's default.
        rng: injectable for reproducible tests; defaults to the random
            module (module-level functions, same convention as
            ga/heuristics.py's rng=random default).

    Returns (start_time, chosen_ids). chosen_ids may be shorter than
    machine_count if the pool is smaller; callers should treat that as the
    real capacity and may want to count it.
    """
    if not pool_ids:
        return ready_floor, []

    # Effective availability includes the changeover this specific machine
    # would incur, so a machine already running this order is preferred over
    # a nominally-earlier machine that would need re-rigging.
    def effective_free(asset_id):
        free = machine_free.get(asset_id, 0.0)
        if changeover_mins and machine_last_order.get(asset_id, order_id) != order_id:
            free += changeover_mins
        return free

    take = max(1, min(int(machine_count), len(pool_ids)))
    ranked = sorted(pool_ids, key=effective_free)

    if is_protected_type and len(ranked) > take:
        # Section 0H.8: strict least-loaded-first, applied to every operation
        # needing this window's bottleneck type, concentrates them onto the
        # SAME handful of soonest-free machines every time -- deterministic
        # by construction. Softened the same way LINE choice already is:
        # pick each of the `take` slots from a randomised top_k band of
        # whatever's still least-loaded, rather than always taking the
        # single best. Measured before promoting to the full GA pipeline --
        # see PROJECT_STATUS.txt Section 0H.8 for the validation result.
        chosen = []
        remaining = list(ranked)
        for _ in range(take):
            band = remaining[:min(top_k, len(remaining))]
            pick = rng.choice(band)
            chosen.append(pick)
            remaining.remove(pick)
    else:
        chosen = ranked[:take]

    start_time = max([ready_floor] + [effective_free(a) for a in chosen])
    return start_time, chosen


def snap_to_shift_window(dt, shift_start_hour, shift_end_hour):
    """
    Pushes a start time forward into the next working window, if the factory
    has one.

    UNDER 24-HOUR OPERATION THIS IS A NO-OP. The factory runs three back-to-back
    8-hour shifts (06:00-14:00, 14:00-22:00, 22:00-06:00) that tile the whole
    day, so there is no hour at which a machine is unavailable and nothing to
    snap to. Detected here rather than removed from the call sites, so the
    behaviour follows the configured hours instead of being hardcoded either
    way — set SHIFT_START_HOUR/SHIFT_END_HOUR to a genuine window in
    utils/config.py and the original snapping returns.

    Why this mattered: the previous 06:00-22:00 window pushed any operation
    starting outside it to the next morning. Operations here routinely run for
    days, so an 8-hour dead zone per day inflated elapsed spans by roughly a
    third — and it was inconsistent with the machines, which the capacity model
    treats as continuously available.

    Note this only ever moved the START time; an operation that began at 21:00
    still ran straight through the night, so the old behaviour was not even a
    self-consistent 16-hour model.
    """
    covers_whole_day = (
        shift_start_hour == shift_end_hour
        or (shift_start_hour <= 0 and shift_end_hour >= 24)
    )
    if covers_whole_day:
        return dt
    if shift_start_hour <= dt.hour < shift_end_hour:
        return dt
    if dt.hour < shift_start_hour:
        return dt.replace(hour=shift_start_hour, minute=0, second=0, microsecond=0)
    next_day = dt + timedelta(days=1)
    return next_day.replace(hour=shift_start_hour, minute=0, second=0, microsecond=0)