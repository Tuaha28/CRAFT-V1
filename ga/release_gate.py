"""
Workload Control (WLC) pre-shop-pool release gate.

Design: WLC_RELEASE_GATE_PROPOSAL.md. Orders whose real release_date has
passed sit in a pool and are only admitted into active scheduling (a
rolling-horizon window's commit batch) if doing so would not push any
(line, type) pool's outstanding direct workload past a norm expressed as
`norm_days` of that pool's own LIVE usable capacity
(utils/machine_reserve.py's now-live `usable_cap_by_type` --
WLC_RELEASE_GATE_PROPOSAL.md Section 9). This module reads that value via
a fresh `ChromosomeFactory`, so a norm built today reflects today's real
machine availability (breakdowns included), not the static pre-fix
baseline -- see `test_norm_uses_live_capacity_not_static_baseline` in
tests/test_release_gate.py for the direct confirmation.

Three guards are built in from the start, per review, not added after:
  - FEASIBILITY: an order whose own release + critical-path already
    exceeds its deadline is structurally late no matter what the gate
    does. Reported explicitly (self.infeasible_orders), never silently
    absorbed into a late-but-uncomplaining schedule.
  - STARVATION: an order waiting in the pool longer than
    `starvation_days` force-admits regardless of norm, so a tight norm
    can never leave an order queued indefinitely -- the failure mode
    date-only release cannot have but a naive gate can.
  - MANAGER BYPASS: an order_id in `manager_forced_ids` always admits
    immediately, skipping the norm check entirely
    (WLC_RELEASE_GATE_PROPOSAL.md Section 8.3) -- a real, existing
    capability (agent/decision_engine.py's `manager_forced_orders`) that
    must not silently regress once a pool exists.

Scope, deliberately (WLC_RELEASE_GATE_PROPOSAL.md Section 3): DIRECT LOAD
only, one uniform norm formula applied per pool (the bottleneck pool's own
smaller live capacity already gives it a smaller absolute norm with no
special-casing needed -- see Section 4's DBR discussion). Indirect load
(discounting upstream, not-yet-arrived work) is NOT implemented here --
flagged as a deferred refinement, not assumed necessary.

NOT wired into ga/rolling_horizon.py's default path -- opt-in via the
`release_gate` parameter there, ships unused unless explicitly
constructed and passed in, same "inactive unless deliberately enabled"
convention as RISK_BUFFER_AT_CERTAINTY / MC_PROTECTION_FRACTION.
"""
from collections import defaultdict

import pandas as pd

from ga.chromosome import ChromosomeFactory
from ga.scheduler import DEFAULT_LINE_EFFICIENCY, DEFAULT_SETUP_TIME_MINS
from utils.config import GA_MAX_MACHINES_PER_OPERATION
from utils.logger import get_logger

logger = get_logger("release_gate", "ga.log")

MINS_PER_DAY = 1440.0

# Starting-point defaults, both explicitly flagged as needing empirical
# tuning via ablation (WLC_RELEASE_GATE_PROPOSAL.md Sections 4/6/7) -- not
# adopted with confidence, just where an ablation should start from.
DEFAULT_NORM_DAYS = 2.0
DEFAULT_STARVATION_DAYS = 14.0


def _avg_line_efficiency(df_lines):
    if 'target_efficiency_percentage' in df_lines.columns:
        eff = df_lines.loc[df_lines['is_active'] == 1, 'target_efficiency_percentage']
        if not eff.empty and pd.notna(eff.mean()):
            return max(eff.mean() / 100.0, 0.05)
    return DEFAULT_LINE_EFFICIENCY


class ReleaseGate:
    """
    Owns all pre-shop-pool state: which orders are pooled vs admitted,
    per-pool outstanding load, and the feasibility/starvation/manager-
    bypass rules. ga/rolling_horizon.py (or a test) only ever calls
    `admit_next_batch()` / `mark_completions()` / `has_pending()` --
    nothing else needs to know this class's internal bookkeeping.
    """

    def __init__(self, df_routings, df_machines, df_lines, origin,
                 manager_forced_ids=None,
                 norm_days=DEFAULT_NORM_DAYS,
                 starvation_days=DEFAULT_STARVATION_DAYS,
                 log_fn=print,
                 ownership_snapshot=None, legacy_mode=None):
        self.log_fn = log_fn
        self.starvation_mins = starvation_days * MINS_PER_DAY
        self.origin = origin

        # Full-book factory, built ONCE. optype/machine-ceiling/live-
        # capacity data (self.factory.usable_cap_by_type,
        # machines_per_line_map) are functions of df_machines/df_lines
        # only, not of which orders are present -- safe to share across
        # every pooled order regardless of which window eventually
        # schedules it. This is also the ONLY point where LIVE capacity
        # (Section 9's build_reserve fix) enters this module: nothing
        # here reads machine current_status directly, it all flows
        # through ChromosomeFactory -> utils.machine_reserve.build_reserve.
        # Ownership threaded through like every other ChromosomeFactory
        # consumer (Batch-5 fix 2): this call site was missed, so the
        # release gate reached the ownership read with no snapshot and no
        # legacy opt-in and aborted. Passing it explicitly keeps the
        # 'one resolved snapshot per context' guarantee intact.
        self.factory = ChromosomeFactory(
            df_routings, df_machines, df_lines,
            ownership_snapshot=ownership_snapshot, legacy_mode=legacy_mode)
        avg_efficiency = _avg_line_efficiency(df_lines)

        self.norm_days = norm_days
        self.norm_minutes_by_pool = {
            optype: norm_days * MINS_PER_DAY * usable
            for optype, usable in self.factory.usable_cap_by_type.items()
        }

        self.manager_forced_ids = set(str(x) for x in (manager_forced_ids or []))
        self.infeasible_orders = []  # reported once here, never silently absorbed

        self._orders = self._build_pool(df_routings, avg_efficiency)
        # Pool, in admission-priority order == release-date order (matches
        # ga/rolling_horizon.py's existing all_orders sort so a disabled
        # gate and an enabled gate walk orders in the same base order).
        self._pool_ids = sorted(
            self._orders.keys(),
            key=lambda oid: (self._orders[oid]['release_mins'], oid)
        )
        self._pool_entry_mins = {oid: self._orders[oid]['release_mins'] for oid in self._pool_ids}

        # order_id -> {'pool_loads': {...}, 'completion_mins': None or float}
        self._admitted = {}

        self._report_infeasible_orders()

    # ------------------------------------------------------------------
    def _mins_from_origin(self, dt, clamp_zero):
        if pd.isna(dt):
            return None
        mins = (pd.Timestamp(dt).to_pydatetime() - self.origin).total_seconds() / 60.0
        return max(0.0, mins) if clamp_zero else mins

    def _build_pool(self, df_routings, avg_efficiency):
        df = df_routings.drop_duplicates(subset=['production_order_id', 'operation_id']).copy()
        df['release_date'] = pd.to_datetime(df['release_date'])
        df = df.sort_values(['production_order_id', 'operation_sequence_number'])

        orders = {}
        for _, row in df.iterrows():
            order_id = str(row['production_order_id'])
            key = f"{row['production_order_id']}_{row['operation_id']}"

            sam = float(row.get('sam', 30)) or 30.0
            quantity = int(row.get('target_quantity', 1)) or 1
            remaining_frac = row.get('remaining_frac', 1.0)
            remaining_frac = 1.0 if pd.isna(remaining_frac) else min(max(float(remaining_frac), 0.0), 1.0)
            quantity = max(1, int(round(quantity * remaining_frac)))
            serial_mins = (sam * quantity) / avg_efficiency

            optype = self.factory.optype_for_op.get(key)
            per_line = self.factory.machines_per_line_map.get(key, {})
            company_cap = self.factory.usable_cap_by_type.get(optype, GA_MAX_MACHINES_PER_OPERATION)
            best_line_machines = max(per_line.values()) if per_line else 1
            max_machines = max(1, min(int(best_line_machines), int(company_cap), GA_MAX_MACHINES_PER_OPERATION))
            min_duration_mins = DEFAULT_SETUP_TIME_MINS + (serial_mins / max_machines)

            if order_id not in orders:
                orders[order_id] = {
                    'order_id': order_id,
                    'release_mins': self._mins_from_origin(row.get('release_date'), clamp_zero=True) or 0.0,
                    'deadline_mins': self._mins_from_origin(row.get('order_deadline'), clamp_zero=False),
                    'pool_loads': defaultdict(float),
                    'critical_path_mins': 0.0,
                }
            if optype is not None:
                orders[order_id]['pool_loads'][optype] += serial_mins
            orders[order_id]['critical_path_mins'] += min_duration_mins

        return orders

    def _report_infeasible_orders(self):
        for order_id in self._pool_ids:
            o = self._orders[order_id]
            if o['deadline_mins'] is None:
                continue  # no known deadline -- nothing to check against
            earliest_possible_completion = o['release_mins'] + o['critical_path_mins']
            if earliest_possible_completion > o['deadline_mins']:
                shortfall_days = (earliest_possible_completion - o['deadline_mins']) / MINS_PER_DAY
                report = {
                    'order_id': order_id,
                    'release_days': o['release_mins'] / MINS_PER_DAY,
                    'own_critical_path_days': o['critical_path_mins'] / MINS_PER_DAY,
                    'deadline_days': o['deadline_mins'] / MINS_PER_DAY,
                    'shortfall_days': shortfall_days,
                }
                self.infeasible_orders.append(report)
                self.log_fn(
                    f"RELEASE GATE: order {order_id} is INFEASIBLE regardless of "
                    f"scheduling -- earliest possible completion is "
                    f"{shortfall_days:.1f} days past its deadline even running "
                    f"alone with unlimited priority (release "
                    f"{o['release_mins']/MINS_PER_DAY:.1f}d + own critical path "
                    f"{o['critical_path_mins']/MINS_PER_DAY:.1f}d > deadline "
                    f"{o['deadline_mins']/MINS_PER_DAY:.1f}d). Still pooled and will "
                    f"still be scheduled best-effort -- this is a report, not a "
                    f"decision to drop the order."
                )

    # ------------------------------------------------------------------
    def has_pending(self):
        return len(self._pool_ids) > 0

    def pool_size(self):
        return len(self._pool_ids)

    def _current_outstanding(self, as_of_mins):
        outstanding = defaultdict(float)
        for state in self._admitted.values():
            completion = state['completion_mins']
            if completion is None or completion > as_of_mins:
                for optype, mins in state['pool_loads'].items():
                    outstanding[optype] += mins
        return outstanding

    def next_release_mins(self):
        """
        Earliest release_mins among still-pooled orders, or None if the
        pool is empty. Lets a caller fast-forward its own notion of
        current time when a round admits nothing because everything left
        genuinely hasn't been released yet -- see
        admit_next_batch's `current_time_mins` contract below.
        """
        if not self._pool_ids:
            return None
        return min(self._orders[oid]['release_mins'] for oid in self._pool_ids)

    def admit_next_batch(self, max_batch, current_time_mins, starvation_reference_mins=None):
        """
        Walk the pool in release-date order, admitting up to max_batch
        orders as of `current_time_mins` -- the caller's own notion of
        "now" for RELEASE-ELIGIBILITY purposes (ga/rolling_horizon.py
        passes max(machine_free.values()), i.e. how far real simulated
        time has actually progressed; a standalone caller can just track
        elapsed days itself).

        starvation_reference_mins: the "now" STARVATION is measured
        against. Defaults to current_time_mins (the natural single-call
        behaviour, where there is only one "now"). A caller ACCUMULATING
        several admit_next_batch calls into one batch (ga/rolling_horizon.py,
        WLC_RELEASE_GATE_PROPOSAL.md Section 10) must pass a value that
        stays FIXED at the genuinely-elapsed real time for the whole
        accumulation, separate from current_time_mins -- which that same
        caller legitimately advances ahead, scanning for more release-
        eligible orders to gather. Conflating the two (an earlier version
        of this integration did) lets pure lookahead scanning manufacture
        FALSE starvation: advancing current_time_mins to find more
        orders would also make already-pooled, merely norm-blocked
        orders look like they've been "waiting" that whole scanned
        span, cascading into force-admitting far more of the batch than
        the norm should ever allow in one go. Caught by
        test_accumulation_still_respects_the_norm_across_the_whole_batch
        before it reached real data.

        An order is even ELIGIBLE this round only if its own real
        release_mins <= current_time_mins (or it is manager-forced) --
        an order that hasn't actually been placed yet in reality is never
        admitted early just because it happens to fit the norm; the pool
        is a queue of REAL, already-existing orders, not a lookahead into
        orders that don't exist yet. Eligible orders admit if ANY of:
          - order_id is in manager_forced_ids (unconditional bypass,
            including bypassing the release-not-yet-arrived check itself
            -- harmless, since ga/fitness.py's ready-time floor still
            independently prevents it from actually STARTING early), or
          - it has starved (waited longer than starvation_mins, measured
            against starvation_reference_mins, since its own release), or
          - admitting it keeps every touched pool's outstanding total
            (existing admitted-not-yet-complete load + this order's own
            load) at or under that pool's norm.
        Orders that don't fit (or aren't released yet) are left in the
        pool and re-considered next call, not dropped -- a later,
        smaller, already-released order CAN be admitted ahead of an
        earlier one that doesn't fit yet (literature-standard look-ahead
        scan, not strict FIFO blocking).

        Returns the admitted order_ids, in the order they were admitted.
        """
        if not self._pool_ids:
            return []
        if starvation_reference_mins is None:
            starvation_reference_mins = current_time_mins

        outstanding = self._current_outstanding(current_time_mins)

        admitted_ids = []
        still_pooled = []
        for order_id in self._pool_ids:
            o = self._orders[order_id]
            forced = order_id in self.manager_forced_ids
            released_yet = o['release_mins'] <= current_time_mins

            if not released_yet and not forced:
                still_pooled.append(order_id)
                continue

            if len(admitted_ids) >= max_batch:
                still_pooled.append(order_id)
                continue

            starved = released_yet and (starvation_reference_mins - o['release_mins']) > self.starvation_mins

            if not (forced or starved):
                fits = all(
                    outstanding[optype] + load <= self.norm_minutes_by_pool.get(optype, float('inf'))
                    for optype, load in o['pool_loads'].items()
                )
                if not fits:
                    still_pooled.append(order_id)
                    continue

            if forced:
                self.log_fn(f"RELEASE GATE: order {order_id} admitted via manager bypass (norm skipped).")
            elif starved:
                self.log_fn(
                    f"RELEASE GATE: order {order_id} force-admitted -- waited "
                    f"{(starvation_reference_mins - o['release_mins']) / MINS_PER_DAY:.1f}d in the pool, "
                    f"past the {self.starvation_mins / MINS_PER_DAY:.1f}d starvation limit."
                )

            for optype, load in o['pool_loads'].items():
                outstanding[optype] += load
            self._admitted[order_id] = {'pool_loads': dict(o['pool_loads']), 'completion_mins': None}
            admitted_ids.append(order_id)

        self._pool_ids = still_pooled
        return admitted_ids

    def mark_completions(self, order_completion_minutes: dict):
        """
        order_completion_minutes: {order_id: completion_mins}, the SAME
        t=0-relative frame as everything else here (e.g.
        ga/fitness.py::evaluate_schedule's new order_completion_minutes
        return field). Orders not in self._admitted (not gated through
        this instance) are ignored.
        """
        for order_id, completion_mins in order_completion_minutes.items():
            if order_id in self._admitted:
                self._admitted[order_id]['completion_mins'] = completion_mins
