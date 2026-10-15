import sys
import os
import random
import copy
import numpy as np
import pandas as pd
from datetime import datetime
from deap import base, tools, algorithms

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.logger import get_logger
from utils.config import (
    GA_MAX_MACHINES_PER_OPERATION, GA_LOTS_PER_OPERATION,
    DURATION_CALIBRATION_FACTOR as CONFIG_DURATION_CALIBRATION_FACTOR,
)
from utils.derived_constants import get_derived
from ga.chromosome import ChromosomeFactory, creator
from ga.fitness import evaluate_schedule
from ga.operators import custom_crossover, custom_mutate
from ga.schedule_utils import fuzzy_urgency_priority
from ga.heuristics import build_population_seeds, baseline_chromosomes
from ga.local_search import polish as local_search_polish
from ga.alns import alns_refine as alns_refine_search

logger = get_logger("ga_scheduler", "ga.log")

DEFAULT_LINE_EFFICIENCY = 0.85
DEFAULT_SETUP_TIME_MINS = 15.0


def most_congested_machine_type(df_machines, machine_free):
    """
    This window's single most-congested machine type, by the SAME relative-
    congestion measure PROJECT_STATUS.txt Section 0H.3 used to find Tubular
    Sleeve Join: mean carried-forward machine_free per type, minus the
    cross-type mean. The type with the largest positive margin -- booked
    furthest ahead of its peers -- is returned; None if there's nothing
    carried forward yet (machine_free empty, e.g. window 1), no usable
    machine/type data, only one type to compare, or the types are tied
    (margin <= 0 for the argmax -- nothing is GENUINELY more congested).

    Deliberately NOT hardcoded to any specific type name -- Section 0H.6
    traced Tubular Sleeve Join's congestion to its ROUTING POSITION (always
    the last of 5 fixed steps), not anything intrinsic to that type, so a
    different routing structure or dataset could make a different type the
    bottleneck. Measuring this fresh each window is what makes the fix
    generalise rather than quietly assume today's answer forever.

    Factored out as a pure, module-level function (rather than inline in
    GAScheduler.__init__) so it's unit-testable without constructing a full
    scheduler -- same reasoning as data_generation/seed_database_fixed.py's
    sample_execution_variance_multiplier/compute_queue_delay_mins.

    df_machines: needs at least asset_id and operation_type columns (as
        returned by database.queries.fetch_machine_state()).
    machine_free: {asset_id: minutes-from-origin when it next frees up},
        e.g. GAScheduler's initial_machine_free (carried from a prior
        window). Missing/never-touched machines are treated as free at 0.
    """
    if not machine_free or df_machines is None or df_machines.empty:
        return None
    if 'operation_type' not in df_machines.columns:
        return None

    machines = df_machines.dropna(subset=['operation_type']).copy()
    if machines.empty:
        return None
    machines['_optype'] = machines['operation_type'].astype(str).str.strip().str.lower()

    type_free_sums, type_counts = {}, {}
    for _, row in machines.iterrows():
        t = row['_optype']
        free = machine_free.get(row['asset_id'], 0.0)
        type_free_sums[t] = type_free_sums.get(t, 0.0) + free
        type_counts[t] = type_counts.get(t, 0) + 1

    if len(type_counts) < 2:
        return None  # nothing to be relatively MORE congested than

    type_means = {t: type_free_sums[t] / type_counts[t] for t in type_counts}
    grand_mean = sum(type_means.values()) / len(type_means)
    margins = {t: m - grand_mean for t, m in type_means.items()}
    most_congested = max(margins, key=margins.get)
    if margins[most_congested] <= 0:
        return None
    return most_congested

# Target number of gene mutations per individual, used to derive the per-gene
# mutation probability from the problem size. See the indpb calculation in
# GAScheduler.__init__ for why a fixed per-gene rate does not survive a jump
# from 20 to 7,050 operations.
MUTATIONS_PER_INDIVIDUAL = 6.0

def make_key(order_id, operation_id):
    return f"{order_id}_{operation_id}"


# ---------------------------------------------------------------------------
# Worker-process plumbing for parallel fitness evaluation
#
# The read-only scheduling context is sent to each worker ONCE at pool
# creation and held in this module-level global, rather than being bound into
# the evaluate callable.
#
# That distinction is the whole ballgame. The obvious wiring --
# toolbox.register("evaluate", evaluate_schedule, op_details=..., machine_pools=...)
# and then pool.map over it -- pickles every one of those dictionaries again
# for each chunk of every generation. machine_pools alone maps each of ~1,500
# operations to ~99 lines of asset-id lists. Measured on a 300-order window:
# serial 34.0s, that version 69.8s. The transfer cost was double the compute
# it was trying to spread.
# ---------------------------------------------------------------------------
_WORKER_CTX = {}


def _init_eval_worker(ctx):
    """Runs once per worker process at pool startup."""
    global _WORKER_CTX
    _WORKER_CTX = ctx


def _eval_in_worker(individual):
    """
    Evaluates one chromosome using the context already resident in this
    worker. Only the chromosome crosses the process boundary per call.
    """
    return evaluate_schedule(individual, **_WORKER_CTX)

class GAScheduler:
    def __init__(
        self, 
        df_routings: pd.DataFrame, 
        df_machines: pd.DataFrame, 
        df_lines: pd.DataFrame, 
        gnn_predictions: dict, 
        current_time: datetime = None,
        manager_prioritized_orders: list = None,
        present_operator_count: int = 0,
        initial_machine_free: dict = None,
        initial_machine_last_order: dict = None,
        scored_order_ids: set = None,
        ownership_snapshot=None, legacy_mode=None,
        worker_availability=None, operator_index=None,
        initial_operator_reservations=None,
        option_b_logging: bool = False,
    ):
        logger.info("Initializing Line-Based GA Scheduler with Pure Manager Directives...")
        self.df_routings = df_routings
        self.df_machines = df_machines
        self.df_lines = df_lines
        
        self.gnn_predictions = copy.deepcopy(gnn_predictions)
        self.now = current_time or datetime.now()
        # Worker-availability predicate handed to the shared calendar walk in
        # ga/fitness.py. None means factory-level availability -- the three 8h
        # bands tile all 24 hours, so the walk inserts no pause and spans equal
        # work content, which is what this scheduler has always computed. A
        # per-worker predicate belongs here once worker-aware assignment exists;
        # until then this stays None rather than guessing a band.
        self.worker_availability = worker_availability
        # Employee identity is decoded deterministically from this immutable
        # index. Maintained production callers supply it; None is retained
        # only for explicitly worker-blind formula/unit tests.
        self.operator_index = operator_index
        self.initial_operator_reservations = initial_operator_reservations
        self.option_b_logging = bool(option_b_logging)
        self.option_b_events = []
        self._option_b_generation = 0
        
        # Explicit orders checked by the manager on the frontend UI dashboard
        self.manager_prioritized_orders = [str(o) for o in (manager_prioritized_orders or [])]
        
        self.factory = ChromosomeFactory(df_routings, df_machines, df_lines, ownership_snapshot=ownership_snapshot, legacy_mode=legacy_mode)
        self.num_ops = len(self.factory.op_keys)
        
        self.active_line_ids = self.df_lines[self.df_lines['is_active'] == 1]['line_id'].tolist()
        
        # The calibration the LOADED DATABASE was actually generated with.
        self.duration_calibration_factor = float(get_derived(
            "duration_calibration_factor", CONFIG_DURATION_CALIBRATION_FACTOR,
            "GA durations will not match the timings the database was seeded with.",
        ))

        # LINE EFFICIENCY (audit #6, silent-default fix). This used to fall
        # back to DEFAULT_LINE_EFFICIENCY = 0.85 whenever the column was
        # missing or degenerate, then clamp with max(..., 0.05) -- two
        # separate places manufacturing a plausible-looking number out of
        # absent data, in a term that divides every duration in the schedule.
        # Missing efficiency data is now a hard error: planning the entire
        # factory against an invented efficiency is worse than refusing to
        # plan, and this value is available in every real dataset.
        if 'target_efficiency_percentage' not in self.df_lines.columns:
            raise ValueError(
                "df_lines has no 'target_efficiency_percentage' column -- cannot "
                "derive line efficiency, which scales every operation duration. "
                "Refusing to plan against a manufactured default."
            )
        eff_series = self.df_lines.loc[self.df_lines['is_active'] == 1, 'target_efficiency_percentage']
        eff_mean = eff_series.mean() if not eff_series.empty else float('nan')
        if eff_series.empty or pd.isna(eff_mean) or eff_mean <= 0:
            raise ValueError(
                f"No usable target_efficiency_percentage across "
                f"{len(eff_series)} active line(s) (mean={eff_mean!r}). Efficiency "
                f"divides every duration; a substituted value would silently "
                f"rescale the whole schedule."
            )
        avg_efficiency = eff_mean / 100.0
        if avg_efficiency < 0.05 or avg_efficiency > 1.5:
            # Outside any physically sensible band -- almost always a unit
            # error (a fraction stored where a percentage was expected).
            # Surfaced rather than clamped into looking reasonable.
            raise ValueError(
                f"Mean line efficiency {avg_efficiency:.4f} is outside the plausible "
                f"0.05-1.5 range; check whether target_efficiency_percentage is a "
                f"percentage or a fraction."
            )
        logger.info(f"Line efficiency (mean of active lines): {avg_efficiency:.4f}")

        self.op_details = {}
        order_deadlines_minutes = {}
        self.order_deadline_abs = {}
        # GA_AUDIT.md Section 5/6: real release_date, in the same t=0=self.now
        # relative-minutes frame as order_deadlines_minutes below. Previously
        # release_date was read ONLY for window sorting (ga/rolling_horizon.py)
        # and never reached the simulation itself -- every order's first
        # operation was free to start at t=0 regardless of when it actually
        # releases. Read by ga/fitness.py::evaluate_schedule as a ready-time
        # floor. Missing/NaT release dates default to 0.0 (available
        # immediately), same as a missing deadline means "not penalized".
        self.order_release_minutes = {}

        for _, row in self.factory.df_routings.iterrows():
            key = make_key(row['production_order_id'], row['operation_id'])
            order_id = str(row['production_order_id'])
            
            sam = float(row.get('sam', 30))
            if sam <= 0: sam = 30.0
                
            quantity = int(row.get('target_quantity', 1))
            if quantity <= 0: quantity = 1

            # Only schedule the work that is actually LEFT. remaining_frac is
            # the fraction of this operation's lots not yet 'Done' (see
            # database/queries.py::fetch_active_orders_and_routings). Without
            # this the GA re-plans finished work from scratch: 27% of all
            # lot-rows in this database are already Done and 1,922 of 7,050
            # operations are partially complete, so every completion time —
            # and therefore every lateness figure — was inflated by work that
            # does not need doing. Defensive: treat a missing/NaN value as
            # fully remaining rather than silently scheduling nothing.
            remaining_frac = row.get('remaining_frac', 1.0)
            if pd.isna(remaining_frac):
                remaining_frac = 1.0
            remaining_frac = min(max(float(remaining_frac), 0.0), 1.0)
            quantity = max(1, int(round(quantity * remaining_frac)))

            # serial_processing_mins is the TOTAL work content of this
            # operation expressed as single-machine minutes. fitness.py then
            # divides it by the chromosome's MC gene to get an actual
            # duration, so "how many machines to use" is a real GA decision
            # rather than a fixed assumption.
            #
            # BUG FIX (parallelism was counted twice): this previously divided
            # the quantity by GA_LOTS_PER_OPERATION (60) *as well*, and
            # fitness.py then divided the result by MC again. Both divisions
            # represent the same thing — running the operation on several
            # machines at once — so the work was discounted twice over. A
            # 6,750-unit operation at sam=2.5 has ~19,850 minutes of real work
            # content; the old expression produced ~330, roughly 60x too fast.
            #
            # The lot decomposition is a description of HOW the work is split
            # across machines, not an extra speedup on top of splitting it.
            # The 60 lots ARE the parallel machines, which is precisely what
            # the MC gene now models against a finite, reserve-capped pool.
            #
            # This fix is only safe alongside the machine-level capacity model
            # in fitness.py. Under the old line-serial model, honest durations
            # against ~350 reachable machines gave a 212-day makespan and put
            # every order past its deadline again. See ga/schedule_utils.py
            # ::select_machines_and_start.
            # DURATION_CALIBRATION_FACTOR applied here (audit #2): the
            # seeder multiplies by it when generating every actual/planned
            # timestamp in the database, and this expression omitted it, so
            # the GA was planning each operation at 1/0.8255 = 1.21x the
            # duration the GNN was trained on and the schedule was measured
            # against.
            #
            # Read from what the SEEDER actually derived for the loaded
            # database (outputs/derived_constants.json), not from a static
            # constant: the seeder recomputes this from the source
            # spreadsheets on every run, so a config literal goes stale the
            # moment anyone reseeds. Falls back to the config value, loudly,
            # only when no seeder-derived value exists.
            serial_processing_mins = (
                self.duration_calibration_factor * sam * quantity
            ) / avg_efficiency
            self.op_details[key] = {
                'required_capability': self.factory.optype_for_op.get(key),
                'setup_mins': DEFAULT_SETUP_TIME_MINS,
                'serial_processing_mins': serial_processing_mins,
            }
            
            if order_id not in self.order_release_minutes:
                release_val = row.get('release_date', None)
                if pd.notna(release_val):
                    release_dt = pd.Timestamp(release_val).to_pydatetime()
                    self.order_release_minutes[order_id] = max(
                        0.0, (release_dt - self.now).total_seconds() / 60.0)
                else:
                    self.order_release_minutes[order_id] = 0.0

            # Extract Deadline & Calculate Urgency Score (0 to 10)
            urgency_score = 5.0
            if order_id not in self.order_deadline_abs:
                deadline_val = row.get('order_deadline', None)
                if pd.notna(deadline_val):
                    deadline_dt = pd.Timestamp(deadline_val).to_pydatetime()
                    self.order_deadline_abs[order_id] = deadline_dt
                    mins_left = (deadline_dt - self.now).total_seconds() / 60.0
                    order_deadlines_minutes[order_id] = mins_left
                    
                    # Urgency bands are expressed in DAYS against the customer
                    # delivery deadline. They used to be 12h / 24h / 72h,
                    # which were calibrated for the old internal
                    # scheduled_end_date — a date already in the past for 79%
                    # of orders, so effectively everything scored 10.0.
                    # Customer deadlines on this data sit 30-60 days out
                    # (see database/queries.py), against a makespan measured
                    # in weeks, so hour-scale bands would now collapse the
                    # other way and score everything 2.0. These bands are a
                    # business-policy choice, not a constant — retune them if
                    # the order book's lead times change.
                    days_left = mins_left / 1440.0
                    if days_left <= 3: urgency_score = 10.0
                    elif days_left <= 7: urgency_score = 8.0
                    elif days_left <= 14: urgency_score = 5.0
                    else: urgency_score = 2.0
                else:
                    self.order_deadline_abs[order_id] = None

            # --- MANAGER DIRECTIVE CHECK (NO LOYALTY MODELING) ---
            # If the manager selected this order on the frontend, apply fuzzy urgency boost.
            # Otherwise, multiplier is exactly 1.0.
            if order_id in self.manager_prioritized_orders:
                fuzzy_multiplier = fuzzy_urgency_priority(urgency_score)
            else:
                fuzzy_multiplier = 1.0
            
            # Store the manager's boost SEPARATELY from the model's priority.
            #
            # BUG FIX: this used to do priority *= fuzzy_multiplier, writing
            # the boosted value back into the prediction. ga/fitness.py then
            # clamps priority to [0,1] as a defensive guard on the model
            # output — and since the priority head is a sigmoid, any boost
            # large enough to matter was clamped straight back off. A
            # multiplier of 3.0 on a typical priority of 0.50 gives 1.50,
            # which becomes 1.00: identical to a multiplier of 2.0, or 2.5, or
            # anything else above 2.0. The manager's most urgent orders were
            # exactly the ones whose boost was discarded.
            #
            # Keeping them separate means the clamp still protects against a
            # malformed model output while the business directive is applied
            # to the reward term, where it is free to exceed 1.0.
            if key in self.gnn_predictions:
                self.gnn_predictions[key]['priority_multiplier'] = fuzzy_multiplier
        
        self.order_deadlines_minutes = order_deadlines_minutes

        # Work content per operation, shared with the chromosome factory and
        # the genetic operators so all three balance lines on the SAME number
        # rather than each deriving its own.
        self.op_work = {
            key: detail['serial_processing_mins'] for key, detail in self.op_details.items()
        }
        self.factory.op_work = self.op_work

        # op_key -> routing sequence number, for the heuristic constructors.
        # Sourced from the real column, not inferred from operation_id.
        self.seq_lookup = {
            make_key(row['production_order_id'], row['operation_id']): row['operation_sequence_number']
            for _, row in self.factory.df_routings.iterrows()
        }
        
        self.toolbox = base.Toolbox()
        self.toolbox.register("individual", self.factory.generate_random_individual)
        # Diverse seeding, not 50 draws of one construction. See
        # ga/heuristics.py::build_population_seeds for why this matters:
        # crossover between near-identical individuals cannot produce anything
        # new, and a population built from one greedy rule shares that rule's
        # blind spots. Random immigrants in optimize() deliberately bypass this
        # and use toolbox.individual instead, so unguided material keeps
        # arriving throughout the run.
        self.toolbox.register("population", self._seeded_population)
        
        # Machine-level scheduling state. machine_line_map lets the
        # load-balance term roll per-machine busy time back up to lines;
        # usable_machine_count is the company-wide non-reserved total, which
        # is the denominator for the idle-labour term's achievable ceiling.
        self.machine_line_map = {
            row['asset_id']: row['line_id']
            for _, row in self.df_machines.dropna(subset=['line_id']).iterrows()
        }
        # Same identities the decoder can actually select: active-line,
        # non-reserved, and not Breakdown/Under_Maintenance. The previous
        # denominator subtracted only reserve assets, so the idle-labour
        # ceiling still counted machines excluded by the live-capacity
        # aggregate even after that earlier fix.
        self.usable_machine_count = len(self.factory.usable_asset_ids)
        self.present_operator_count = present_operator_count or 0
        # {asset_id: minutes-from-origin when it frees up} left behind by an
        # earlier planning window. Empty means an idle floor at t=0.
        self.initial_machine_free = dict(initial_machine_free or {})
        self.initial_machine_last_order = dict(initial_machine_last_order or {})
        # See scored_order_ids in ga/fitness.py::evaluate_schedule. None
        # (default) scores the whole lookahead, i.e. unchanged behaviour for
        # any caller that doesn't opt in. Stored as-is (no cast) so a caller
        # passing e.g. a stringified set gets exactly the membership test it
        # asked for.
        self.scored_order_ids = scored_order_ids

        # PROJECT_STATUS.txt Section 0H.8: this window's single most-
        # congested machine type, computed ONCE here (not per fitness call)
        # from the SAME carried-forward machine_free state used to build the
        # window, so it costs nothing extra per generation. Reuses exactly
        # the measurement built for the Section 0H.3/0H.6 diagnostics: mean
        # carried-forward free-time per type, relative to the cross-type
        # mean -- the type booked furthest ahead of its peers is the one
        # softened in ga/fitness.py's machine selection this window. Window
        # 1 (empty initial_machine_free) has nothing carried forward yet, so
        # no type is protected -- there is nothing to measure a window
        # ahead of.
        self.protected_machine_type = self._compute_protected_machine_type()
        if self.protected_machine_type:
            logger.info(f"Bottleneck-protected machine type this window: {self.protected_machine_type}")
        # PROJECT_STATUS.txt Section 0H.9: ChromosomeFactory._max_machines_for
        # reads this straight off the factory instance (set here, once the
        # factory already exists and this window's type is known) so every
        # consumer of it -- generate_random_individual, build_chromosome
        # (population seeding AND baseline_chromosomes, i.e. the cheap EDF
        # check too) -- picks it up with no further wiring.
        self.factory.protected_machine_type = self.protected_machine_type

        logger.info(
            f"Capacity: {self.usable_machine_count} usable machines "
            f"({len(self.factory.reserved_asset_ids)} held in standby reserve), "
            f"{self.present_operator_count} present operators. "
            f"Labour utilisation ceiling: "
            f"{(self.usable_machine_count / self.present_operator_count * 100) if self.present_operator_count else 0:.1f}%."
        )

        # One definition of the evaluation context, reused for the serial
        # toolbox binding and for shipping to worker processes.
        # initial_machine_* is carried forward from the previous planning
        # window so the GA optimises against the floor it will actually
        # inherit; giving it only to the final measurement would optimise an
        # idle factory the window does not have.
        self._eval_ctx = {
            'num_ops': self.num_ops,
            'op_details': self.op_details,
            'gnn_predictions': self.gnn_predictions,
            'all_line_ids': self.active_line_ids,
            'order_deadlines': self.order_deadlines_minutes,
            'order_release_minutes': self.order_release_minutes,
            'machine_pools': self.factory.machine_ids_per_line_map,
            'machine_line_map': self.machine_line_map,
            'usable_machine_count': self.usable_machine_count,
            'present_operator_count': self.present_operator_count,
            'initial_machine_free': self.initial_machine_free,
            'initial_machine_last_order': self.initial_machine_last_order,
            'scored_order_ids': self.scored_order_ids,
            'op_optype': self.factory.optype_for_op,
            'protected_machine_type': self.protected_machine_type,
            # Authoritative routing order for the precedence safety net --
            # the same map the repair operators use, so validation and
            # repair can never disagree about what "in order" means.
            'op_sequence_map': self.seq_lookup,
            # The scheduling ORIGIN. evaluate_schedule works in relative
            # minutes; the shared calendar walk needs real timestamps to know
            # where shift boundaries fall, so it has to be told t=0.
            'base_time': self.now,
            # Injected worker-availability predicate for the calendar walk.
            # None = factory-level availability (the three 8h bands tile all
            # 24 hours, so no pause is inserted). Deliberately not defaulted
            # to a band-of-start guess: a real pause pattern belongs to the
            # ASSIGNED worker, and that assignment does not exist until the
            # worker-aware decoder does.
            'worker_availability': self.worker_availability,
            'operator_index': self.operator_index,
            'initial_operator_reservations': self.initial_operator_reservations,
        }
        self._register_serial_evaluate()
        # MUTATION RATE SCALED TO PROBLEM SIZE.
        #
        # indpb is a PER-GENE probability, and it was hardcoded at 0.1. On a
        # 20-operation test that means ~2 mutations per individual, which is
        # sensible. On the real 7,050-operation problem it means ~705 random
        # swaps, ~705 line reassignments and ~705 machine-count changes to a
        # single offspring — that is not mutation, it is randomisation, and it
        # explains an observed run where the population average (21,931) was
        # worse than every dispatching baseline while the elite sat at 10,412:
        # the operators were destroying good individuals faster than selection
        # could accumulate them.
        #
        # Standard practice is an expected O(1) mutations per individual, i.e.
        # indpb ~ k/n. The 0.1 cap keeps small problems behaving as before.
        self.mutation_indpb = min(0.1, MUTATIONS_PER_INDIVIDUAL / max(1, self.num_ops))
        logger.info(
            f"Mutation rate: indpb={self.mutation_indpb:.5f} "
            f"(~{self.mutation_indpb * self.num_ops:.1f} expected gene mutations per individual "
            f"across {self.num_ops} operations)."
        )

        # op_work is passed to BOTH operators so line reassignment stays
        # work-aware during evolution. Without it, mutation and the
        # post-crossover repair would keep drawing lines uniformly at random
        # and steadily undo the balanced initial population from generation 1
        # onward.
        mate_kwargs = {"trace_callback": self._record_option_b_trace} if self.option_b_logging else {}
        mutate_kwargs = {"trace_callback": self._record_option_b_trace} if self.option_b_logging else {}
        self.toolbox.register("mate", custom_crossover, num_ops=self.num_ops,
                               eligibility_map=self.factory.eligibility_map,
                               machines_per_line_map=self.factory.machines_per_line_map,
                               max_mc_cap=GA_MAX_MACHINES_PER_OPERATION,
                               op_work=self.op_work,
                               op_optype=self.factory.optype_for_op,
                               protected_machine_type=self.protected_machine_type,
                               op_sequence_map=self.seq_lookup, **mate_kwargs)
        self.toolbox.register("mutate", custom_mutate, num_ops=self.num_ops,
                               eligibility_map=self.factory.eligibility_map,
                               machines_per_line_map=self.factory.machines_per_line_map,
                               max_mc_cap=GA_MAX_MACHINES_PER_OPERATION,
                               indpb=self.mutation_indpb,
                               op_work=self.op_work,
                               op_optype=self.factory.optype_for_op,
                               protected_machine_type=self.protected_machine_type,
                               op_sequence_map=self.seq_lookup, **mutate_kwargs)
        # Tournament size reduced from 3 to 2 — softer selection pressure.
        # With a small population (default 50) against an astronomically
        # large permutation search space (num_ops! for the OS array alone),
        # tournsize=3 was greedy enough to collapse the population to
        # near-identical individuals within just a few generations, after
        # which crossover between near-clones can't produce anything new —
        # see optimize()'s random-immigrants addition for the other half of
        # this fix.
        self.toolbox.register("select", tools.selTournament, tournsize=2)
        # Serial by default; enable_parallel_evaluation() swaps in a pool map.
        self.toolbox.register("map", map)


    def _record_option_b_trace(self, event):
        # Observational only: callbacks never draw RNG or mutate an individual.
        event = dict(event)
        event['generation'] = self._option_b_generation
        event['event_id'] = len(self.option_b_events)
        self.option_b_events.append(event)

    def _compute_protected_machine_type(self):
        """Thin instance-context wrapper -- see most_congested_machine_type
        above (the actual, unit-testable logic) for the full explanation."""
        return most_congested_machine_type(self.df_machines, self.initial_machine_free)

    def _register_serial_evaluate(self):
        """Binds evaluate() to run in-process with the shared context."""
        self.toolbox.register("evaluate", evaluate_schedule, **self._eval_ctx)

    # ------------------------------------------------------------------
    # Parallel fitness evaluation
    # ------------------------------------------------------------------
    def enable_parallel_evaluation(self, processes: int = None):
        """
        Spread fitness evaluation across worker processes.

        Purely a compute change -- it does not alter the search, the results,
        or the reproducibility of a seeded run, because DEAP's map only
        distributes evaluate() calls and evaluate_schedule is a pure function
        of the chromosome plus the read-only state passed in at registration.

        Worth doing regardless of what is built next: evaluation dominates
        runtime (a 300-order window is ~200-250s, nearly all of it in
        evaluate_schedule), and that cost has already forced generation counts
        down from 50 to 25 in one sweep. Every experiment after this gets
        cheaper to run properly.

        Returns the pool so the caller can close it; use close_parallel_evaluation().
        """
        import multiprocessing

        if processes is None:
            processes = max(1, (multiprocessing.cpu_count() or 2) - 1)
        if processes <= 1:
            logger.info("Parallel evaluation not enabled (single usable core).")
            return None

        # The scheduling context goes to each worker ONCE via the initializer.
        # See the note on _WORKER_CTX above for why binding it into evaluate
        # instead made the parallel version twice as slow as serial.
        ctx = self._eval_ctx
        self._pool = multiprocessing.Pool(
            processes=processes, initializer=_init_eval_worker, initargs=(ctx,))
        self.toolbox.register("map", self._pool.map)
        # Swap evaluate for the worker-side version, which reads the resident
        # context instead of carrying it. chunksize matters: the default of 1
        # round-trips per individual and the IPC dominates.
        self.toolbox.register("evaluate", _eval_in_worker)
        logger.info(f"Parallel fitness evaluation enabled across {processes} worker process(es).")
        return self._pool

    def close_parallel_evaluation(self):
        """Tears down the worker pool, if one was started."""
        pool = getattr(self, "_pool", None)
        if pool is not None:
            pool.close()
            pool.join()
            self._pool = None
            # Restore the serial map AND the serial evaluate, so the toolbox
            # stays usable in-process afterwards.
            self.toolbox.register("map", map)
            self._register_serial_evaluate()
            logger.info("Parallel fitness evaluation pool closed.")

    def _seeded_population(self, n):
        """
        Initial population built from several construction strategies rather
        than one repeated. See ga/heuristics.py::build_population_seeds.
        """
        raw = build_population_seeds(
            self.factory, self.op_details, self.order_deadlines_minutes,
            self.seq_lookup, n
        )
        return [creator.Individual(genes) for genes in raw]

    def run_baselines(self) -> dict:
        """
        Scores the classical dispatching heuristics standalone, on the SAME
        simulation and the same metrics as the GA.

        Two uses: a sanity check that the GA is beating the obvious rules, and
        the comparison table a results chapter needs. Reusing
        evaluate_schedule(return_details=True) rather than writing a second
        metric pass means the baselines cannot silently be measured
        differently from the thing they are baselines for.
        """
        results = {}
        for name, chromosome in baseline_chromosomes(
            self.factory, self.op_details, self.order_deadlines_minutes, self.seq_lookup
        ).items():
            _fit, details = evaluate_schedule(
                chromosome, self.num_ops, self.op_details, self.gnn_predictions,
                all_line_ids=self.active_line_ids,
                order_deadlines=self.order_deadlines_minutes,
                order_release_minutes=self.order_release_minutes,
                machine_pools=self.factory.machine_ids_per_line_map,
                machine_line_map=self.machine_line_map,
                usable_machine_count=self.usable_machine_count,
                present_operator_count=self.present_operator_count,
                op_sequence_map=self.seq_lookup,
                # The baselines MUST inherit the same committed floor the GA
                # does. Omitting this scored them against an idle factory
                # while the GA faced the real carried-forward state, so in a
                # rolling run the comparison silently drifted apart: window 1
                # (both starting empty) reported the GA +7.7%, and by window 5
                # -114.8%, which is impossible given the best baseline is
                # seeded into the population and protected by elitism. The
                # margin was measuring the head start, not the algorithms.
                initial_machine_free=self.initial_machine_free,
                initial_machine_last_order=self.initial_machine_last_order,
                # Baselines must be judged on the same scored subset as the
                # GA (see scored_order_ids in fitness.py) or the margin
                # comparison silently drifts again, the same failure mode
                # documented just above for initial_machine_free.
                scored_order_ids=self.scored_order_ids,
                # Same reasoning again for the Section 0H.8 bottleneck
                # protection -- a baseline scored without it while the GA
                # gets it would be the same head-start bug in a new place.
                op_optype=self.factory.optype_for_op,
                protected_machine_type=self.protected_machine_type,
                return_details=True,
            )
            results[name] = details
            logger.info(
                f"BASELINE {name:<15} fitness {details['fitness']:>10.1f} | "
                f"makespan {details.get('makespan_days', 0):>6.1f}d | "
                f"on-time {details.get('on_time_rate', 0)*100:>5.1f}% | "
                f"mean tardiness {details.get('mean_tardiness_hours', 0):>7.1f}h | "
                f"labour {details.get('labor_utilization_rate', 0)*100:>4.1f}%"
            )
        return results

    def optimize(self, pop_size=50, generations=20, cxpb=0.7, mutpb=0.2, n_solutions=3,
                  elite_frac=0.04, immigrant_frac=0.1, random_seed=None,
                  memetic_interval=None, memetic_evals=30, memetic_replace='worst'):
        """
        Returns up to n_solutions DIVERSE candidate schedules, not just the
        single best chromosome. "Diverse" means genuinely different resource
        allocation strategies (different line assignments), not near-clones
        that happen to have close fitness scores — three copies of
        essentially the same schedule wouldn't give a human anything real to
        choose between.

        Approach: run evolution with a larger-than-1 Hall of Fame (so there's
        an actual pool of good, distinct-by-DEAP's-equality candidates to
        choose from), then greedily select n_solutions from that pool by
        fitness, skipping any candidate whose line-assignment (LA) array is
        too similar (by Hamming distance) to one already selected.

        ELITISM: DEAP's algorithms.eaSimple (used previously) has NO
        elitism by design — every generation, the entire population is
        replaced by selection + crossover + mutation, with nothing
        guaranteeing a strong individual found early survives unchanged
        into the next generation. That's why the logged `min` column could
        visibly get WORSE from one generation to the next even though
        HallOfFame quietly kept the true best-ever individual safe — a
        confusing thing to see in a convergence log or a thesis plot.
        Replaced with the standard manual elitist loop: the top
        `elite_frac` fraction of each generation (min 1) is cloned forward
        into the next generation completely unchanged, guaranteeing the
        population's best fitness is monotonically non-increasing
        (never regresses) across generations.

        RANDOM IMMIGRANTS (the other half of the elitism fix's followup):
        elitism alone fixed regression, but exposed a separate problem —
        with a small population (default 50) against an astronomically
        large permutation search space, the population can collapse to
        near-identical individuals within just a few generations under
        tournament selection, after which crossover between close relatives
        can't produce anything genuinely new and `min` stops improving
        entirely (observed: 0 improvement across 31 real generations).
        Each generation, the worst `immigrant_frac` fraction of the newly
        generated offspring (never the protected elites) gets replaced with
        brand-new, completely random individuals. This continuously injects
        fresh, unrelated genetic material — cheap, standard technique, and
        the elite/HallOfFame mechanisms already guarantee these immigrants
        can never make the final returned solution worse, only give the GA
        more raw material to potentially improve on.

        MEMETIC INTEGRATION (memetic_interval, off by default --
        reproduces the original end-only-polish behaviour exactly when
        None). Motivated by a real, measured gap: on window 1, 50
        generations of evolution alone improved the best fitness by only
        0.035% (808.08 -> 807.80), while a single end-of-run local-search
        polish pass improved it by 0.615% in the same run -- ~17.5x more
        (GA_AGENT_POSTMORTEM.md Section 2). Polishing only ONCE, on the
        final output, means that gain never gets a chance to compound: an
        improved individual is never available as a PARENT for further
        crossover/mutation. When memetic_interval is set (e.g. 5), every
        `memetic_interval` generations the current best individual is
        polished with a small evaluation budget (memetic_evals --
        deliberately much smaller than the final polish's max_evals,
        since this runs many times across one run, not once) and, if
        genuinely improved (strict-improvement only,
        ga/local_search.py's own guarantee), is injected back into the
        population per `memetic_replace` so it can be selected as a
        parent from the very next generation onward.

        memetic_replace: which population slot the improved individual
        overwrites (elites, positions [0, elite_count), are never touched
        by either policy):
          'worst' (default) -- always overwrites the single worst
            individual by fitness.
          'random' -- overwrites a uniformly random NON-elite individual
            instead.
        NEITHER policy is a proven win -- read GA_AGENT_POSTMORTEM.md
        Section 7c/7e before trusting either. 'worst' was measured
        across 3 real arms and reversed on a seed change: +0.515% (window
        1, seed 42) but -3.66% WORSE than the non-memetic baseline
        (window 1, seed 123) and +0.000% (window 2, seed 42) -- elitism's
        own never-regress guarantee held throughout (confirmed by
        `test_memetic_never_regresses_generation_to_generation`), but
        that only protects the tracked MINIMUM, not the population as a
        whole: repeatedly overwriting the worst individual with a
        near-clone of the best is hypothesized (not yet proven beyond
        this one comparison) to shrink population diversity under
        `tools.selTournament(tournsize=2)`, letting the near-clone family
        dominate selection and converge the whole population onto a
        worse basin on some seeds. 'random' exists specifically to test
        that hypothesis -- it still gives crossover/mutation the improved
        genetic material, but without deterministically removing the
        single individual most likely to be a useful diversity source.
        Do NOT assume 'random' is safer without checking
        GA_AGENT_POSTMORTEM.md for whatever it was last measured to do --
        it is a hypothesis being tested, not a fix being shipped.

        Returns: list of (chromosome, fitness_score) tuples, best first
        (length may be < n_solutions if the population didn't produce
        enough genuinely distinct good candidates), plus the logbook.
        """
        # Reproducibility. Every stochastic part of this GA draws from the
        # `random` module, so seeding it makes a run repeatable end to end.
        #
        # Needed for any CONTROLLED comparison: without it, two runs differ by
        # search noise as well as by whatever was actually changed, and the
        # noise is large — two runs of the same configuration have differed by
        # over 2 days of makespan. Left as None by default so normal operation
        # keeps its independent randomness.
        if random_seed is not None:
            random.seed(random_seed)
            np.random.seed(random_seed)
            logger.info(f"GA seeded with {random_seed} — this run is reproducible.")

        logger.info(f"Starting GA Optimization: {pop_size} pop size, {generations} generations, seeking {n_solutions} diverse solutions.")
        elite_count = max(1, int(pop_size * elite_frac))
        immigrant_count = max(1, int(pop_size * immigrant_frac))
        logger.info(f"Elitism enabled: top {elite_count} individual(s) carried forward unchanged each generation.")
        logger.info(f"Random immigrants enabled: {immigrant_count} fresh random individual(s) injected each generation.")

        pop = self.toolbox.population(n=pop_size)

        # Seed the single best pure heuristic directly into the population, so
        # the GA is guaranteed to start with at least one individual that is
        # no worse than the classical rules — and therefore cannot finish
        # worse than them either, given elitism. The jittered strategy
        # segments above explore around the rules; this pins the rule itself.
        try:
            baselines = self.run_baselines()
            best_name = min(baselines, key=lambda k: baselines[k]['fitness'])
            best_genes = baseline_chromosomes(
                self.factory, self.op_details, self.order_deadlines_minutes, self.seq_lookup
            )[best_name]
            pop[-1] = creator.Individual(best_genes)
            logger.info(
                f"Seeded best pure heuristic '{best_name}' "
                f"(fitness {baselines[best_name]['fitness']:.1f}) into the initial population."
            )
            self.baseline_results = baselines
        except Exception as e:
            # SILENT-DEFAULT FIX (audit #5b). This used to warn and continue
            # with baseline_results = {}, which quietly dropped BOTH the
            # seeded best-heuristic individual AND the baseline figures every
            # GA-vs-baseline comparison in the reports is computed from -- so
            # a genuine bug here surfaced only as a slightly worse GA and a
            # missing comparison table, neither of which looks like a failure.
            # Baseline results are load-bearing for reporting, so this is now
            # fatal rather than cosmetic.
            raise RuntimeError(
                f"Heuristic seeding failed ({e}). The best-of-5 baseline is both a "
                f"population seed and the reference every reported GA margin is "
                f"measured against, so continuing without it would silently produce "
                f"an unseeded run with no comparison figures."
            ) from e
        # Keep a bigger pool than n_solutions so there's room to filter for
        # diversity afterward — HallOfFame alone only guarantees "best by
        # fitness", not "meaningfully different from each other".
        hof_size = max(n_solutions * 5, 20)
        hof = tools.HallOfFame(hof_size)
        stats = tools.Statistics(lambda ind: ind.fitness.values)
        stats.register("avg", np.mean)
        stats.register("min", np.min)

        logbook = tools.Logbook()
        logbook.header = "gen", "nevals", "avg", "min"

        # Initial evaluation (generation 0) — same as eaSimple's setup step.
        invalid_ind = [ind for ind in pop if not ind.fitness.valid]
        fitnesses = self.toolbox.map(self.toolbox.evaluate, invalid_ind)
        for ind, fit in zip(invalid_ind, fitnesses):
            ind.fitness.values = fit
        hof.update(pop)
        record = stats.compile(pop)
        logbook.record(gen=0, nevals=len(invalid_ind), **record)
        logger.info(logbook.stream)

        memetic_polishes = 0
        memetic_improved = 0
        for gen in range(1, generations + 1):
            n_evals = self._advance_generation(pop, elite_count, immigrant_count, cxpb, mutpb)

            if memetic_interval and gen % memetic_interval == 0:
                best_ind = tools.selBest(pop, 1)[0]
                polished_genes, polished_fit, _pstats = local_search_polish(
                    list(best_ind), self.num_ops, self.factory, self.op_work,
                    self.toolbox.evaluate, max_evals=memetic_evals, rng=random, log_fn=None)
                memetic_polishes += 1
                n_evals += _pstats.get('evaluations', 0)
                if polished_fit < best_ind.fitness.values[0] - 1e-9:
                    memetic_improved += 1
                    polished_ind = creator.Individual(polished_genes)
                    polished_ind.fitness.values = (polished_fit,)
                    # Elites occupy pop[0:elite_count] after _advance_generation
                    # (see that method) -- both policies restrict their target
                    # to the non-elite range explicitly, rather than relying on
                    # 'worst' never coincidentally landing on an elite.
                    if memetic_replace == 'random':
                        target_idx = random.randrange(elite_count, len(pop))
                    else:  # 'worst'
                        target_idx = max(range(elite_count, len(pop)), key=lambda i: pop[i].fitness.values[0])
                    pop[target_idx] = polished_ind
                    logger.info(
                        f"  [memetic] gen {gen}: best {best_ind.fitness.values[0]:.2f} -> "
                        f"{polished_fit:.2f}, injected via '{memetic_replace}' policy "
                        f"as a parent for future generations."
                    )

            hof.update(pop)

            record = stats.compile(pop)
            logbook.record(gen=gen, nevals=n_evals, **record)
            logger.info(logbook.stream)

        if memetic_interval:
            logger.info(
                f"Memetic integration: {memetic_polishes} polish call(s) every "
                f"{memetic_interval} generation(s), {memetic_improved} genuinely improved "
                f"and injected back into the population."
            )

        selected = self._select_diverse_solutions(hof, n_solutions)
        self.last_evolution_state = __import__('ga.checkpointing', fromlist=['encode_evolution']).encode_evolution(pop, hof, selected[0][0], logbook, generations, sum(int(x.get('nevals', 0)) for x in logbook))
        logger.info(
            f"GA Optimization Complete! Selected {len(selected)} diverse solution(s), "
            f"best fitness: {selected[0][1]:.2f}" if selected else "GA Optimization Complete! No valid solutions found."
        )
        return selected, logbook

    def optimize_islands(self, pop_size=50, generations=50, cxpb=0.7, mutpb=0.2,
                          n_solutions=3, n_islands=5, migration_interval=8,
                          migrants_per_island=1, elite_frac=0.04, immigrant_frac=0.1,
                          random_seed=None):
        """
        Island-model GA: several sub-populations evolving semi-independently,
        exchanging their best individuals periodically.

        WHY: the single-population search beats plain dispatching rules by
        7.3-8.1% after the environment fixes (per-(line,type) load tracking
        and removing the GNN's blanket duration padding). Before those fixes
        it was ~3%. The remaining question is whether what is left is an
        OPERATOR weakness — crossover and mutation not exploring effectively —
        or simply a small residual. An island model attacks premature
        convergence specifically: sub-populations drift to different regions,
        and migration recombines them, which a single panmictic population
        cannot do once it has collapsed onto one basin.

        FAIRNESS. This must be compared against optimize() on equal terms or
        the result is meaningless:
          * same TOTAL population, split across islands, not n_islands x
            pop_size — otherwise the islands simply get more compute;
          * same total generations;
          * same initial individuals, built once and dealt out, so both
            searches start from the identical set;
          * the same _advance_generation step, shared rather than duplicated.
        The only difference is the population STRUCTURE and migration.

        Migration is a ring: island i sends copies of its best to island i+1,
        replacing that island's worst. Copies, not moves — an island never
        loses its own best to a neighbour, which would make migration a
        destructive operation rather than an enriching one.

        Sequential by design. Islands are naturally parallel, but parallel
        evaluation measured at only 1.05x on this hardware and has already
        produced one false reproducibility alarm; establishing whether
        migration helps at all comes first.
        """
        if random_seed is not None:
            random.seed(random_seed)
            np.random.seed(random_seed)
            logger.info(f"Island GA seeded with {random_seed} — this run is reproducible.")

        n_islands = max(2, int(n_islands))
        island_size = max(4, pop_size // n_islands)
        elite_count = max(1, int(island_size * elite_frac))
        immigrant_count = max(1, int(island_size * immigrant_frac))

        logger.info(
            f"Island GA: {n_islands} islands x {island_size} individuals "
            f"({n_islands * island_size} total vs {pop_size} single-population), "
            f"{generations} generations, migrating {migrants_per_island} individual(s) "
            f"per island every {migration_interval} generations."
        )

        # Build the whole population once, then deal it out, so the islands
        # collectively start from the same material a single population would.
        full_pop = self.toolbox.population(n=n_islands * island_size)
        try:
            baselines = self.run_baselines()
            best_name = min(baselines, key=lambda k: baselines[k]['fitness'])
            best_genes = baseline_chromosomes(
                self.factory, self.op_details, self.order_deadlines_minutes, self.seq_lookup
            )[best_name]
            full_pop[-1] = creator.Individual(best_genes)
            self.baseline_results = baselines
            logger.info(f"Seeded best pure heuristic '{best_name}' into the island population.")
        except Exception as e:
            # Same reasoning as the single-population path above (audit #5b).
            raise RuntimeError(
                f"Heuristic seeding failed in the island run ({e}); baseline results "
                f"are required for the reported comparison."
            ) from e

        islands = [full_pop[i * island_size:(i + 1) * island_size] for i in range(n_islands)]

        hof = tools.HallOfFame(max(n_solutions * 5, 20))
        stats = tools.Statistics(lambda ind: ind.fitness.values)
        stats.register("avg", np.mean)
        stats.register("min", np.min)
        logbook = tools.Logbook()
        logbook.header = "gen", "nevals", "avg", "min"

        for island in islands:
            invalid = [ind for ind in island if not ind.fitness.valid]
            for ind, fit in zip(invalid, self.toolbox.map(self.toolbox.evaluate, invalid)):
                ind.fitness.values = fit
            hof.update(island)
        everyone = [ind for island in islands for ind in island]
        logbook.record(gen=0, nevals=len(everyone), **stats.compile(everyone))
        logger.info(logbook.stream)

        migrations = 0
        for gen in range(1, generations + 1):
            n_evals = 0
            for island in islands:
                n_evals += self._advance_generation(
                    island, elite_count, immigrant_count, cxpb, mutpb)

            if migration_interval and gen % migration_interval == 0 and n_islands > 1:
                self._migrate(islands, migrants_per_island)
                migrations += 1

            everyone = [ind for island in islands for ind in island]
            for island in islands:
                hof.update(island)
            logbook.record(gen=gen, nevals=n_evals, **stats.compile(everyone))
            logger.info(logbook.stream)

        logger.info(f"Island GA complete: {migrations} migration event(s).")
        selected = self._select_diverse_solutions(hof, n_solutions)
        return selected, logbook

    def polish(self, chromosome, max_evals=250, random_seed=None):
        """
        Local-search polishing pass over a finished schedule.

        Applied AFTER the GA, not inside it, because it is a different kind of
        operator: the GA explores by large random recombination, this exploits
        by small targeted moves aimed at the pool setting the schedule's
        length. Measured, the GA's own operators contribute ~0.4% over the
        initial population, so this is where improvement is expected to come
        from. See ga/local_search.py.
        """
        rng = random.Random(random_seed) if random_seed is not None else random
        best, fit, stats = local_search_polish(
            list(chromosome), self.num_ops, self.factory, self.op_work,
            self.toolbox.evaluate, max_evals=max_evals, rng=rng,
            log_fn=logger.info,
        )
        polished = creator.Individual(best)
        polished.fitness.values = (fit,)
        return polished, stats

    def alns_refine(self, chromosome, max_evals=400, removal_frac=0.035, random_seed=None,
                     repair_score_weights=None):
        """
        Sequential ALNS refinement pass -- ALNS_REFINEMENT_PROPOSAL.md.
        Alternative to polish() for the end-of-run refinement stage. NOT
        wired into optimize() -- opt-in only, same pattern as polish()
        itself and memetic_interval. See ga/alns.py for the destroy/
        repair/acceptance/adaptive-weighting design.

        repair_score_weights: optional (w_load, w_risk, w_impact) tuple --
        opt-in richer repair-line scoring (ALNS_REPAIR_SCORING_PROPOSAL.md
        Section 13). None (default): unchanged plain least-loaded-line repair.
        """
        rng = random.Random(random_seed) if random_seed is not None else random
        best, fit, stats = alns_refine_search(
            list(chromosome), self.num_ops, self.factory, self.op_work,
            self.toolbox.evaluate, self.gnn_predictions,
            max_evals=max_evals, removal_frac=removal_frac, rng=rng,
            log_fn=logger.info, repair_score_weights=repair_score_weights,
        )
        refined = creator.Individual(best)
        refined.fitness.values = (fit,)
        return refined, stats

    @staticmethod
    def _migrate(islands, migrants_per_island):
        """
        Ring migration: island i's best individuals are COPIED into island
        i+1, displacing that island's worst.

        Copies rather than moves, and computed from a snapshot taken before
        any island is modified — migrating in place would let an individual
        that arrived this round immediately hop onward, which is a cascade
        rather than a migration.
        """
        n = len(islands)
        outgoing = [
            [ind for ind in tools.selBest(islands[i], migrants_per_island)]
            for i in range(n)
        ]
        for i in range(n):
            target = islands[(i + 1) % n]
            worst = tools.selWorst(target, migrants_per_island)
            for victim, migrant in zip(worst, outgoing[i]):
                target[target.index(victim)] = migrant

    def _advance_generation(self, pop, elite_count, immigrant_count, cxpb, mutpb):
        """
        One generation, in place: elitism -> selection -> crossover/mutation
        -> evaluation -> random immigrants.

        Factored out so the single-population and island searches run the
        IDENTICAL generation step. Two copies of this loop would drift, and a
        comparison between the two search structures would then be measuring
        the drift as well as the structure.

        Returns the number of evaluations performed.
        """
        target_size = len(pop)
        self._option_b_generation += 1
        if self.option_b_logging:
            self.option_b_events.append({'operator':'elite','generation':self._option_b_generation,'chromosomes':[list(x) for x in tools.selBest(pop, elite_count)]})

        # Elites: exact clones of the current top performers, untouched by
        # crossover/mutation this generation, so the best can never regress.
        elites = [self.toolbox.clone(ind) for ind in tools.selBest(pop, elite_count)]

        offspring = self.toolbox.select(pop, target_size - elite_count)
        offspring = [self.toolbox.clone(ind) for ind in offspring]
        offspring = algorithms.varAnd(offspring, self.toolbox, cxpb, mutpb)

        invalid_ind = [ind for ind in offspring if not ind.fitness.valid]
        fitnesses = self.toolbox.map(self.toolbox.evaluate, invalid_ind)
        for ind, fit in zip(invalid_ind, fitnesses):
            ind.fitness.values = fit
        n_evals = len(invalid_ind)

        # RANDOM IMMIGRANTS: replace the worst offspring (never the elites,
        # already excluded from this list) with fresh unguided individuals.
        # Deliberately from toolbox.individual rather than the seeded strategy
        # mix — rebuilding them from heuristics the population already
        # contains defeats the purpose, and costs ~8s per call at full scale.
        n_immigrants = min(immigrant_count, len(offspring))
        if n_immigrants > 0:
            offspring.sort(key=lambda ind: ind.fitness.values[0], reverse=True)  # worst first
            immigrants = [self.toolbox.individual() for _ in range(n_immigrants)]
            imm_fitnesses = self.toolbox.map(self.toolbox.evaluate, immigrants)
            for ind, fit in zip(immigrants, imm_fitnesses):
                ind.fitness.values = fit
            if self.option_b_logging:
                self.option_b_events.append({'operator':'immigrant','generation':self._option_b_generation,'chromosomes':[list(x) for x in immigrants]})
            offspring[:n_immigrants] = immigrants
            n_evals += n_immigrants

        pop[:] = elites + offspring
        return n_evals

    def _select_diverse_solutions(self, hof, n_solutions, min_diversity_frac=0.15):
        """
        Greedily picks up to n_solutions from the Hall of Fame pool (already
        sorted best-first by DEAP), skipping candidates whose LA array
        overlaps too much (Hamming similarity) with an already-selected one.
        min_diversity_frac = minimum fraction of LA positions that must
        differ from every already-picked solution.
        """
        selected = []
        for ind in hof:
            la = ind[self.num_ops:2 * self.num_ops]
            is_diverse = True
            for chosen, _ in selected:
                chosen_la = chosen[self.num_ops:2 * self.num_ops]
                diff_frac = sum(1 for a, b in zip(la, chosen_la) if a != b) / max(1, self.num_ops)
                if diff_frac < min_diversity_frac:
                    is_diverse = False
                    break
            if is_diverse:
                selected.append((ind, ind.fitness.values[0]))
            if len(selected) >= n_solutions:
                break

        # Fallback: if diversity filtering was too strict and left us with
        # fewer than requested (e.g. a small problem with limited eligible
        # lines), just fill the remainder with the next-best candidates
        # regardless of similarity — a slightly-similar second option still
        # beats returning only one.
        if len(selected) < n_solutions:
            chosen_ids = {id(ind) for ind, _ in selected}
            for ind in hof:
                if id(ind) not in chosen_ids:
                    selected.append((ind, ind.fitness.values[0]))
                    chosen_ids.add(id(ind))
                if len(selected) >= n_solutions:
                    break

        return selected

if __name__ == "__main__":
    from database.queries import fetch_active_orders_and_routings, fetch_machine_state
    
    print("Testing Line-Based GA Scheduler with Manager Directives...")
    routings = fetch_active_orders_and_routings()
    machines = fetch_machine_state()
    mock_lines = pd.DataFrame({'line_id': [1, 2, 3], 'is_active': [1, 1, 1]})
    
    small_routings = routings.head(20)
    mock_gnn = {
        make_key(row['production_order_id'], row['operation_id']): {'delay': random.uniform(0.0, 0.5), 'priority': random.uniform(0.5, 1.0)}
        for _, row in small_routings.iterrows()
    }
    
    # Simulate frontend manager selection
    sample_order_ids = small_routings['production_order_id'].astype(str).unique().tolist()
    frontend_manager_picks = [sample_order_ids[0]] if sample_order_ids else []
    print(f"Manager explicitly prioritized orders from UI: {frontend_manager_picks}")
    
    test_time = datetime(2026, 7, 27, 9, 19, 23)
    scheduler = GAScheduler(
        df_routings=small_routings, 
        df_machines=machines, 
        df_lines=mock_lines, 
        gnn_predictions=mock_gnn, 
        current_time=test_time,
        manager_prioritized_orders=frontend_manager_picks
    )
    
    solutions, log = scheduler.optimize(pop_size=50, generations=100, n_solutions=3)
    print(f"\n[OK] Optimization Complete! Found {len(solutions)} diverse solution(s):")
    for i, (ind, score) in enumerate(solutions, 1):
        print(f"  Option {i}: Fitness Score: {score:.2f} (lower is better)")
