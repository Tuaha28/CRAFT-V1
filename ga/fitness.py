import sys
import os
import random
import statistics
import hashlib
import json
from datetime import timedelta
import numpy as np

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.logger import get_logger
from utils.config import IDLE_LABOR_WEIGHT
from ga.infeasibility import missing_machine_pool
from ga.schedule_utils import (
    find_slot_and_insert,
    select_machines_and_start,
    calculate_operation_work,
    split_operation_work,
    apply_risk_buffer,
)
from utils.work_calendar import (
    elapsed_completion_minutes, completion_after_productive_minutes,
)
from ga.worker_allocation import (
    allocate_crew, normalize_reservations, reservation_busy_minutes,
)

logger = get_logger("ga_fitness", "ga.log")

# --- Tunable constants -----------------------------------------------------
# alpha/gamma/delta: original weights (makespan / delay penalty / priority reward)
# EPSILON: weight on the line-load-balance penalty.
# ZETA: weight (minutes-late multiplier) on the deadline-lateness penalty.
#   Set higher than GAMMA/EPSILON since missing a customer ship date is
#   normally the single most business-critical outcome in garment
#   scheduling — deliberately made the dominant term.
#
# SCALE NOTE: these weights only mean what they look like they mean if the
# terms they multiply are on comparable scales. Previously they weren't —
# total_lateness_penalty was a raw SUM across every late order (with 1,000+
# orders each potentially thousands of minutes late, this alone reached the
# TENS OF MILLIONS), while total_delay_penalty/total_priority_reward were
# capped in the low hundreds-of-thousands and base_makespan was a single
# number in the low thousands. ZETA=3.0 looked like "lateness matters 3x as
# much as makespan" but in practice lateness outweighed everything else by
# 2-3 orders of magnitude regardless of the weights — the GA was
# functionally optimizing lateness alone. Fixed below by (a) converting
# sum-across-many-items terms (delay penalty, priority reward, lateness) to
# PER-ITEM AVERAGES so their magnitude doesn't scale with how many
# operations/orders exist, and (b) expressing every time-based term in
# HOURS instead of raw minutes, so they land in a broadly comparable range
# instead of minutes vs. a 0-100 penalty scale. Not a full statistical
# normalization (that would need per-run reference/ideal-point scaling,
# arguably overkill for a single weighted-sum GA) — but the weights now
# actually reflect relative importance instead of being overridden by unit
# mismatches.
ALPHA, GAMMA, DELTA, EPSILON, ZETA = 1.0, 1.0, 1.0, 1.0, 3.0

# Weight on the on-time-delivery-rate term (see the lateness block in
# evaluate_schedule). Expressed on the same hours scale as the other terms:
# an order book where EVERY order misses its date costs the equivalent of
# this many hours of makespan. 24.0 = "a fully-late book is as bad as adding
# a day to the schedule", which keeps it a meaningful nudge without letting
# a binary count dominate the continuous tardiness signal.
SERVICE_LEVEL_WEIGHT = 24.0

# Retained as historical experiment metadata for audit scripts that reproduce
# the retired post-hoc reroute. The authoritative decoder does not branch on
# this threshold; delay probability is currently candidate-invariant.
HIGH_RISK_THRESHOLD = 0.75

# Minimum buffer (minutes) forced onto a line whenever it switches from one
# production order/style to a different one. Real lines lose time to
# re-rigging patterns/machines and a short efficiency ramp-up when
# changing styles — without this, gap-filling will happily interleave
# unrelated orders on the same line with zero gap, which isn't realistic.
# This is a placeholder default (no changeover-time table exists in the DB
# yet) — replace with real per-style-transition data if/when you have it.
CHANGEOVER_MINUTES = 25.0


def phenotype_hash(operations):
    """Stable semantic hash used to prove score/export decoder parity."""
    canonical = []
    for row in operations:
        item = {}
        for key, value in sorted(row.items()):
            if key == 'worker_segments_by_employee':
                item[key] = {
                    str(emp): [[start.isoformat(), end.isoformat()] for start, end in segments]
                    for emp, segments in sorted(value.items())
                }
            elif isinstance(value, tuple):
                item[key] = list(value)
            else:
                item[key] = value
        canonical.append(item)
    payload = json.dumps(canonical, sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def evaluate_schedule(individual, num_ops, op_details, gnn_predictions,
                       all_line_ids=None, order_deadlines=None,
                       machine_pools=None, machine_line_map=None,
                       usable_machine_count=0, present_operator_count=0,
                       return_details=False,
                       initial_machine_free=None, initial_machine_last_order=None,
                       scored_order_ids=None,
                       op_optype=None, protected_machine_type=None,
                       order_release_minutes=None, op_sequence_map=None,
                       base_time=None, worker_availability=None,
                       operator_index=None, initial_operator_reservations=None):
    """
    Simulates the execution of the line-assigned schedule chromosome.
    Calculates Makespan based on Production Line bottlenecks, integrates GNN delay/priority
    predictions, and computes both base and cushioned completion timelines.

    Returns a single-item tuple (DEAP requirement for minimization).

    Args:
        op_details: {op_key: {'setup_mins': float, 'serial_processing_mins': float}}.
            Previously this stored a single precomputed 'duration_mins' that
            divided total processing time by the FULL count of eligible
            machines company-wide — i.e. it silently assumed every
            operation always ran on every matching machine simultaneously,
            with no contention between orders and no actual decision being
            made. duration is now computed per-individual from the
            chromosome's own MC (machine-count) gene: setup_mins +
            serial_processing_mins / machine_count — so "how many machines
            to use" is an actual GA decision variable, not a fixed
            assumption. serial_processing_mins is the time to run this
            operation on exactly ONE machine (sam * quantity / efficiency).
        all_line_ids: optional list/iterable of every eligible line ID that
            exists (not just ones this individual happens to use). Lets the
            load-balance penalty notice a line left completely idle.
        machine_pools: {op_key: {line_id: [asset_id, ...]}} — the usable
            machines of the required type on each eligible line, with the 7%
            standby reserve already removed (ga/chromosome.py). This is what
            makes scheduling machine-level rather than line-level.
        machine_line_map: {asset_id: line_id}, for aggregating per-machine
            busy time back up to lines in the load-balance term.
        usable_machine_count: company-wide count of non-reserved machines.
            Denominator for the idle-labour term's achievable ceiling.
        present_operator_count: operators on shift and not on leave. Carried
            for reporting; the fitness term itself scales against the machine
            ceiling, for the reason documented at the idle-labour block.
        order_deadlines: optional dict {production_order_id: deadline in
            MINUTES FROM NOW}, i.e. the same relative-time frame as this
            function's internal simulation clock (t=0 = now). Built once in
            GAScheduler from Purchase_Orders.delivery_deadline. Orders
            not present in this dict are simply not penalized for lateness
            (unknown deadline).
        scored_order_ids: optional set of order_id strings this call should
            SCORE, as opposed to merely simulate. None (the default) scores
            every order in the chromosome, i.e. the original behaviour.

            This exists for receding-horizon rolling (ga/rolling_horizon.py):
            the GA is shown a LOOKAHEAD wider than what actually gets
            committed this round (see that module's docstring), so machine
            contention is realistic. But until this parameter existed, the
            fitness function also *scored* the whole lookahead as if every
            order in it were being committed now — so the makespan and
            deadline terms judged a schedule partly by how well it happened
            to arrange orders that will be RE-PLANNED FROM SCRATCH next
            round with fresh information. Two independent experiments
            (widening the lookahead; shrinking the commit chunk relative to
            a fixed lookahead) both made on-time rate, makespan, throughput
            and labour utilisation WORSE as the scored-but-uncommitted
            fraction grew — see PROJECT_STATUS.txt Section 0B. The GA was
            being pulled toward finishing/timing orders it had no actual
            power to lock in, at the expense of the orders it was about to
            commit.

            Only the makespan (ALPHA) term and the whole deadline-lateness
            block are restricted to scored_order_ids. load_balance_penalty
            and idle_labor_penalty stay whole-lookahead deliberately: they
            measure real physical resource usage (which machines/labour
            this schedule actually engages across everything simulated),
            not a claim about which orders are "done" — restricting those
            too would reintroduce the same kind of blindness as the
            per-(line,type) load-tracking bug fixed earlier in this file's
            history. total_delay_penalty/total_priority_reward likewise stay
            whole-lookahead: they reward the GA for sequencing high-risk /
            high-priority work early regardless of which round commits it,
            which is exactly the foresight a lookahead is for.
        op_optype: optional {op_key: canonical_operation_type}, e.g.
            ga/chromosome.py's optype_for_op. Together with
            protected_machine_type, identifies which operations in THIS
            call should get the softened machine-selection treatment in
            ga/schedule_utils.py::select_machines_and_start. None (either
            argument) disables it entirely -- every operation falls back to
            the original strict least-loaded selection, unchanged.
        protected_machine_type: optional canonical operation_type string --
            the current window's single most-congested machine type (see
            GAScheduler, which computes this once per window from carried-
            forward machine state, the same measurement built for
            PROJECT_STATUS.txt Section 0H.3/0H.6). See Section 0H.8 for why
            and how this is used.
        order_release_minutes: optional dict {production_order_id: real
            release_date offset in MINUTES FROM the same t=0 as
            order_deadlines}. GA_AUDIT.md Section 5/6: previously
            release_date was used ONLY to sort orders into rolling-horizon
            windows (ga/rolling_horizon.py), never enforced here -- every
            order's first operation defaulted to order_ready=0.0 regardless
            of its real release date, i.e. every order in a window was
            treated as available to start on day one. Measured
            (scratch/release_date_enforcement_check.py) against window 1's
            real 300-order/20-day release spread: that mis-modeling
            inflates peak same-instant machine-type backlog 6-7x (80-86%
            reduction when arrivals are spread across their real release
            dates instead) -- a real, quantified contributor to the
            10.75x capacity-floor gap and window 1's 11.3% on-time finding,
            confirmed before this parameter was added. None (the default)
            reproduces the old, unfixed behaviour exactly (every order
            ready at 0.0) for any caller that doesn't opt in.
    """
    # Split chromosome into Operation Sequence (OS), Line Assignment (LA),
    # and Machine Count (MC)
    os_array = individual[:num_ops]
    la_array = individual[num_ops:2*num_ops]
    mc_array = individual[2*num_ops:]

    # ---------------------------------------------------------
    # SAFETY NET: os_array must be a valid permutation — every real
    # operation exactly once. If a crossover/mutation operator ever
    # produces a chromosome with duplicate or missing operations,
    # hard-reject it here instead of letting it score artificially well.
    # ---------------------------------------------------------
    def _reject(why, infeasibility=None):
        # Invalid chromosome. Returns the same shape the caller asked for, so
        # a return_details=True caller never gets a bare tuple it cannot unpack.
        if return_details:
            details = {'fitness': float('inf'), 'invalid': why}
            if infeasibility is not None:
                details['infeasibility'] = infeasibility
            return (float('inf'),), details
        return (float('inf'),)

    if len(set(os_array)) != num_ops:
        return _reject('os_array is not a permutation of the operations')

    # ---------------------------------------------------------
    # SAFETY NET 2: os_array must also respect ROUTING ORDER — within a
    # production order, operations must appear in ascending sequence.
    #
    # The permutation check above cannot catch this, and its absence was
    # exploitable: this simulation enforces order_ready_times in os_array
    # VISIT order, so a chromosome listing operation 5 before operation 1
    # is simply scored as if that were the routing, and gets to pack work
    # into gaps a legal schedule cannot reach. Precedence violation was
    # therefore a fitness ADVANTAGE, and elitism preserved it — 99% of
    # orders in a real run came out unexecutable. ga/chromosome.py now
    # repairs at generation time and both genetic operators already repair,
    # so nothing should reach here in violation; this rejects it outright if
    # anything ever does, rather than letting it win again.
    #
    # Sequence comes from op_sequence_map -- the authoritative
    # Routing_Operations.operation_sequence_number, the same map
    # ga/operators.py::repair_precedence, ga/chromosome.py,
    # ga/heuristics.py and ga/local_search.py were all switched to.
    #
    # WAS: int(op_key.split('_')[1]), i.e. routing order inferred from the
    # numeric operation_id, with an `except ValueError: continue` that
    # silently skipped the check for any non-numeric id -- a validator that
    # validated nothing, without saying so. Worse, once repair_precedence
    # started ordering by real routing sequence, this validator was left
    # measuring a DIFFERENT order: on any routing where operation_id does
    # not ascend with sequence, repair would produce a correctly-ordered
    # chromosome and this check would reject it as a violation, sending
    # every individual to fitness=inf.
    #
    # A missing lookup is now a hard reject, not a silent pass: an
    # operation whose routing position is unknown cannot be confirmed
    # legal, and scoring it as if it were is what this safety net exists
    # to prevent.
    # ---------------------------------------------------------
    op_sequence_map = op_sequence_map or {}
    last_seq_seen = {}
    for op_key in os_array:
        order_id, _, _ = op_key.partition('_')
        op_seq = op_sequence_map.get(op_key)
        if op_seq is None:
            return _reject(f'no routing sequence known for operation {op_key}')
        if order_id in last_seq_seen and op_seq < last_seq_seen[order_id]:
            return _reject(f'precedence violated within order {order_id}')
        last_seq_seen[order_id] = op_seq

    order_deadlines = order_deadlines or {}

    # MACHINE-LEVEL STATE. Replaces the previous per-LINE interval lists,
    # which modelled a production line as a single serial resource — one
    # operation at a time, its other ~29 machines idle — capping the factory
    # at roughly 350 concurrently busy machines out of 3,000.
    #   machine_free[asset_id]       -> minutes-from-now when it next frees up
    #   machine_last_order[asset_id] -> the order it last ran, for changeover
    #   machine_busy[asset_id]       -> committed minutes, for load balance
    # Seeded from the previous planning window when rolling. An empty dict is
    # a clean floor at t=0; carrying real busy-times forward is what makes a
    # multi-window simulation honest, because window 2 must start against the
    # machines window 1 actually left committed rather than an idle factory.
    machine_free = dict(initial_machine_free or {})
    machine_last_order = dict(initial_machine_last_order or {})
    machine_busy = {}
    order_ready_times = {}
    # Worker state is part of the authoritative phenotype whenever an
    # operator index is supplied. Maintained production callers always
    # supply it; a missing index retains the explicit worker-blind legacy
    # mode used by isolated formula tests.
    worker_aware = operator_index is not None
    operator_reservations = normalize_reservations(initial_operator_reservations)
    operator_busy_minutes = reservation_busy_minutes(operator_reservations)
    phenotype_operations = []

    total_delay_penalty = 0.0
    total_priority_reward = 0.0
    max_cushioned_makespan = 0.0
    # Employee-minutes consumed: one operator per machine for the duration of
    # the operation. Feeds the idle-labour term below.
    labor_used_minutes = 0.0
    pool_short_count = 0

    # Simulate execution sequentially based on the operation sequence
    for i, op_key in enumerate(os_array):
        line_id = la_array[i]
        order_id = op_key.split('_')[0]

        # Duration depends on this individual's own MC gene. Now that
        # scheduling is machine-level, that choice has a real cost: the
        # machines it claims are unavailable to every other operation
        # competing for the same finite, reserve-capped pool.
        details = op_details.get(op_key, {})
        machine_count = max(1, int(mc_array[i])) if i < len(mc_array) else 1

        # The pool of real machines available for this operation on this
        # line, reserve already excluded (ga/chromosome.py). Positive work
        # without physical machines is infeasible, never virtual capacity.
        pool_ids = machine_pools.get(op_key, {}).get(line_id, []) if machine_pools else []
        if not pool_ids and calculate_operation_work(details, machine_count) > 0:
            failure = missing_machine_pool(
                order_id, op_key,
                (op_optype or {}).get(op_key) or details.get('required_capability', 'unknown'),
                line_id,
            )
            return _reject('positive work has no eligible machine pool', failure)
        if pool_ids:
            machine_count = min(machine_count, len(pool_ids))
            if machine_count < max(1, int(mc_array[i]) if i < len(mc_array) else 1):
                pool_short_count += 1

        # SPLIT, not one duration. setup is ELAPSED machine occupancy and
        # stays outside the calendar; only the productive half is walked.
        # See ga/schedule_utils.split_operation_work for the evidence (the
        # simulator models no setup at all) and for the recorded contract
        # mismatch this creates against the training data.
        setup_mins, productive_mins = split_operation_work(details, machine_count)
        duration_mins = setup_mins + productive_mins

        # An order's FIRST operation cannot start before its own real
        # release date, even if precedence (order_ready_times, absent on
        # first sight of this order) would otherwise allow t=0. Every
        # later operation of the same order is already governed by
        # precedence alone (order_ready_times has since advanced past the
        # release floor), so max() only ever binds on the first operation.
        release_floor = (order_release_minutes or {}).get(order_id, 0.0)
        order_ready = max(order_ready_times.get(order_id, 0.0), release_floor)

        # ---------------------------------------------------------
        # CUSHION CALCULATION: Factor in GNN delay probability risk
        # ---------------------------------------------------------
        preds = gnn_predictions.get(op_key, {'delay': 0.0, 'priority': 0.5})
        delay_prob = min(max(preds.get('delay', 0.0), 0.0), 1.0)
        # Clamp guards the MODEL's output; the manager's boost is applied
        # after it, so a directive is never silently capped away. See the
        # note in ga/scheduler.py where priority_multiplier is set.
        priority = min(max(preds.get('priority', 0.5), 0.0), 1.0)
        priority *= float(preds.get('priority_multiplier', 1.0))

        # The cushion pads PRODUCTIVE work, not the setup occupancy -- setup
        # is a fixed machine cost and padding it would model re-rigging
        # taking longer because an operation is delay-prone. With the buffer
        # disabled by value (RISK_BUFFER_AT_CERTAINTY = 0.0) this is
        # identical to the previous apply_risk_buffer(duration_mins, ...).
        productive_buffered = apply_risk_buffer(productive_mins, delay_prob)
        buffer_mins = setup_mins + productive_buffered

        # Machine-level placement. Changeover is charged per machine inside
        # the helper (a machine switching production order needs re-rigging),
        # and the operation starts when the last of its chosen machines is
        # free. Shared verbatim with agent/decision_engine.py so the exported
        # schedule is the schedule that was scored.
        #
        # is_protected_type: True only for operations whose type matches
        # THIS window's single most-congested type (Section 0H.8) -- every
        # other operation gets the original, untouched strict-greedy
        # selection.
        is_protected_type = bool(
            protected_machine_type is not None and op_optype is not None
            and op_optype.get(op_key) == protected_machine_type
        )
        start_time, chosen_ids = select_machines_and_start(
            pool_ids, machine_free, machine_last_order,
            order_id, machine_count, order_ready, CHANGEOVER_MINUTES,
            is_protected_type=is_protected_type,
        )

        # Authoritative elapsed execution. Setup is elapsed machine-only
        # occupancy. Productive work is performed by one fixed deterministic
        # crew and advances only over the complete crew's common availability
        # segments. Machines remain occupied across every pause.
        productive_start = start_time + setup_mins
        selected_employees = []
        worker_segments = ()
        sourcing_tier = 'worker-blind-legacy'
        if worker_aware:
            if base_time is None:
                return _reject('worker-aware decoding requires base_time')
            crew = allocate_crew(
                operator_index,
                (op_optype or {}).get(op_key) or details.get('required_capability'),
                line_id, len(chosen_ids),
                base_time + timedelta(minutes=productive_start),
                productive_buffered, operator_reservations,
                operator_busy_minutes, priority,
                state_is_normalized=True,
            )
            if crew['shortage'] is not None:
                failure = dict(crew['shortage'])
                failure.update({'order_id': str(order_id), 'operation_key': op_key})
                return _reject('operation has no feasible complete worker crew', failure)
            selected_employees = list(crew['employees'])
            worker_segments = tuple(crew['segments'])
            sourcing_tier = crew['tier']
            operator_reservations = crew['reservations']
            operator_busy_minutes = crew['busy_minutes']
            cushioned_end_dt = crew['end']
            base_end_dt = completion_after_productive_minutes(
                worker_segments, productive_mins)
            cushioned_end_time = (cushioned_end_dt - base_time).total_seconds() / 60.0
            base_end_time = (base_end_dt - base_time).total_seconds() / 60.0
        else:
            base_end_time = elapsed_completion_minutes(
                base_time, productive_start, productive_mins, worker_availability)
            cushioned_end_time = elapsed_completion_minutes(
                base_time, productive_start, productive_buffered, worker_availability)
            if base_time is not None:
                worker_segments = ((
                    base_time + timedelta(minutes=productive_start),
                    base_time + timedelta(minutes=cushioned_end_time),
                ),)

        elapsed_occupancy = max(0.0, cushioned_end_time - start_time)
        for asset_id in chosen_ids:
            machine_free[asset_id] = cushioned_end_time
            machine_last_order[asset_id] = order_id
            machine_busy[asset_id] = machine_busy.get(asset_id, 0.0) + elapsed_occupancy

        # Productive labour only. Setup and off-duty pauses hold machines but
        # do not reserve or count worker time.
        labor_used_minutes += len(chosen_ids) * productive_buffered

        phenotype_operations.append({
            'operation_key': str(op_key),
            'production_order_id': str(order_id),
            'operation_id': str(op_key).partition('_')[2],
            'line_id': int(line_id),
            'machine_ids': list(chosen_ids),
            'employee_ids': [str(e) for e in selected_employees],
            'operator_sourcing_tier': sourcing_tier,
            'setup_minutes': float(setup_mins),
            'productive_minutes': float(productive_mins),
            'buffered_productive_minutes': float(productive_buffered),
            'start_minutes': float(start_time),
            'productive_start_minutes': float(productive_start),
            'base_end_minutes': float(base_end_time),
            'end_minutes': float(cushioned_end_time),
            'machine_occupation': (float(start_time), float(cushioned_end_time)),
            'worker_segments_by_employee': {
                str(e): tuple(worker_segments) for e in selected_employees
            },
            'delay_probability': float(delay_prob),
            'priority': float(priority),
        })

        order_ready_times[order_id] = cushioned_end_time

        if cushioned_end_time > max_cushioned_makespan:
            max_cushioned_makespan = cushioned_end_time

        total_delay_penalty += (delay_prob * 50.0)

        position_weight = 1.0 - (i / num_ops)
        total_priority_reward += (priority * position_weight * 100.0)

    base_makespan = max_cushioned_makespan

    # ---------------------------------------------------------
    # COMMIT-WEIGHTED MAKESPAN: see scored_order_ids in the docstring above.
    # base_makespan (whole lookahead) still drives the idle-labour ceiling
    # below, since that term is about real machine/labour usage across
    # everything simulated. scored_makespan is what the ALPHA fitness term
    # actually optimises against — the completion of the orders this round
    # will commit, not orders that will be re-planned from scratch next
    # round regardless of how this individual arranges them.
    # ---------------------------------------------------------
    if scored_order_ids is not None:
        scored_completions = [
            c for oid, c in order_ready_times.items() if oid in scored_order_ids
        ]
        scored_makespan = max(scored_completions) if scored_completions else base_makespan
    else:
        scored_makespan = base_makespan

    # ---------------------------------------------------------
    # LOAD-BALANCE PENALTY
    # Machine busy-minutes are aggregated up to their line so this term keeps
    # the same meaning it had under the line-serial model — spread work evenly
    # across production lines — while being measured from real per-machine
    # commitments underneath.
    # ---------------------------------------------------------
    busy_per_line = {}
    for asset_id, busy in machine_busy.items():
        line_of_machine = machine_line_map.get(asset_id) if machine_line_map else None
        if line_of_machine is not None:
            busy_per_line[line_of_machine] = busy_per_line.get(line_of_machine, 0.0) + busy
    if all_line_ids:
        for ln in all_line_ids:
            busy_per_line.setdefault(ln, 0.0)
    load_values = list(busy_per_line.values())
    load_balance_penalty = statistics.pstdev(load_values) if len(load_values) > 1 else 0.0

    # ---------------------------------------------------------
    # IDLE-LABOUR PENALTY  (business objective: fully utilise labour)
    #
    # SOFT, like the deadline terms, and for the same reason: a hard
    # "no idle operators" constraint is unsatisfiable here. There are 8,302
    # present operators and only ~2,790 usable machines, so at least 66% of
    # the workforce is idle in EVERY possible schedule. A hard constraint
    # would reject the entire search space.
    #
    # Scaled against the ACHIEVABLE ceiling, not the raw headcount. Raw
    # company-wide utilisation is labour_used / (operators x makespan), which
    # can never exceed usable_machines / operators ~= 33.5% — so a penalty on
    # raw utilisation spends most of its weight on a floor no schedule can
    # move, exactly the un-actionable-constant problem already fixed for
    # overdue deadlines. Dividing by the ceiling gives the actionable
    # quantity: of the labour capacity this factory can physically engage,
    # how much did this schedule actually engage? That reduces algebraically
    # to machine-pool utilisation, which is the real lever the MC gene pulls.
    #
    # The raw company-wide rate is still what gets REPORTED, because that is
    # the number the business asked to see — see agent/decision_engine.py.
    # ---------------------------------------------------------
    engageable_machine_minutes = usable_machine_count * base_makespan
    if engageable_machine_minutes > 0:
        utilization_vs_ceiling = min(1.0, labor_used_minutes / engageable_machine_minutes)
    else:
        utilization_vs_ceiling = 1.0
    idle_labor_penalty = (1.0 - utilization_vs_ceiling) * IDLE_LABOR_WEIGHT

    # ---------------------------------------------------------
    # DEADLINE LATENESS PENALTY: for every order with a known deadline,
    # penalize the amount by which its actual (cushioned) completion time
    # exceeds the promised delivery date. This is the objective that was
    # previously entirely missing — order_deadline was fetched from the DB
    # and never used anywhere in scheduling.
    #
    # Expressed as a PER-ORDER AVERAGE in HOURS (not a raw sum in minutes
    # across every late order) — see the scale note above. A schedule with
    # 1,500 orders and one with 150 orders should be penalized based on how
    # late orders TYPICALLY are, not accumulate a penalty that's 10x bigger
    # just because there are 10x more orders in the problem instance.
    #
    # Restricted to scored_order_ids when given (see the docstring above) —
    # a provisional order still being simulated for machine contention but
    # not committed this round contributes nothing to this penalty, exactly
    # as it contributes nothing to scored_makespan.
    # ---------------------------------------------------------
    total_lateness_minutes = 0.0
    num_orders_with_deadline = 0
    num_orders_late = 0
    for order_id, completion in order_ready_times.items():
        if scored_order_ids is not None and order_id not in scored_order_ids:
            continue
        deadline = order_deadlines.get(order_id)
        if deadline is not None:
            num_orders_with_deadline += 1
            # Clamp the reference to "now" (t=0 in this simulation's clock).
            #
            # A deadline already in the past arrives here as a NEGATIVE
            # number of minutes, and max(0, completion - deadline) then
            # evaluates to completion + |overdue| — a constant floor that no
            # schedule can reduce and that the GA therefore cannot act on. It
            # inflates every candidate equally and flattens the differences
            # between them, which is exactly the wrong behaviour for a
            # selection signal.
            #
            # Clamping treats an already-overdue order as "due immediately",
            # so the penalty measures only the delay THIS schedule adds. The
            # pre-existing breach is still real and still reported — see
            # already_breached in agent/decision_engine.py — it just isn't
            # smeared across the search landscape.
            effective_deadline = max(deadline, 0.0)
            lateness = max(0.0, completion - effective_deadline)
            if lateness > 0:
                num_orders_late += 1
            total_lateness_minutes += lateness

    # Averaged over ALL orders with a deadline (on-time orders contribute 0),
    # so the magnitude reflects how late orders TYPICALLY are rather than
    # scaling with how many orders happen to be in the problem instance.
    avg_lateness_hours = (total_lateness_minutes / 60.0) / max(1, num_orders_with_deadline)
    frac_orders_late = num_orders_late / max(1, num_orders_with_deadline)

    # Two terms, because they answer different questions and a single average
    # conflates them: "how late are we typically" (avg_lateness_hours) versus
    # "how much of the order book misses its date at all" (frac_orders_late).
    # A schedule that makes one order catastrophically late and a schedule
    # that makes half the book slightly late can score identically on the
    # average alone; on-time delivery rate is also the KPI a garment factory
    # actually reports. SERVICE_LEVEL_WEIGHT is in the same hours-scale as
    # the other terms, so a fully-late order book costs the equivalent of
    # SERVICE_LEVEL_WEIGHT hours of makespan.
    lateness_penalty_term = (avg_lateness_hours * ZETA) + (frac_orders_late * SERVICE_LEVEL_WEIGHT)

    fitness_score = (
        (ALPHA * (scored_makespan / 60.0))
        + (GAMMA * (total_delay_penalty / max(1, num_ops)))
        - (DELTA * (total_priority_reward / max(1, num_ops)))
        + (EPSILON * (load_balance_penalty / 60.0))
        + lateness_penalty_term
        + idle_labor_penalty
    )

    if return_details:
        # Same simulation, richer output. Exists so the heuristic baselines in
        # ga/heuristics.py can be scored on identical numbers to the GA
        # without a second implementation of the metrics — the failure mode
        # this codebase keeps hitting.
        late_hours = [
            max(0.0, completion - max(order_deadlines[o], 0.0)) / 60.0
            for o, completion in order_ready_times.items()
            if order_deadlines.get(o) is not None
            and (scored_order_ids is None or o in scored_order_ids)
        ]
        tardy = [h for h in late_hours if h > 0]
        return (fitness_score,), {
            'fitness': fitness_score,
            # The scalarised objective's own terms, each already multiplied
            # by its weight and therefore directly comparable and summing to
            # 'fitness'. Emitted here rather than recomputed by a reporting
            # script: a second implementation of this arithmetic is exactly
            # the duplicated-quantity failure this codebase has repeatedly
            # paid for, and a breakdown that does not add up to the fitness
            # the GA actually optimised would be worse than none.
            # priority_reward is NEGATIVE by construction (it is subtracted).
            'fitness_components': {
                'makespan': ALPHA * (scored_makespan / 60.0),
                'delay_penalty': GAMMA * (total_delay_penalty / max(1, num_ops)),
                'priority_reward': -(DELTA * (total_priority_reward / max(1, num_ops))),
                'load_balance': EPSILON * (load_balance_penalty / 60.0),
                'lateness': lateness_penalty_term,
                'idle_labour': idle_labor_penalty,
            },
            # The two halves of lateness_penalty_term, kept separate because
            # they answer different questions (see the comment above it).
            'fitness_lateness_split': {
                'avg_lateness_hours_term': avg_lateness_hours * ZETA,
                'service_level_term': frac_orders_late * SERVICE_LEVEL_WEIGHT,
            },
            # Whole-lookahead completion time, same scope as load-balance and
            # idle-labour above — NOT scored_makespan, which only measures
            # the committed subset and would understate how long the
            # simulated machines are actually occupied.
            'makespan_minutes': base_makespan,
            'makespan_days': base_makespan / 1440.0,
            'scored_makespan_minutes': scored_makespan,
            'orders_with_deadline': num_orders_with_deadline,
            'orders_late': num_orders_late,
            'on_time_rate': 1.0 - frac_orders_late,
            'mean_tardiness_hours': (sum(tardy) / len(tardy)) if tardy else 0.0,
            'max_tardiness_hours': max(tardy) if tardy else 0.0,
            'labor_used_minutes': labor_used_minutes,
            # Same number under a name that states what it is, so a consumer
            # cannot mistake demand for supply. Prefer this key.
            'labor_required_minutes': labor_used_minutes,
            'labor_utilization_rate': (
                labor_used_minutes / (present_operator_count * base_makespan)
                if present_operator_count and base_makespan > 0 else 0.0
            ),
            'labor_utilization_vs_ceiling': utilization_vs_ceiling,
            'load_balance_stdev_minutes': load_balance_penalty,
            'machines_engaged': len(machine_busy),
            'pool_short_operations': pool_short_count,
            # Per-order lateness in the SAME time frame the caller supplied
            # deadlines in, so a rolling simulation can aggregate genuine
            # on-time performance across windows instead of averaging each
            # window's own rate.
            'order_lateness_hours': {
                o: max(0.0, completion - max(order_deadlines[o], 0.0)) / 60.0
                for o, completion in order_ready_times.items()
                if order_deadlines.get(o) is not None
                and (scored_order_ids is None or o in scored_order_ids)
            },
            # Machine state to carry into the next window.
            'final_machine_free': dict(machine_free),
            'final_machine_last_order': dict(machine_last_order),
            # Every order's own cushioned completion time, same t=0 frame as
            # order_release_minutes/order_deadlines -- lets a caller compute
            # real in-flight-order-count-over-time (release <= t <=
            # completion) instead of only aggregate lateness/makespan.
            'order_completion_minutes': dict(order_ready_times),
            'phenotype_operations': phenotype_operations,
            'final_operator_reservations': {
                str(emp): list(intervals)
                for emp, intervals in operator_reservations.items()
            },
            'operator_busy_minutes': dict(operator_busy_minutes),
            'execution_feasible': True,
            'phenotype_hash': phenotype_hash(phenotype_operations),
        }

    return (fitness_score,)

if __name__ == "__main__":
    print("Testing Line-Based Fitness Function...")

    mock_os = [f"100_{i}" for i in range(5)] + [f"101_{i}" for i in range(5)]
    random.shuffle(mock_os)
    mock_la = [random.randint(1, 4) for _ in range(10)]
    mock_mc = [random.randint(1, 20) for _ in range(10)]

    mock_individual = mock_os + mock_la + mock_mc
    num_ops = 10

    mock_details = {
        key: {'setup_mins': 15.0, 'serial_processing_mins': random.randint(60, 600)}
        for key in mock_os
    }

    mock_gnn = {
        key: {
            'delay': random.uniform(0.0, 1.0),
            'priority': random.uniform(0.0, 1.0)
        } for key in mock_os
    }

    mock_deadlines = {'100': 200.0, '101': 50.0}  # '101' likely to be late -> penalized

    fitness = evaluate_schedule(mock_individual, num_ops, mock_details, mock_gnn,
                                 all_line_ids=[1, 2, 3, 4], order_deadlines=mock_deadlines)

    print(f"[OK] Fitness Evaluated!")
    print(f"Mock Chromosome Length: {len(mock_individual)}")
    print(f"Calculated Fitness Score: {fitness[0]:.2f} (Lower is better)")