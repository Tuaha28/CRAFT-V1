"""
Company-wide standby machine reserve.

BUSINESS RULE: hold MACHINE_RESERVE_FRACTION (7%) of every machine TYPE back
from scheduled work, so a breakdown can be covered immediately from spare
capacity instead of preempting another order's committed schedule.

Applied per machine type COMPANY-WIDE, not per line. This factory averages
~6 machines of a given type on a line (min 1, max 15), so a per-line 7% would
round to zero almost everywhere and the policy would silently do nothing.

Two consumers must agree exactly on which machines are reserved:
  - ga/chromosome.py, which builds the per-line machine pools the GA schedules
    onto (and therefore the MC gene's ceiling), and
  - agent/impact_assessor.py, which must exclude the reserve from its ordinary
    alternate-machine search and then draw on it first when a machine breaks.
If those two disagreed, the assessor could hand a breakdown a machine the GA
had already committed to other work. Membership is therefore a pure,
deterministic function of the machine table — no randomness, no time
dependence, stable across processes and across runs.
"""

import math

from utils.config import MACHINE_RESERVE_FRACTION
from utils.logger import get_logger

logger = get_logger("machine_reserve", "ga.log")

# Shared between build_reserve's live-capacity count and
# reserve_pool_by_type's replacement-offer filter, so "which statuses mean
# a machine isn't really available right now" is defined in exactly one
# place rather than as two separately-typed literal tuples that could drift.
NON_USABLE_STATUSES = ('Breakdown', 'Under_Maintenance')


def usable_cap_for_type(total_of_type: int, fraction: float = MACHINE_RESERVE_FRACTION) -> int:
    """
    How many machines of a type may be committed to scheduled work.

    Integer arithmetic on purpose. In float, 0.07 * 600 is
    42.000000000000006, so ceil() would reserve 43 while "93% of 600" is 558
    — the reserve count and the usable cap would not add up. Computing the
    cap first and deriving the reserve as the remainder keeps them exactly
    complementary: 600 -> 558 usable + 42 reserved.
    """
    if total_of_type <= 0:
        return 0
    keep_percent = int(round((1.0 - fraction) * 100))
    cap = (total_of_type * keep_percent) // 100
    # Never reserve away the last machine of a type — a type with a single
    # machine would otherwise become unschedulable entirely.
    return max(1, min(total_of_type, cap))


def build_reserve(df_machines, fraction: float = MACHINE_RESERVE_FRACTION) -> tuple:
    """
    Chooses the reserved machines.

    df_machines: as returned by database.queries.fetch_machine_state() —
        needs at least asset_id and operation_type columns.

    Selection rule: within each operation_type, machines are ranked by
    asset_id descending and the highest IDs are reserved. Any deterministic
    rule would satisfy the correctness requirement above; highest-ID-first is
    used because it is trivially reproducible by hand when checking a
    schedule, and because it spreads the reserve across lines incidentally
    rather than concentrating it (asset_id does not correlate with line_id in
    this schema).

    Deliberately NOT reserving by current_status: it is tempting to reserve
    machines that are already Idle or Under_Maintenance, but that would make
    reserve membership drift every time the floor state changes, and the
    reserve would then differ between the GA run and a later breakdown
    assessment against the same schedule. RESERVE MEMBERSHIP stays exactly
    this deterministic, status-blind rule -- unchanged by the live-capacity
    fix below.

    Returns (reserved_asset_ids: set, usable_cap_by_type: dict).
    usable_cap_by_type maps normalised operation_type -> the LIVE usable
    count: the 93%-of-total POLICY cap (usable_cap_for_type), minus
    whichever of those specific non-reserved machines currently have a
    NON_USABLE_STATUSES status (Breakdown / Under_Maintenance). This is one
    of the three bounds on the MC gene (see
    ga/chromosome.py::_max_machines_for) and, going forward, the intended
    capacity denominator for any workload-norm calculation
    (WLC_RELEASE_GATE_PROPOSAL.md Section 8.4).

    BUG FIX (2026-08-24): previously usable_cap_by_type was a pure function
    of TOTAL asset count -- identical whether every machine of a type was
    healthy or half of them were down, because current_status was never
    read here at all. Found while stress-testing the WLC release-gate
    proposal against an extended multi-machine-type outage: a pre-existing
    gap in the GA's OWN capacity model (ga/chromosome.py never filtered on
    current_status either, for the MC-gene ceiling), not something that
    proposal introduced. This does NOT touch which specific machines are
    schedulable (ga/chromosome.py's per-line pools still list every
    non-reserved asset_id regardless of status -- a separate, deeper gap,
    not fixed here) -- only the AGGREGATE COMPANY-WIDE CEILING NUMBER is
    now live-capacity-aware. A machine missing 'current_status' entirely
    (synthetic test fixtures that never set the column) is treated as
    Active, matching reserve_pool_by_type's existing convention.
    """
    reserved = set()
    usable_cap_by_type = {}

    if df_machines is None or df_machines.empty or 'operation_type' not in df_machines.columns:
        logger.warning("No machine/operation_type data supplied — reserve is empty.")
        return reserved, usable_cap_by_type

    df = df_machines.dropna(subset=['operation_type']).copy()
    df['_optype'] = df['operation_type'].astype(str).str.strip().str.lower()
    has_status = 'current_status' in df.columns

    for optype, group in df.groupby('_optype'):
        asset_ids = sorted(set(group['asset_id'].tolist()), reverse=True)
        total = len(asset_ids)
        cap = usable_cap_for_type(total, fraction)
        reserve_count = total - cap
        reserve_ids = set(asset_ids[:reserve_count]) if reserve_count > 0 else set()

        broken_usable = 0
        if has_status:
            # drop_duplicates: a machine can carry more than one
            # Machine_Capabilities row (the LEFT JOIN in
            # database.queries.fetch_machine_state), so group may repeat an
            # asset_id -- the original code already defends against this
            # for asset_ids via set(); status lookup needs the same care.
            status_by_asset = (
                group.drop_duplicates(subset=['asset_id'])
                .set_index('asset_id')['current_status']
            )
            for asset_id in asset_ids:
                if asset_id in reserve_ids:
                    continue  # a reserved machine breaking doesn't cost usable capacity
                status = status_by_asset.get(asset_id, 'Active')
                if status is None or status != status:  # None or NaN (NaN != NaN is always True)
                    status = 'Active'
                if str(status) in NON_USABLE_STATUSES:
                    broken_usable += 1

        live_cap = max(0, cap - broken_usable)
        usable_cap_by_type[optype] = live_cap
        if reserve_ids:
            reserved.update(reserve_ids)
        logger.info(
            f"Reserve '{optype}': {total} total -> {cap} usable (policy cap), "
            f"{broken_usable} currently down -> {live_cap} live usable, "
            f"{reserve_count} held in reserve."
        )

    logger.info(
        f"Standby reserve: {len(reserved)} of {len(df['asset_id'].unique())} machines held back "
        f"({fraction:.0%} per type, company-wide). These are never assigned to scheduled work."
    )
    return reserved, usable_cap_by_type


def reserve_pool_by_type(df_machines, reserved_asset_ids: set) -> dict:
    """
    Reserved machines grouped by normalised operation_type, for the breakdown
    path in agent/impact_assessor.py. Only machines in a usable state are
    offered as replacements — a reserved machine that is itself in Breakdown
    is no use as cover.
    """
    pools = {}
    if df_machines is None or df_machines.empty:
        return pools
    df = df_machines.dropna(subset=['operation_type']).copy()
    df['_optype'] = df['operation_type'].astype(str).str.strip().str.lower()
    for _, row in df.iterrows():
        asset_id = row['asset_id']
        if asset_id not in reserved_asset_ids:
            continue
        if str(row.get('current_status', 'Active')) in NON_USABLE_STATUSES:
            continue
        pools.setdefault(row['_optype'], []).append(asset_id)
    for optype in pools:
        pools[optype].sort()
    return pools
