import math
import random
from collections import defaultdict
from deap import base, creator, tools
import numpy as np
import sys
import os
import pandas as pd

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.logger import get_logger
from utils.machine_taxonomy import machine_type_to_operation_type
from utils.config import GA_MAX_MACHINES_PER_OPERATION
from utils.machine_reserve import build_reserve, NON_USABLE_STATUSES
from ga.operators import repair_precedence
from ga.infeasibility import ScheduleInfeasibleError, missing_machine_pool
from ga.heuristics import choose_line_least_loaded
# Module-qualified (not "from ga.schedule_utils import MC_PROTECTION_FRACTION")
# so a live monkeypatch of ga.schedule_utils.MC_PROTECTION_FRACTION -- the
# same pattern buffer_reenable_test.py already uses for
# RISK_BUFFER_AT_CERTAINTY -- is actually seen here. A value-import would
# freeze the default at load time and never see the patch.
import ga.schedule_utils as schedule_utils

logger = get_logger("ga_chromosome", "ga.log")

if not hasattr(creator, "FitnessMin"):
    creator.create("FitnessMin", base.Fitness, weights=(-1.0,))

if not hasattr(creator, "Individual"):
    creator.create("Individual", list, fitness=creator.FitnessMin)

class ChromosomeFactory:
    def __init__(self, df_routings: pd.DataFrame, df_machines: pd.DataFrame, df_lines: pd.DataFrame, *, ownership_snapshot=None, legacy_mode=None):
        self.df_routings = df_routings.drop_duplicates(subset=['production_order_id', 'operation_id']).copy()
        self.ownership_snapshot = ownership_snapshot
        self.legacy_mode = legacy_mode
        self.df_machines = df_machines
        self.df_lines = df_lines
        # {op_key: single-machine work minutes}. Populated by GAScheduler once
        # op_details exists, because the work figure depends on line
        # efficiency, which the factory does not own. Deliberately NOT
        # recomputed here from sam x quantity: a second, slightly-different
        # work formula is precisely the duplication that produced the earlier
        # double-division bug. Empty means work-aware balancing degrades to
        # the first eligible line, and generate_random_individual warns.
        self.op_work = {}
        # PROJECT_STATUS.txt Section 0H.9: this window's single most-
        # congested machine type (or None -- most windows have nothing
        # protected). Set by GAScheduler right after computing it, once
        # this factory already exists. Read by _max_machines_for below;
        # left None here so any caller that never sets it (tests, the
        # module's own __main__ smoke test) keeps the exact prior
        # behaviour.
        self.protected_machine_type = None
        self.eligibility_map = self._build_eligibility_map()
        
        self.op_keys = self.df_routings['production_order_id'].astype(str) + "_" + self.df_routings['operation_id'].astype(str)
        self.op_keys = self.op_keys.tolist()

        # op_key -> operation_sequence_number, the authoritative routing order
        # (Routing_Operations.operation_sequence_number). Passed to
        # repair_precedence below and, for the GA operators' own repair calls,
        # to ga/scheduler.py's identically-sourced seq_lookup -- never
        # inferred from operation_id (see ga/operators.py::repair_precedence).
        self.op_sequence_map = {
            f"{row['production_order_id']}_{row['operation_id']}": row['operation_sequence_number']
            for _, row in self.df_routings.iterrows()
        }

    def _load_ownership(self, order_ids):
        from utils.factory_ownership import resolve_ownership
        self.ownership_snapshot = resolve_ownership(
            order_ids, self.ownership_snapshot, legacy_mode=self.legacy_mode)
        return {} if self.ownership_snapshot is None else self.ownership_snapshot.for_orders(order_ids)

    def _build_eligibility_map(self):
        eligibility = {}
        self.parallel_capacity_map = {}
        # op_key -> {line_id: count of USABLE machines on that line}. Used to
        # bound the MC (machine-count) gene per-line.
        self.machines_per_line_map = {}
        # op_key -> {line_id: [asset_id, ...]}. The actual machine identities,
        # needed now that ga/fitness.py schedules at machine level rather than
        # treating a line as one serial resource. Reserved machines are absent
        # from these lists, so nothing downstream can schedule onto them.
        self.machine_ids_per_line_map = {}
        valid_machines = self.df_machines.dropna(subset=['line_id'])
        
        active_lines = self.df_lines[self.df_lines['is_active'] == 1]['line_id'].tolist()

        active_machines = valid_machines[valid_machines['line_id'].isin(active_lines)].copy()
        # Match on operation_type (from Machine_Capabilities) via the
        # canonical taxonomy mapping — NOT a machine_model string prefix.
        # Previously this matched a 4-char prefix of required_machine_type
        # against machine_model text (e.g. "sing" in "juki-sing-83"), the
        # same fragile hack fixed in graph/edge_builder.py. It happened to
        # work here too by the same naming coincidence, but was one more
        # place that heuristic could silently break.
        has_optype = 'operation_type' in active_machines.columns
        if has_optype:
            active_machines['operation_type'] = active_machines['operation_type'].astype(str).str.strip().str.lower()

        # Standby reserve: 7% of each machine type company-wide, never
        # available to scheduled work. Dropped from the pools here, at the
        # single point where every downstream consumer (MC bound, fitness
        # machine-level simulation, schedule export) gets its machine
        # identities — so the policy cannot be bypassed by one of them.
        self.reserved_asset_ids, self.usable_cap_by_type = build_reserve(self.df_machines)
        if self.reserved_asset_ids:
            active_machines = active_machines[~active_machines['asset_id'].isin(self.reserved_asset_ids)]
        # Complete the earlier live-capacity fix: build_reserve() already
        # subtracts Breakdown/Under_Maintenance assets from each type's
        # aggregate MC ceiling, but the per-line identity pools previously
        # still contained those exact assets. The decoder could therefore
        # select a machine that its own aggregate capacity model called
        # unavailable. Reserve membership remains status-blind; status is
        # applied only after the deterministic reserve set is chosen.
        if 'current_status' in active_machines.columns:
            active_machines = active_machines[
                ~active_machines['current_status'].isin(NON_USABLE_STATUSES)
            ].copy()
        self.usable_asset_ids = set(active_machines['asset_id'].dropna().tolist())

        # op_key -> the canonical operation_type it needs, so the company-wide
        # cap can be looked up in the combined MC bound below.
        self.optype_for_op = {}

        # ------------------------------------------------------------------
        # ONE FACTORY PER ORDER (audit #4).
        #
        # Line eligibility used to be every line COMPANY-WIDE carrying a
        # matching machine type, with no factory term anywhere in the GA --
        # not in eligibility, not in fitness, not in the heuristics. So
        # consecutive operations of one order could be placed in different
        # factories at zero transfer time and zero cost, and the GA had every
        # incentive to do exactly that whenever it shortened a schedule. An
        # order is produced at a branch; it is not scattered across branches.
        #
        # HOW AN ORDER'S FACTORY IS DETERMINED. There is no authoritative
        # factory column to read: Production_Orders is
        # (production_order_id, po_id, product_id, target_quantity,
        # scheduled_start/end_date, status) and Purchase_Orders carries only
        # client/deadline fields -- checked against the live schema, not
        # assumed. The existing Job_Shop_Schedules rows are not usable as the
        # source either: they were written by an older seeder revision and
        # place every one of the 1,029 orders across ~5.76 factories on
        # average, so deriving "the order's factory" from them would just
        # re-import the violation.
        #
        # So it is DERIVED, mirroring the rule the current seeder itself uses
        # (data_generation/seed_database_fixed.py, `feasible_factories` /
        # `order_factory`): a factory is feasible for an order when it holds
        # at least one usable machine of EVERY operation type that order's
        # routing requires. The seeder then picks with random.choice; the GA
        # needs reproducibility and cannot re-roll per run, so the pick here
        # is deterministic and capacity-aware -- least loaded so far,
        # measured per usable machine so a large factory absorbs
        # proportionally more, ties broken by factory_id.
        # ------------------------------------------------------------------
        # A machine frame with no factory_id at all is a SINGLE-SITE dataset
        # (synthetic fixtures, and any caller predating factory_id being
        # added to database/queries.py::fetch_machine_state). Modelled as one
        # factory rather than skipping the constraint: the rule below then
        # still runs and is trivially satisfied, instead of being silently
        # bypassed by a branch that turns the whole fix off.
        if 'factory_id' not in active_machines.columns:
            logger.warning(
                "Machine data carries no factory_id -- treating the whole plant as a "
                "single factory. The one-factory-per-order constraint is inert here."
            )
            active_machines = active_machines.copy()
            active_machines['factory_id'] = 0

        line_factory = {}
        for line_id, fid in zip(active_machines['line_id'], active_machines['factory_id']):
            line_factory.setdefault(line_id, fid)
        self.line_factory = line_factory

        optype_by_key = {}
        optypes_by_order = defaultdict(set)
        for _, op in self.df_routings.iterrows():
            key = f"{op['production_order_id']}_{op['operation_id']}"
            canonical = machine_type_to_operation_type(op.get('required_machine_type', ''))
            optype_by_key[key] = canonical
            optypes_by_order[str(op['production_order_id'])].add(canonical)

        # {factory_id: {optype, ...}} and each factory's usable machine count.
        optypes_by_factory = defaultdict(set)
        capacity_by_factory = defaultdict(int)
        if has_optype:
            for fid, otype in zip(active_machines['factory_id'], active_machines['operation_type']):
                optypes_by_factory[fid].add(otype)
                capacity_by_factory[fid] += 1

        # OWNERSHIP IS READ, NOT RE-DERIVED (Batch-4 fix 5b).
        #
        # This block used to assign every order a factory afresh on each
        # construction. Two problems, both real:
        #   * a horizon rebuild could move an order -- including one already
        #     released or part-produced -- to a different branch, which is
        #     physically impossible for work in progress; and
        #   * graph/edge_builder.py needs the SAME answer to scope contention
        #     cliques, and a second independent derivation is free to drift
        #     from this one without anything failing visibly.
        # Both are closed by reading the persisted map the seeder wrote.
        # See utils/factory_ownership.py.
        #
        # The feasibility logic below is retained but now only VALIDATES the
        # recorded ownership rather than choosing it, so an order recorded
        # against a branch that cannot actually run its routing is still
        # caught loudly instead of silently producing an empty machine pool.
        self.order_factory = {}
        persisted = self._load_ownership(sorted(optypes_by_order))
        load_by_factory = defaultdict(float)
        for order_id in sorted(optypes_by_order, key=lambda o: (len(str(o)), str(o))):
            required = optypes_by_order[order_id]
            feasible = [
                f for f in sorted(optypes_by_factory)
                if all(t is not None and t in optypes_by_factory[f] for t in required)
            ]
            owner = persisted.get(str(order_id))
            if owner is not None:
                if feasible and owner not in feasible:
                    raise ScheduleInfeasibleError(missing_machine_pool(
                        order_id, f"{order_id}_*",
                        f"recorded owning factory {owner} cannot run this order's "
                        f"full routing (" + ", ".join(sorted(str(t) for t in required))
                        + f"); feasible factories are {feasible}",
                    ))
                self.order_factory[str(order_id)] = owner
                continue
            if not feasible:
                # Distinguish two genuinely different failures, and let the
                # MORE SPECIFIC one win:
                #   * a required capability exists in NO factory at all --
                #     that is a per-OPERATION infeasibility, and the loop
                #     below reports it precisely (which operation, which
                #     capability). Leave this order unassigned and fall
                #     through to it.
                #   * every capability exists somewhere, but no SINGLE
                #     factory holds all of them -- that failure only exists
                #     because of the one-factory rule, so it is reported
                #     here, at order level.
                company_wide = set().union(*optypes_by_factory.values()) if optypes_by_factory else set()
                if any(t is None or t not in company_wide for t in required):
                    continue
                raise ScheduleInfeasibleError(missing_machine_pool(
                    order_id, f"{order_id}_*",
                    "no single factory covers this order's full routing: "
                    + ", ".join(sorted(str(t) for t in required)),
                ))
            # NEW orders only -- anything with recorded ownership took the
            # `continue` above. Deterministic, capacity-weighted least-loaded.
            #
            # DEFERRED (reported, not fixed): the load increment below is
            # len(required), a count of distinct capability TYPES an order
            # needs (1-5), not its operation count, quantity or processing
            # minutes. So this balances "how many capability kinds each
            # branch has been handed", which is a weak proxy for workload --
            # a 12,000-unit order and a 1,500-unit one increment identically.
            # Correcting it needs per-order work content, which is computed
            # later (ga/scheduler.py builds op_details AFTER this factory
            # exists), so it is a restructure rather than a one-line change.
            chosen = min(
                feasible,
                key=lambda f: (load_by_factory[f] / max(1, capacity_by_factory[f]), f),
            )
            self.order_factory[str(order_id)] = chosen
            load_by_factory[chosen] += len(required)

        for _, op in self.df_routings.iterrows():
            key = f"{op['production_order_id']}_{op['operation_id']}"
            canonical_optype = optype_by_key[key]
            self.optype_for_op[key] = canonical_optype

            matching = (
                active_machines[active_machines['operation_type'] == canonical_optype]
                if (has_optype and canonical_optype is not None)
                else active_machines.iloc[0:0]
            )
            # Scope to this ORDER's factory -- the constraint itself.
            order_fid = self.order_factory.get(str(op['production_order_id']))
            if order_fid is not None:
                matching = matching[matching['factory_id'] == order_fid]

            if matching.empty:
                # Operations passed here reserve processing work under the
                # scheduler's existing minimum-quantity/setup convention.
                # Do not fabricate capacity for an impossible instance.
                raise ScheduleInfeasibleError(missing_machine_pool(
                    op['production_order_id'], key,
                    canonical_optype or str(op.get('required_machine_type', 'unknown')),
                ))
            else:
                eligible_lines = matching['line_id'].unique().tolist()
                eligibility[key] = eligible_lines
                self.parallel_capacity_map[key] = max(1, len(matching))
                self.machines_per_line_map[key] = matching.groupby('line_id').size().to_dict()
                self.machine_ids_per_line_map[key] = {
                    line_id: sorted(grp['asset_id'].tolist())
                    for line_id, grp in matching.groupby('line_id')
                }

        return eligibility

    def _max_machines_for(self, op_key, line_id):
        """
        The MC gene's ceiling for a given (operation, line) pairing.

        ONE combined bound, evaluated as a single min() over all three limits,
        rather than separate checks applied in different places that could
        disagree with each other:

          1. GA_MAX_MACHINES_PER_OPERATION - the business preference ceiling.
          2. usable machines of the required type ON THIS LINE - physical
             availability where the work is actually assigned. Reserved
             machines are already absent from this count.
          3. 93% of that type's COMPANY-WIDE total - the standby-reserve cap
             (utils/machine_reserve.py).

        On this factory's data (2) almost always binds first: a line carries
        ~6 machines of a given type against a company-wide usable cap of 558.
        (3) is still evaluated here rather than assumed non-binding, so the
        policy holds if the machine mix ever changes.

        A 4th, narrower limit applies on top of the three above, only for
        THIS window's protected (most-congested) type, and only when
        ga.schedule_utils.MC_PROTECTION_FRACTION < 1.0 (PROJECT_STATUS.txt
        Section 0H.9): the combined bound is further scaled down by that
        fraction, so a single operation can no longer claim the type's
        entire per-line pool by itself. Every other type, and this type
        whenever the fraction is 1.0 (the default), is unaffected.
        """
        on_this_line = self.machines_per_line_map.get(op_key, {}).get(line_id, 1)
        optype = self.optype_for_op.get(op_key)
        company_cap = self.usable_cap_by_type.get(optype, GA_MAX_MACHINES_PER_OPERATION)
        bound = max(1, min(int(on_this_line), int(company_cap), GA_MAX_MACHINES_PER_OPERATION))

        fraction = schedule_utils.MC_PROTECTION_FRACTION
        if (fraction < 1.0 and self.protected_machine_type is not None
                and optype == self.protected_machine_type):
            bound = max(1, math.ceil(bound * fraction))
        return bound

    def generate_random_individual(self):
        os_array = random.sample(self.op_keys, len(self.op_keys))

        # Repair precedence at BIRTH, not just after crossover/mutation.
        #
        # BUG FIX: custom_crossover() and custom_mutate() both call
        # repair_precedence(), but freshly generated individuals never did —
        # and they enter the population in two places: the initial population
        # (pop_size individuals) and the random immigrants injected every
        # generation by GAScheduler.optimize(). evaluate_schedule() only
        # validated that os_array is a PERMUTATION, never that it respects
        # routing order, so these individuals were scored as-is.
        #
        # Worse, they scored WELL: the simulation enforces order_ready_times
        # in os_array visit order, so an individual that runs operation 5
        # before operation 1 is free to pack work into gaps a legal schedule
        # cannot use. Violating precedence is a fitness advantage, elitism
        # and HallOfFame then preserve the cheaters, and the exported plan
        # inherits it. Measured on a real 50-generation run before this fix:
        # 1,396 of 1,410 orders (99%) came out with operations scheduled out
        # of sequence — e.g. order 1023 running its op 1 on Aug 30 and its
        # op 2 on Aug 13 — making the whole schedule, and every completion
        # and lateness figure derived from it, unexecutable.
        #
        # Repairing here (before LA/MC are drawn, so the per-position line
        # and machine-count genes line up with the operation that actually
        # ends up at that index) means every individual is precedence-valid
        # from the moment it exists.
        os_array = repair_precedence(os_array, self.op_sequence_map)

        # WORK-AWARE LINE ASSIGNMENT.
        #
        # This was random.choice(eligible_lines). Choosing uniformly among ~99
        # eligible lines balances operation COUNT but not WORK, and on this
        # order book those are wildly different things: quantities range from
        # 1,500 to 12,000 units, so lines ended up carrying 35-95 operations
        # each (near-perfectly even) while the work behind them differed by
        # 181,646 minutes of standard deviation — about 126 days of spread.
        # That imbalance, not machine capacity, was the dominant cause of a
        # 313-day makespan against a ~26-day capacity floor.
        #
        # Each operation now goes to the eligible line carrying the least work
        # PER MACHINE of the required type. Per-machine matters because lines
        # hold between 1 and 15 machines of a given type — a line with 15
        # should absorb fifteen times the work of a line with 1.
        #
        # top_k=2 rather than strict greedy: an entirely deterministic
        # constructor would make every individual in the population identical,
        # which is the opposite of what an initial population is for.
        #
        # Load is keyed by (line, machine type), matching the granularity of
        # the resource — a line's Flatlock machines being busy says nothing
        # about its Body Assembly machines. Keying by line alone left 271 of
        # 494 pools permanently unused. See
        # ga/heuristics.py::choose_line_least_loaded.
        line_load = {}
        la_array = []
        for op in os_array:
            eligible = self.eligibility_map[op]
            line_id = choose_line_least_loaded(op, eligible, line_load, self, random, top_k=2)
            if line_id is None:
                line_id = eligible[0] if eligible else 1
            la_array.append(line_id)
            pool_key = (line_id, self.optype_for_op.get(op))
            line_load[pool_key] = line_load.get(pool_key, 0.0) + self.op_work.get(op, 0.0)
        # MC: how many machines (from the eligible pool on the assigned
        # line) to run this operation's lots across in parallel — the
        # decision variable that actually answers "should this operation
        # use 30-40 machines". Bounded by both real availability on that
        # line and the GA_MAX_MACHINES_PER_OPERATION business cap.
        mc_array = [
            random.randint(1, self._max_machines_for(op, la_array[i]))
            for i, op in enumerate(os_array)
        ]
        chromosome = creator.Individual(os_array + la_array + mc_array)
        return chromosome

toolbox = base.Toolbox()

def register_ga_components(chromosome_factory):
    toolbox.register("individual", chromosome_factory.generate_random_individual)
    toolbox.register("population", tools.initRepeat, list, toolbox.individual)
    logger.info("DEAP Toolbox registered with OS/LA chromosome representation.")
    return toolbox
