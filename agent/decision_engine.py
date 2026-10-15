import os
import sys
import torch
import json
from datetime import datetime, timedelta
import pandas as pd
from sqlalchemy import text

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.logger import get_logger
from utils.run_artifacts import current_run_id, stamp_artifact
from utils.config import (
    OUTPUT_DIR, SHIFT_START_HOUR, SHIFT_END_HOUR, GNN_HIDDEN_CHANNELS,
    ORDER_QUOTE_CUSHION_DAYS, PLANNING_HORIZON_ORDERS, DEVICE,
    GA_POPULATION_SIZE, GA_GENERATIONS,
)
from database.db import get_db_session, get_engine
from database.queries import (
    fetch_present_operator_count,
    fetch_concurrent_operator_capacity,
    fetch_assignable_operators,
    fetch_line_factory_map,
    fetch_committed_machine_state,
    fetch_committed_operator_reservations,
    apply_planning_horizon,
)
from graph.graph_builder import FactoryGraphBuilder
from models.gnn import MultiTaskFactoryGNN, prior_corrected_delay_probabilities
from ga.scheduler import GAScheduler
from ga.schedule_utils import (
    find_slot_and_insert,
    snap_to_shift_window,
    select_machines_and_start,
    calculate_operation_work,
    split_operation_work,
    apply_risk_buffer,
)
from ga.fitness import CHANGEOVER_MINUTES
from utils.machine_taxonomy import machine_type_to_operation_type
from utils.shift_rotation import batch_available_at, SHIFT_BANDS, shift_name_for
from utils.work_calendar import can_cover_work, completion_after_productive_minutes
from ga.worker_allocation import operator_available_at

logger = get_logger("ai_agent", "agent.log")

# EFFICIENCY-AWARE CREW SELECTION (Batch-4 fix 6d).
#
# How far selection may lean toward skill instead of an even spread of work.
# Stated here, as named policy constants with their reasoning, rather than as
# bare numbers inside a sort lambda.
#
# Synthetic selection-policy weights, not fitted from the skill distribution.
# Load is divided by the maximum candidate load (not min-max normalized).
# At priority 1, W=0.60: the busiest/best-skilled worker can outrank an
# idle/least-skilled worker indefinitely. This is a tradeoff, NOT a fairness
# guarantee. Eligibility and interval reservations remain hard constraints.
EFFICIENCY_WEIGHT_BASE = 0.30
EFFICIENCY_WEIGHT_MAX = 0.60

class AIDecisionEngine:
    # How a worker must cover an operation, for eligibility purposes.
    #
    #   'segmented'  (default, correct) -- the model the training data was
    #       generated under: work pauses at off-duty boundaries and THE SAME
    #       worker resumes it (data_generation/simulation_calendar.py, via
    #       utils/work_calendar.py). Length alone never disqualifies a worker.
    #
    #   'continuous' (superseded) -- one worker must be on duty for the whole
    #       wall-clock span. Shifts are three 8h bands tiling the day, so this
    #       rejected every operation longer than 8h unconditionally: 0 of 776
    #       such operations were staffable, 3,441,832 of 6,082,368 unfilled
    #       worker-minutes. Retained ONLY so the staffing measurement can
    #       attribute its own correction between this and the stale-reservation
    #       defect. Not a supported production setting.
    WORKER_CONTINUITY_POLICY = 'segmented'

    def __init__(self, *, run_id=None):
        self.run_id = run_id or current_run_id()
        logger.info("Initializing Line-Based AI Decision Engine...")
        self.model_path = os.path.join(OUTPUT_DIR, "checkpoints", "gnn_multitask_latest.pth")
        # Manager overrides sit ABOVE the fuzzy triage layer (see
        # agent/fuzzy_logic.py) — once set, an order stays top-priority for
        # every future rescheduling/impact-assessment pass until explicitly
        # cleared, not just for the one call that set it. In-memory only for
        # now (process lifetime); persist to a DB table if this needs to
        # survive a restart.
        self.manager_forced_orders = set()

    def set_manager_priority(self, order_id: str):
        order_id = str(order_id)
        self.manager_forced_orders.add(order_id)
        logger.info(f"📌 Manager override: Order {order_id} marked top-priority (bypasses fuzzy triage from now on).")

    def clear_manager_priority(self, order_id: str):
        order_id = str(order_id)
        self.manager_forced_orders.discard(order_id)
        logger.info(f"Manager override cleared for Order {order_id}.")

    def _require_checkpoint(self):
        if not os.path.isfile(self.model_path):
            raise FileNotFoundError(
                f"Required GNN checkpoint is missing: {self.model_path}. "
                "Rescheduling aborted; untrained weights are not permitted."
            )

    def _load_trained_model(self, metadata):
        """Load a compatible checkpoint or abort; inference always uses eval mode."""
        self._require_checkpoint()
        try:
            from graph.feature_schema import load_preprocessing
            preprocessing = load_preprocessing(self.model_path)
            model = MultiTaskFactoryGNN(
                metadata=metadata, hidden_channels=GNN_HIDDEN_CHANNELS,
                operation_in_dim=preprocessing['operation_schema']['count']
            ).to(torch.device(DEVICE))
            model.load_state_dict(
                torch.load(self.model_path, map_location=torch.device(DEVICE)))
        except Exception as exc:
            # The reason is part of the message, not just __cause__. This
            # handler previously flattened every distinct failure -- missing
            # preprocessing artifact, schema mismatch, corrupt weights --
            # into one generic sentence, so an operator saw "rescheduling
            # aborted" with no indication of which of those it was or what
            # to do about it.
            raise RuntimeError(
                f"Failed to load GNN checkpoint {self.model_path}; "
                f"rescheduling aborted. Reason: {type(exc).__name__}: {exc}"
            ) from exc
        model.eval()
        return model

    def _fetch_production_lines(self) -> pd.DataFrame:
        """Fetches active production lines from the database."""
        logger.info("Fetching production lines...")
        query = "SELECT line_id, department_id, line_number, target_efficiency_percentage, is_active FROM Production_Lines WHERE is_active = 1;"
        return pd.read_sql(query, get_engine())

    def trigger_rescheduling(self, manager_prioritized_orders: list = None, n_solutions: int = 3):
        """
        The master pipeline. Rebuilds the graph, runs the GNN, runs the line-based GA 
        with optional frontend manager directives, and exports the upgraded JSON 
        schedule format for the frontend dashboard.

        n_solutions: how many genuinely distinct candidate schedules to
        surface (not just the single GA winner) — see
        ga/scheduler.py::_select_diverse_solutions. Each gets its own
        proposed_schedule_option_N.json / order_completions_option_N.json;
        the top-ranked one is ALSO written to the original unsuffixed
        filenames for backward compatibility with anything already reading
        proposed_schedule.json directly. A schedule_options_summary.json
        compares all N on the metrics that actually differ between them and
        states which one is recommended and why — a single silently-chosen
        "best" schedule doesn't give a human anything to weigh in on.
        """
        logger.info("--- STARTING LINE-BASED AI RESCHEDULING CYCLE ---")
        self._require_checkpoint()  # Fail before any graph/database work.
        # One origin for releases, deadlines and both committed-resource
        # snapshots. Re-reading datetime.now() after graph inference would
        # describe a different state from the schedule the cycle started on.
        cycle_origin = datetime.now()
        
        # Persistent manager overrides always apply, on top of whatever this
        # specific call passed in.
        combined_priorities = set(str(x) for x in (manager_prioritized_orders or []))
        combined_priorities |= self.manager_forced_orders
        manager_prioritized_orders = list(combined_priorities) if combined_priorities else None
        
        # 1. Build Current Graph
        builder = FactoryGraphBuilder()
        from graph.feature_schema import load_preprocessing
        preprocessing = load_preprocessing(self.model_path)
        hetero_graph, _ = builder.build_graph(preprocessing=preprocessing)
        # Device placement (audit #8) -- DEVICE was defined and never read,
        # so every inference pass ran on CPU regardless of hardware.
        device = torch.device(DEVICE)
        hetero_graph = hetero_graph.to(device)
        
        # Fetch production lines for the GA
        df_lines = self._fetch_production_lines()

        # ROLLING HORIZON: plan the earliest-released PLANNING_HORIZON_ORDERS
        # orders rather than the entire book. Measured: a 300-order window
        # gives 41.9 orders/day against 12.0 for the full book, landing within
        # ~23% of the factory's physical capacity floor where a single
        # full-book pass is 4x off it. See utils/config.py for the sweep.
        #
        # The GRAPH is still built over everything — the GNN's view of the
        # factory should not be truncated by a planning decision — only the
        # routings handed to the GA are windowed.
        scheduling_routings = apply_planning_horizon(
            builder.df_routings, PLANNING_HORIZON_ORDERS
        )
        
        # 2. Load Trained GNN Model
        logger.info("Loading trained GNN weights...")
        # Was hardcoded to 64 — harmless only by coincidence (config default
        # is also 64). Using the config value means this can never silently
        # mismatch the architecture the checkpoint was actually trained with.
        model = self._load_trained_model(hetero_graph.metadata())

        # 3. GNN Inference
        logger.info("Running GNN Inference to predict Delays and Priorities...")
        with torch.no_grad():
            predictions = model(hetero_graph.x_dict, hetero_graph.edge_index_dict,
                                 hetero_graph['operation'].type_idx)
        
        # Map predictions back to operation keys for the GA
        # BUG FIX: predictions['delay'] are RAW LOGITS (the model is trained
        # with BCEWithLogitsLoss, which expects unbounded raw scores, not
        # probabilities) — this used to store the raw logit directly with no
        # sigmoid applied. Downstream, delay_prob was computed as
        # min(max(raw_logit, 0.0), 1.0), which just CLAMPS an unbounded
        # number into [0,1] instead of converting it. Any logit >= 1.0 (a
        # perfectly ordinary confident prediction — sigmoid(1.0) ≈ 0.73)
        # got hard-capped and displayed as exactly 1.00, permanently
        # erasing the real difference between "73% confident" and "99.99%
        # confident". models/evaluate.py already did this correctly
        # (torch.sigmoid(...) before computing metrics) — this is why eval
        # metrics looked sane while every escalation log line still showed
        # a flat, saturated (1.00). Fixed at the source here so every
        # downstream use (buffer sizing, escalation threshold, XAI text)
        # gets the real probability.
        delay_probs = prior_corrected_delay_probabilities(
            predictions['delay'], preprocessing['delay_pos_weight']
        ).squeeze(-1)
        op_keys = list(builder.op_map.keys())
        gnn_preds = {}
        for i, key in enumerate(op_keys):
            gnn_preds[key] = {
                'delay': delay_probs[i].item(),
                'priority': predictions['priority'][i].item()
            }

        # 4. Run Line-Based Genetic Algorithm (passing frontend manager picks)
        logger.info("Handing line constraints, GNN predictions, and manager directives to the GA Optimizer...")
        # BUG FIX (PIPELINE_METHODOLOGY_TRACE.md Stage 10 / FULL_CODEBASE_AUDIT.md):
        # was hardcoded here, a second independent copy of the same two
        # numbers run_final_pipeline.py separately hardcoded -- utils/config.py
        # is now the single source of truth for both call sites.
        pop_size = GA_POPULATION_SIZE
        generations = GA_GENERATIONS
        
        # Concurrent capacity, not the whole-day roster — only one of the three
        # shifts is on the floor at a time. See
        # database/queries.py::fetch_concurrent_operator_capacity.
        present_operator_count = fetch_concurrent_operator_capacity()
        operator_index = self._build_operator_index(
            fetch_assignable_operators(), df_lines, fetch_line_factory_map()
        )
        committed_machine_free, committed_machine_last_order = (
            fetch_committed_machine_state(cycle_origin)
        )
        committed_operator_reservations = (
            fetch_committed_operator_reservations(cycle_origin)
        )

        scheduler = GAScheduler(
            ownership_snapshot=builder.ownership_snapshot,
            df_routings=scheduling_routings,
            df_machines=builder.df_machines,
            df_lines=df_lines,
            gnn_predictions=gnn_preds,
            manager_prioritized_orders=manager_prioritized_orders,
            present_operator_count=present_operator_count,
            current_time=cycle_origin,
            initial_machine_free=committed_machine_free,
            initial_machine_last_order=committed_machine_last_order,
            operator_index=operator_index,
            initial_operator_reservations=committed_operator_reservations,
        )
        
        solutions, logbook = scheduler.optimize(pop_size=pop_size, generations=generations, n_solutions=n_solutions)
        if not solutions:
            logger.error("GA optimization produced no valid solutions — aborting rescheduling cycle.")
            return

        # 5. Export EVERY candidate solution, not just the winner, and
        # collect comparable metrics for each.
        option_summaries = []
        for i, (chromosome, fitness) in enumerate(solutions, start=1):
            label = None if i == 1 else f"option_{i}"  # option 1 also gets the unsuffixed default filenames
            summary = self._export_proposed_schedule(
                chromosome, scheduler.num_ops, scheduler.op_details, gnn_preds,
                df_lines, scheduler.factory.eligibility_map, scheduler.order_deadline_abs,
                scheduling_routings, builder.df_machines, label=label,
                machine_pools=scheduler.factory.machine_ids_per_line_map,
                operator_index=operator_index,
                present_operator_count=present_operator_count,
                usable_machine_count=scheduler.usable_machine_count,
                op_optype=scheduler.factory.optype_for_op,
                protected_machine_type=scheduler.protected_machine_type,
                initial_machine_free=committed_machine_free,
                initial_machine_last_order=committed_machine_last_order,
                initial_operator_reservations=committed_operator_reservations, base_time=scheduler.now,
            )
            summary['option_number'] = i
            summary['fitness_score'] = round(fitness, 2)
            option_summaries.append(summary)

        self._export_options_comparison(option_summaries)
        self._log_optimization_metrics(solutions[0][1], logbook, pop_size, generations)
        
        logger.info("--- LINE-BASED AI RESCHEDULING CYCLE COMPLETE ---")

    def _export_options_comparison(self, option_summaries: list):
        """
        Compares the N candidate schedules on the metrics that actually
        differ between them and writes a plain-language recommendation —
        e.g. "Option 1 has the best overall fitness, but Option 2 has
        meaningfully better load balance; recommend Option 1 unless line
        utilization fairness is the priority this cycle." A single silently-
        chosen schedule gives a human nothing to weigh in on; this does.
        """
        by_lateness = max(option_summaries, key=lambda s: s['on_time_rate'])
        by_labour = max(option_summaries, key=lambda s: s['labor_utilization_rate'])
        by_balance = min(option_summaries, key=lambda s: s['load_balance_stdev_minutes'])
        by_makespan = min(option_summaries, key=lambda s: s['makespan_minutes'])
        by_fitness = min(option_summaries, key=lambda s: s['fitness_score'])

        lines = [f"Generated {len(option_summaries)} candidate schedule(s):"]
        for s in option_summaries:
            lines.append(
                f"  Option {s['option_number']} ({s['label']}): fitness={s['fitness_score']:.2f} | "
                f"on-time {s['on_time_rate']*100:.1f}% "
                f"({s['orders_made_late_by_this_schedule']} of {s['orders_with_deadline']} made late; "
                f"{s['orders_already_past_deadline']} already overdue on arrival) | "
                f"mean schedule-added tardiness {s['mean_tardiness_hours_of_late_orders']:.1f}h, "
                f"max {s['max_tardiness_hours']:.1f}h | "
                f"labour {s['labor_utilization_rate']*100:.1f}% used "
                f"({s['labor_utilization_vs_ceiling']*100:.1f}% of the "
                f"{s['labor_utilization_ceiling']*100:.1f}% achievable ceiling; "
                f"{s['idle_employee_minutes']/60:,.0f} idle operator-hours) | "
                f"load-balance stdev {s['load_balance_stdev_minutes']:.1f}min | "
                f"makespan {s['makespan_minutes']/1440:.1f}d | "
                f"{s['high_risk_reroutes']} high-risk reroute(s)."
            )

        recommendation = f"Recommended: Option {by_fitness['option_number']} (best overall fitness score)."
        if by_lateness['option_number'] != by_fitness['option_number']:
            recommendation += (
                f" Note: Option {by_lateness['option_number']} delivers a better on-time rate "
                f"({by_lateness['on_time_rate']*100:.1f}% vs {by_fitness['on_time_rate']*100:.1f}%) "
                f"if meeting customer dates matters more than the combined score this cycle."
            )
        if by_labour['option_number'] != by_fitness['option_number']:
            recommendation += (
                f" Option {by_labour['option_number']} uses labour harder "
                f"({by_labour['labor_utilization_rate']*100:.1f}% vs "
                f"{by_fitness['labor_utilization_rate']*100:.1f}%, a difference of "
                f"{(by_fitness['idle_employee_minutes'] - by_labour['idle_employee_minutes'])/60:,.0f} "
                f"idle operator-hours) if workforce utilisation is the priority."
            )
        if by_balance['option_number'] not in (by_fitness['option_number'], by_lateness['option_number'], by_labour['option_number']):
            recommendation += (
                f" Option {by_balance['option_number']} has the most evenly-loaded lines "
                f"(stdev {by_balance['load_balance_stdev_minutes']:.1f}min) if capacity fairness across "
                f"lines is the priority."
            )
        lines.append(recommendation)

        summary_text = "\n".join(lines)
        logger.info(summary_text)

        output_path = os.path.join(OUTPUT_DIR, "schedule_options_summary.json")
        with open(output_path, "w") as f:
            json.dump({
                'options': option_summaries,
                'recommended_option': by_fitness['option_number'],
                'recommendation_text': recommendation,
            }, f, indent=4, default=str)
        logger.info(f"✅ Multi-option comparison saved to {output_path}.")

    @staticmethod
    def _build_operator_index(df_operators, df_lines, line_factory_map=None):
        """
        Indexes assignable operators for fast per-operation lookup, in the
        three tiers this factory actually staffs by.

        Mirrors the tiering the data generator itself uses
        (data_generation/seed_database_fixed.py): an operator belongs to a
        batch, a batch belongs to a line, and work goes to a member of THAT
        line's batch who holds the required skill. Only when the line's own
        batch has nobody suitable does it widen to the same factory, and only
        then to anyone in the company.

        Returns a dict of three lookups keyed by canonical operation_type.
        """
        by_line = {}     # (line_id, optype) -> [employee_id]
        by_factory = {}  # (factory_id, optype) -> [employee_id]
        by_skill = {}    # optype -> [employee_id]
        shift_of = {}    # floater employee_id -> (start_hour_float, end_hour_float)
        crew_of = {}     # crew employee_id -> (rotation_offset, rest_day_offset)
        # (employee_id, optype) -> efficiency_rating (Batch-4 fix 6d).
        # fetch_assignable_operators() has always SELECTed
        # osm.efficiency_rating, and this function never read it into any
        # structure -- fetched from the database on every cycle and dropped
        # on the floor. Retained now so selection can prefer a more capable
        # operator among candidates that are ALREADY eligible.
        efficiency_of = {}

        if df_operators is None or df_operators.empty:
            return {'by_line': by_line, 'by_factory': by_factory, 'by_skill': by_skill,
                    'line_factory': dict(line_factory_map or {}),
                    'shift_of': shift_of, 'crew_of': crew_of,
                    'efficiency_of': efficiency_of}

        df = df_operators.copy()
        # Not named with a leading underscore: DataFrame.itertuples() renames
        # any such column to a positional placeholder (_5, _6, ...), so
        # row._optype would raise AttributeError.
        df['optype_norm'] = df['operation_type'].astype(str).str.strip().str.lower()

        def _as_hours(v):
            """MySQL TIME comes back as timedelta; normalise to hours-since-midnight."""
            if v is None or (isinstance(v, float) and pd.isna(v)):
                return None
            if hasattr(v, 'total_seconds'):
                return (v.total_seconds() / 3600.0) % 24
            if hasattr(v, 'hour'):
                return v.hour + v.minute / 60.0
            return None

        for row in df.itertuples(index=False):
            optype = row.optype_norm
            emp = row.employee_id
            # A crew member's availability comes from the batch rotation; a
            # floater's from Work_Schedules. Recorded in separate maps so the
            # eligibility check cannot accidentally consult the wrong one —
            # a crew member's static Work_Schedules row is stale by design.
            rot = getattr(row, 'rotation_offset', None)
            rest = getattr(row, 'rest_day_offset', None)
            if rot is not None and not pd.isna(rot) and rest is not None and not pd.isna(rest):
                crew_of.setdefault(emp, (int(rot), int(rest)))
            elif emp not in shift_of:
                s = _as_hours(getattr(row, 'shift_start', None))
                e = _as_hours(getattr(row, 'shift_end', None))
                if s is not None and e is not None:
                    shift_of[emp] = (s, e)
            eff = getattr(row, 'efficiency_rating', None)
            if eff is not None and not pd.isna(eff):
                efficiency_of[(emp, optype)] = float(eff)
            by_skill.setdefault(optype, []).append(emp)
            if row.factory_id is not None and not pd.isna(row.factory_id):
                by_factory.setdefault((int(row.factory_id), optype), []).append(emp)
            if row.line_id is not None and not pd.isna(row.line_id):
                by_line.setdefault((int(row.line_id), optype), []).append(emp)

        # line_id -> factory_id, so tier 2 knows which factory a line is in.
        #
        # Taken from the schema (Production_Lines -> Departments), not inferred
        # from the operators. Inferring it only covered lines that had a crew,
        # and once crews became shift-pure only 46 of 99 lines did — the other
        # 53 had no factory, skipped the factory tier, and went straight to
        # company-wide sourcing.
        line_factory = dict(line_factory_map or {})
        if not line_factory:
            for row in df.dropna(subset=['line_id']).itertuples(index=False):
                line_factory.setdefault(int(row.line_id), int(row.factory_id))

        return {'by_line': by_line, 'by_factory': by_factory, 'by_skill': by_skill,
                'line_factory': line_factory, 'shift_of': shift_of, 'crew_of': crew_of,
                'efficiency_of': efficiency_of}

    @staticmethod
    def _available_at(operator_index, employee_id, when):
        """Compatibility wrapper around the authoritative worker calendar."""
        return operator_available_at(operator_index, employee_id, when)

    @staticmethod
    def _assign_operators(operator_index, required_machine_type, line_id, headcount,
                           operator_busy_minutes, duration_mins, start_dt=None,
                           operator_reservations=None, order_priority=None,
                           operator_segments_out=None):
        """
        Picks the operators who will run an operation: one per engaged machine,
        skill-matched, scoped to the line's own batch first, least-loaded
        within whichever tier supplies them.

        Same selection pattern as machine_ids_used — least-loaded first — so
        assignment spreads across the workforce instead of hammering whichever
        employee happens to sort first.

        NOTE ON SCOPE: operators are assigned here, at export time, not
        scheduled as a constrained resource inside the GA. Labour is a SOFT
        objective in this system (see the idle-labour term in ga/fitness.py) —
        the GA optimises how fully the workforce is used, but does not treat
        an individual operator's availability as a hard constraint the way it
        now does for machines. With ~2,330 skilled operators per operation
        type against a usable pool of ~558 machines of that type, operator
        availability is not the binding constraint, so modelling it as one
        would add cost without changing the schedule.

        EXPORT-TIME FEASIBILITY (audit #3). The GA's soft-labour objective is
        unchanged and deliberately stays soft -- but the EXPORTED schedule
        must not claim one person is in two places at once, which is what
        this method used to do. It checked availability only at the
        operation's START instant and tracked only accumulated busy minutes
        (a least-loaded tie-break, not a reservation), so the same employee
        could be handed two overlapping operations, and someone whose shift
        ended mid-operation could be assigned work running past it.

        Now:
          (a) reservations -- `operator_reservations` holds the (start, end)
              windows already committed to each employee this pass; an
              employee overlapping any of them is not eligible;
          (b) full-duration availability -- _available_at is required across
              the whole operation, not just its first instant;
          (c) no silent short crews -- if the eligible pool is non-empty but
              smaller than headcount, the caller gets a structured
              understaffed record naming required vs available and the
              limiting factor, instead of a quietly undersized crew.

        Returns (employee_ids, tier_name, understaffed_or_None).
        """
        if operator_reservations is None:
            operator_reservations = {}
        optype = machine_type_to_operation_type(required_machine_type)
        if headcount <= 0:
            # No crew is REQUIRED, so zero coverage is correct and there is
            # nothing to report. The only genuinely recordless case.
            return [], 'unfilled', None
        if optype is None:
            # SILENT ZERO, FIXED. This used to return an empty crew with NO
            # understaffed record, so an operation whose machine type could
            # not be resolved contributed its full labour DEMAND to every
            # denominator while contributing nothing to the shortage count --
            # invisible in the accounting.
            #
            # It is not hypothetical: a staffing replay fed 2,579 operations
            # whose keys did not resolve reported understaffed_operations=0
            # alongside unfilled/required=84.55%, a self-contradiction that
            # only this path can produce. That is the third instance in this
            # task of "zero coverage without a corresponding record", so the
            # rule is now absolute -- if nobody is assigned and somebody was
            # needed, it gets recorded.
            return [], 'unfilled', {
                'required': int(headcount),
                'available': 0,
                'shortfall': int(headcount),
                'duration_minutes': float(duration_mins),
                'shortfall_worker_minutes': int(headcount) * float(duration_mins),
                'limiting_factor': 'unresolved-machine-type',
                'operation_type': None,
                'line_id': int(line_id),
            }

        # Candidate-specific productive segments are the reservation
        # authority. A single continuous [start, start+work] interval is not
        # equivalent when the same worker pauses and resumes across shifts.
        segment_cache = {}

        def _covers_full_duration(employee_id):
            """Available across the operation, not merely at its first tick.

            HALF-OPEN [start, end) (Batch-4 fix 6a). The end instant is NOT
            probed. It used to be, which rejected a worker rostered
            06:00-14:00 for an operation occupying exactly 06:00-14:00: the
            probe at 14:00 lands on the first instant of the NEXT shift,
            which they are not on, so a perfectly crewable operation was
            reported as a shortage. An operation ending exactly when a shift
            ends occupies no time at 14:00, so nobody needs to be there for
            it. The same half-open convention governs reservation overlap in
            _is_free below, so the two cannot disagree about whether
            back-to-back operations collide.

            Sampled hourly rather than continuously: _available_at resolves
            shift band and rest day, both of which change on hour/day
            boundaries, so an hourly walk cannot step over a transition. The
            last instant strictly inside the operation is checked explicitly
            in case the duration is not a whole number of hours.
            """
            if start_dt is None:
                segment_cache[employee_id] = ()
                return True
            # An operation cannot start before its assigned worker. If this
            # worker is off duty at the decoded start, choosing them would
            # silently turn machine waiting into unreported setup/idle time.
            if not AIDecisionEngine._available_at(
                    operator_index, employee_id, start_dt):
                return False
            # SEGMENTATION FIX. This used to walk the span hourly and reject
            # on the first unavailable instant -- i.e. demand that ONE worker
            # be continuously on duty for the operation's whole wall-clock
            # span. Shifts are three 8h bands tiling the day, so any operation
            # longer than 8h necessarily contains an instant where its worker
            # is off duty, and that rule rejected every such operation
            # unconditionally. Measured on the authoritative snapshot: 0 of
            # 776 operations longer than 8h were staffable (a 100% failure
            # rate, against 67.8% staffed below 8h), accounting for 3,441,832
            # of 6,082,368 unfilled worker-minutes -- 56.6% of the shortage.
            #
            # The requirement was never part of the model the data was
            # generated under. data_generation/simulation_calendar.py pauses
            # work at every off-duty boundary and resumes it with THE SAME
            # worker ("no implicit handoffs"), so the correct question is
            # whether the worker's recurring availability can absorb the work
            # CONTENT, not whether they can cover it without stopping.
            #
            # utils/work_calendar.can_cover_work runs the seeder's own walk
            # with this engine's availability predicate injected, so the two
            # cannot drift apart.
            if AIDecisionEngine.WORKER_CONTINUITY_POLICY == 'continuous':
                op_end_dt = start_dt + timedelta(
                    minutes=max(0.0, float(duration_mins)))
                # SUPERSEDED rule, retained ONLY so the staffing measurement
                # can attribute its own correction: replaying under this and
                # under 'segmented' separates "the shortage was mismeasured
                # because of stale reservations" from "...because of the
                # continuity requirement". Not a supported production setting.
                probe = start_dt
                while probe < op_end_dt:
                    if not AIDecisionEngine._available_at(operator_index, employee_id, probe):
                        return False
                    probe += timedelta(hours=1)
                last_interior = op_end_dt - timedelta(microseconds=1)
                if last_interior > start_dt and not AIDecisionEngine._available_at(
                        operator_index, employee_id, last_interior):
                    return False
                segment_cache[employee_id] = ((start_dt, op_end_dt),)
                return True
            work_minutes = max(0.0, float(duration_mins))
            if work_minutes <= 0:
                segment_cache[employee_id] = ()
                return True
            ok, _end, segments = can_cover_work(
                start_dt, work_minutes,
                lambda t: AIDecisionEngine._available_at(operator_index, employee_id, t))
            if ok:
                segment_cache[employee_id] = tuple(segments)
            return ok

        def _res_key(employee_id):
            """
            ONE key type for the reservation map. This is a defect fix, not a
            tidy-up.

            _build_operator_index yields employee ids as INT (1, 701, 1681);
            database.queries.fetch_committed_operator_reservations keys its
            map with str(employee_id) ('1', '2', '3'). _is_free looked up the
            int against string keys, so it never matched and every
            DB-LOADED committed reservation was silently ignored -- measured
            directly: raw key overlap 0 of 3, and reserving 19,783 operators
            across an operation's window still assigned a full crew.

            Within-run double-booking DID work, because the write below and
            the read here both used the index's int, which is exactly why the
            existing regression test passed: it builds its index with STRING
            ids ('e1'), so its write and read agreed and the production type
            mismatch was never exercised. A stub that did not reproduce the
            real caller's key type.

            Normalising to str at both ends makes DB-loaded and within-run
            reservations share one namespace.
            """
            return str(employee_id)

        def _is_free(employee_id):
            """No productive segment overlaps an existing worker segment."""
            if start_dt is None:
                return True
            proposed = segment_cache.get(employee_id, ())
            for work_start, work_end in proposed:
                for busy_start, busy_end in operator_reservations.get(
                        _res_key(employee_id), ()):
                    if busy_start < work_end and work_start < busy_end:
                        return False
            return True

        def on_duty(pool):
            if start_dt is None:
                return pool
            return [e for e in pool if _covers_full_duration(e) and _is_free(e)]

        def _efficiency_weight():
            """
            How much this operation's staffing should favour skill over an
            even spread of work. Returns a value in
            [EFFICIENCY_WEIGHT_BASE, EFFICIENCY_WEIGHT_MAX].

            SIGNAL USED: the GNN's per-operation PRIORITY prediction, passed
            in as `order_priority`. Chosen over order slack for three
            reasons: it is already computed and already in scope at this
            point in the export path (no new query, no new failure mode);
            it is defined on [0, 1] with 1 = most urgent, so it maps onto a
            weight without a second calibration that could go stale the way
            the fuzzy slack anchors did; and it is the same quantity the
            fitness function already uses to decide which orders matter,
            so staffing and scheduling agree about what is at risk instead
            of optimising against two different notions of urgency.

            A missing prediction falls back to the base weight -- the
            neutral reading, not the most aggressive one.
            """
            if order_priority is None:
                return EFFICIENCY_WEIGHT_BASE
            p = min(max(float(order_priority), 0.0), 1.0)
            return EFFICIENCY_WEIGHT_BASE + p * (
                EFFICIENCY_WEIGHT_MAX - EFFICIENCY_WEIGHT_BASE)

        def _select_crew(pool, n):
            """
            Pick `n` operators from an ALREADY-ELIGIBLE pool, balancing load
            against skill (Batch-4 fix 6d).

            ELIGIBILITY IS NOT NEGOTIABLE. Everyone in `pool` has already
            passed skill, factory, full-duration shift coverage and
            reservation checks. Efficiency only ever ORDERS these survivors;
            it can never admit someone the constraints excluded.

            THE KEY:

                score = (1 - W) * load_term + W * skill_term   (lower wins)

              * load_term  = own committed minutes / the BUSIEST candidate's.
                             Range [min_load/max_load, 1]. NOTE this is a
                             ratio-to-max, NOT min-max normalisation: the
                             idlest candidate scores 0 only when its load is
                             literally 0, not merely because it is the
                             idlest.
              * skill_term = (max_eff - own_eff) / (max_eff - min_eff) across
                             THIS pool. This one IS min-max: 0 = most
                             capable, 1 = least.

            WHY RATIO-TO-MAX FOR LOAD: busy-minutes grow without bound across
            a pass while efficiency sits in a fixed 70-98 band, so a raw sum
            would hand every decision to load permanently. Normalising per
            pool keeps the weight meaningful.

            *** WHAT THIS DOES **NOT** GUARANTEE -- stated plainly because an
            earlier version of this comment claimed the opposite, and the
            test that "proved" it only ever ran at priority 0.0, the one
            region where it happens to hold. ***

            There is NO "eventually the idle worker wins" property. Because
            normalisation pins the busiest candidate at load_term = 1
            regardless of magnitude, the busiest-and-most-skilled candidate
            scores (1-W), and an idle-and-least-skilled one scores
            approximately W. So skill wins whenever W > 0.5 -- and raising
            the busy operator's load changes nothing, because 1 is already
            the ceiling. Measured directly across the weight range, with one
            candidate at 1,000,000,000 committed minutes and the other at 0:

                priority 0.00 (W=0.30) -> idle/least-skilled wins
                priority 0.25 (W=0.38) -> idle/least-skilled wins
                priority 0.50 (W=0.45) -> idle/least-skilled wins
                priority 0.75 (W=0.52) -> BUSIEST/most-skilled wins
                priority 1.00 (W=0.60) -> BUSIEST/most-skilled wins

            With the current constants (BASE 0.30, MAX 0.60) the crossover
            sits at priority ~0.667. Above it, a single operator can be
            selected without bound for high-priority work.

            DEFERRED, NOT FIXED: making the guarantee true needs an explicit
            fairness constraint -- e.g. a hard cap on committed minutes per
            worker per window, which turns "this worker is saturated" into an
            eligibility failure (a reportable shortage) rather than a scoring
            preference. That is a genuine scheduling-policy change that can
            manufacture shortages, so it is deliberately not being introduced
            immediately before a reseed whose output must stay comparable to
            the recorded history. Recommended as a follow-up with its own
            before/after measurement.
            """
            if not pool:
                return []
            # Normalised lookup: operator_busy_minutes is SEEDED from the
            # reservation map (str keys) in _export_proposed_schedule, so an
            # int-keyed read would miss a worker's committed load entirely
            # and the least-loaded choice would ignore it.
            loads = {e: operator_busy_minutes.get(_res_key(e), 0.0) for e in pool}
            effs = {e: operator_index.get('efficiency_of', {}).get((e, optype))
                    for e in pool}
            known = [v for v in effs.values() if v is not None]

            max_load = max(loads.values()) if loads else 0.0
            if not known or max(known) == min(known):
                # No efficiency signal, or everyone equal -- fall back to
                # pure least-loaded, i.e. the previous behaviour exactly.
                return sorted(pool, key=lambda e: (loads[e], e))[:n]

            hi, lo = max(known), min(known)
            mean_known = sum(known) / len(known)

            def score(e):
                load_term = (loads[e] / max_load) if max_load > 0 else 0.0
                # An operator with no recorded rating for this operation type
                # is treated as average rather than best or worst -- the same
                # "assume typical, don't invent an extreme" convention
                # graph/node_features.py uses for missing efficiency.
                own = effs[e] if effs[e] is not None else mean_known
                skill_term = (hi - own) / (hi - lo)
                return (1.0 - weight) * load_term + weight * skill_term

            weight = _efficiency_weight()
            return sorted(pool, key=lambda e: (score(e), e))[:n]

        def _limiting_factor(n_available):
            """Which constraint actually bound the pool, narrowest first.

            Reported so an understaffed operation says WHY, not just that it
            happened -- 'nobody has this skill' and 'everyone with it is
            already committed elsewhere' call for completely different
            responses (hire/train vs. reschedule).
            """
            skilled = operator_index['by_skill'].get(optype, [])
            if not skilled:
                return 'skill'
            factory_id = operator_index['line_factory'].get(int(line_id))
            in_factory = operator_index['by_factory'].get((factory_id, optype), []) \
                if factory_id is not None else []
            if not in_factory:
                return 'factory'
            if start_dt is not None:
                # Scoped to IN-FACTORY workers (Batch-4 fix 6b): with
                # cross-factory sourcing prohibited, a company-wide pool
                # would report 'shift' when the real constraint is that
                # nobody local is on duty.
                on_shift = [e for e in in_factory if _covers_full_duration(e)]
                if not on_shift:
                    return 'shift'
                if n_available < len([e for e in on_shift if _is_free(e)]):
                    return 'shift'
                return 'already-reserved'
            return 'shift'

        candidates = on_duty(operator_index['by_line'].get((int(line_id), optype), []))
        tier = 'batch'
        if len(candidates) < headcount:
            factory_id = operator_index['line_factory'].get(int(line_id))
            if factory_id is not None:
                wider = on_duty(operator_index['by_factory'].get((factory_id, optype), []))
                if len(wider) >= len(candidates):
                    candidates, tier = wider, 'factory'
        # NO CROSS-FACTORY TIER (Batch-4 fix 6b).
        #
        # There used to be a third tier here: operator_index['by_skill'],
        # every skilled operator COMPANY-WIDE. It was live, and it silently
        # crewed an operation in factory 1 with a worker indexed only in
        # factory 2 -- reporting no shortage, because from the exporter's
        # point of view the crew was full. That is not a staffing solution,
        # it is an unreported transfer between industrial sites, with no
        # travel time, no authorisation and no cost. It also directly
        # contradicts the one-factory-per-order constraint the GA now
        # enforces (ga/chromosome.py) and the seeder's own rule ("never
        # substitute a random employee or borrow from another factory").
        #
        # Sourcing now stops at the order's own factory. No local worker is
        # a SHORTAGE, reported as one below -- an honest "this schedule
        # cannot be crewed here" rather than a fictional crew.

        if not candidates:
            # Nobody qualified is free and on shift anywhere for this whole
            # operation. Reported as unfilled rather than quietly staffed with
            # an off-shift or already-committed operator — a schedule that
            # cannot be crewed should say so.
            return [], 'unfilled', {
                'required': int(headcount),
                'available': 0,
                # Same impact fields the partial-crew branch below records.
                # These were added there first and NOT here, which made every
                # summed worker-minute figure silently ignore the WORST
                # shortages -- the fully-unfilled ones -- because a missing
                # key defaults to zero in any caller that sums them.
                'shortfall': int(headcount),
                'duration_minutes': float(duration_mins),
                'shortfall_worker_minutes': int(headcount) * float(duration_mins),
                'limiting_factor': _limiting_factor(0),
                'operation_type': optype,
                'line_id': int(line_id),
            }

        chosen = _select_crew(candidates, headcount)
        understaffed = None
        if len(chosen) < headcount:
            # (c) Do NOT silently return a short crew. The pool is real but
            # too small, which is a different and more informative failure
            # than "nobody at all", and the caller counts it.
            understaffed = {
                'required': int(headcount),
                'available': int(len(chosen)),
                # Headcount alone cannot be summed into an impact figure: one
                # operation short by 3 operators for 8 hours and one short by
                # 3 for 5 minutes are the same count and wildly different
                # shortages. Carrying the duration here lets a caller report
                # the work that cannot be staffed, not just how many rows
                # tripped the check.
                'shortfall': int(headcount) - int(len(chosen)),
                'duration_minutes': float(duration_mins),
                'shortfall_worker_minutes': (int(headcount) - int(len(chosen))) * float(duration_mins),
                'limiting_factor': _limiting_factor(len(chosen)),
                'operation_type': optype,
                'line_id': int(line_id),
            }
        for emp in chosen:
            _bk = _res_key(emp)
            segments = tuple(segment_cache.get(emp, ()))
            operator_busy_minutes[_bk] = (
                operator_busy_minutes.get(_bk, 0.0) + duration_mins
            )
            if start_dt is not None:
                operator_reservations.setdefault(_bk, []).extend(segments)
            if operator_segments_out is not None:
                operator_segments_out[_bk] = segments
        return chosen, tier, understaffed

    def _export_proposed_schedule(self, chromosome, num_ops, op_details, gnn_preds, df_lines, eligibility_map, order_deadline_abs, df_routings, df_machines, label=None, machine_pools=None, operator_index=None, present_operator_count=0, usable_machine_count=0, op_optype=None, protected_machine_type=None, initial_machine_free=None, initial_machine_last_order=None, initial_operator_reservations=None, base_time=None, phenotype_details=None, scored_fitness=None):
        """Serialize the same authoritative phenotype that fitness scores.

        When phenotype_details is supplied, this method serializes that exact
        already-scored authoritative phenotype. Legacy callers without it are
        decoded once here for compatibility.
        """
        from ga.fitness import evaluate_schedule

        logger.info(
            f"Serializing {'winning' if label is None else label} authoritative phenotype..."
        )
        if operator_index is None:
            raise ValueError(
                "Authoritative export requires operator_index; worker-blind "
                "schedule serialization is not supported"
            )
        base_time = base_time or datetime.now()
        machine_pools = machine_pools or {}
        if op_optype is None:
            op_optype = {
                f"{row['production_order_id']}_{row['operation_id']}":
                    machine_type_to_operation_type(
                        row.get('required_machine_type', '')
                    )
                for _, row in df_routings.iterrows()
            }
        seq_lookup = {
            f"{row['production_order_id']}_{row['operation_id']}":
                row['operation_sequence_number']
            for _, row in df_routings.iterrows()
        }
        release_minutes = {}
        for _, row in df_routings.iterrows():
            order_id = str(row['production_order_id'])
            if order_id in release_minutes:
                continue
            value = row.get('release_date')
            release_minutes[order_id] = (
                max(
                    0.0,
                    (pd.Timestamp(value).to_pydatetime() - base_time).total_seconds() / 60.0,
                )
                if pd.notna(value) else 0.0
            )
        deadline_minutes = {
            str(order_id): (deadline - base_time).total_seconds() / 60.0
            for order_id, deadline in order_deadline_abs.items()
            if deadline is not None
        }
        machine_line_map = {
            row['asset_id']: row['line_id']
            for _, row in df_machines.dropna(subset=['line_id']).iterrows()
        }
        if phenotype_details is None:
            fitness, details = evaluate_schedule(
                chromosome, num_ops, op_details, gnn_preds,
                all_line_ids=(df_lines.loc[df_lines['is_active'] == 1, 'line_id'].tolist()
                              if 'is_active' in df_lines.columns else df_lines['line_id'].tolist()),
                order_deadlines=deadline_minutes, order_release_minutes=release_minutes,
                machine_pools=machine_pools, machine_line_map=machine_line_map,
                usable_machine_count=usable_machine_count, present_operator_count=present_operator_count,
                initial_machine_free=initial_machine_free, initial_machine_last_order=initial_machine_last_order,
                op_sequence_map=seq_lookup, op_optype=op_optype,
                protected_machine_type=protected_machine_type, base_time=base_time,
                operator_index=operator_index, initial_operator_reservations=initial_operator_reservations,
                return_details=True)
        else:
            # Rolling scheduling already decoded and scored this exact phenotype.
            # Re-decoding here is not serialization; it risks changing the plan
            # through a different mutable/input view.  Render the scored object.
            if not isinstance(phenotype_details, dict) or 'phenotype_operations' not in phenotype_details:
                raise ValueError('phenotype_details must be an authoritative evaluation detail mapping')
            if scored_fitness is None:
                raise ValueError('scored_fitness is required with phenotype_details')
            details = phenotype_details
            fitness = (float(scored_fitness),)

        if details.get('invalid') or not details.get('execution_feasible', False):
            raise RuntimeError(
                "Cannot export an infeasible authoritative phenotype: "
                f"{details.get('invalid')} {details.get('infeasibility', '')}"
            )

        line_numbers = {
            int(row['line_id']): row['line_number']
            for _, row in df_lines.iterrows()
        }
        proposed_changes = []
        order_final_op = {}
        operator_tier_counts = {'batch': 0, 'factory': 0, 'unfilled': 0}
        labor_minutes_by_shift = {name: 0.0 for name, _s, _e in SHIFT_BANDS}
        labor_minutes_by_sourcing = {'batch': 0.0, 'factory': 0.0}
        late_order_ids = set()

        for row in details['phenotype_operations']:
            start = base_time + timedelta(minutes=row['start_minutes'])
            base_end = base_time + timedelta(minutes=row['base_end_minutes'])
            end = base_time + timedelta(minutes=row['end_minutes'])
            line_id = int(row['line_id'])
            line_number = line_numbers.get(line_id, f"Line_{line_id:02d}")
            tier = row['operator_sourcing_tier']
            operator_tier_counts[tier] = operator_tier_counts.get(tier, 0) + 1
            work_segments = row['worker_segments_by_employee']
            for segments in work_segments.values():
                for segment_start, segment_end in segments:
                    name = shift_name_for(segment_start)
                    labor_minutes_by_shift[name] = (
                        labor_minutes_by_shift.get(name, 0.0)
                        + (segment_end - segment_start).total_seconds() / 60.0
                    )
                    if tier in labor_minutes_by_sourcing:
                        labor_minutes_by_sourcing[tier] += (
                            segment_end - segment_start
                        ).total_seconds() / 60.0

            order_id = row['production_order_id']
            deadline = order_deadline_abs.get(order_id)
            if deadline is not None and end > deadline:
                late_order_ids.add(order_id)
            sequence = seq_lookup[row['operation_key']]
            current = order_final_op.get(order_id)
            if current is None or sequence >= current['seq']:
                order_final_op[order_id] = {
                    'seq': sequence,
                    'base_end_time': base_end,
                    'cushioned_end_time': end,
                }

            proposed_changes.append({
                'production_order_id': order_id,
                'operation_id': row['operation_id'],
                'assigned_line_id': line_id,
                'assigned_line_number': line_number,
                'machines_used': len(row['machine_ids']),
                'machine_ids_used': [int(a) for a in row['machine_ids']],
                'operators_used': len(row['employee_ids']),
                'employee_ids_used': list(row['employee_ids']),
                'worker_segments_by_employee': {
                    str(employee_id): [
                        {'start': segment_start.isoformat(),
                         'end': segment_end.isoformat()}
                        for segment_start, segment_end in segments
                    ]
                    for employee_id, segments in work_segments.items()
                },
                'operator_sourcing_tier': tier,
                'proposed_start_time': start.isoformat(),
                'base_end_time': base_end.isoformat(),
                'cushioned_end_time': end.isoformat(),
                'setup_minutes': row['setup_minutes'],
                'productive_minutes': row['productive_minutes'],
                'buffered_productive_minutes': row['buffered_productive_minutes'],
                'order_deadline': (
                    deadline.isoformat() if deadline is not None else None
                ),
                'labor_allocation': (
                    f"Fixed same-worker crew assigned to {line_number}"
                ),
                'xai_reason': (
                    f"LA selected {line_number}; MC selected "
                    f"{len(row['machine_ids'])} eligible machine(s). "
                    f"A deterministic {tier} crew performs work only in its "
                    f"common availability segments and the machines remain "
                    f"occupied through {end.isoformat()}."
                ),
            })

        suffix = f"_{label}" if label else ""
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        output_path = os.path.join(OUTPUT_DIR, f"proposed_schedule{suffix}.json")
        with open(output_path, "w", encoding="utf-8") as handle:
            json.dump(proposed_changes, handle, indent=4)
        stamp_artifact(output_path, self.run_id)

        order_completions = self._export_order_completions(
            order_final_op, order_deadline_abs, label=label, base_time=base_time
        )
        with_deadline = [
            row for row in order_completions
            if row['existing_committed_deadline'] is not None
        ]
        added_late = [
            row for row in with_deadline
            if (row['schedule_added_late_hours'] or 0.0) > 0.0
        ]
        added_late_hours = [
            row['schedule_added_late_hours'] for row in added_late
        ]
        already_passed = [
            row for row in with_deadline if row['deadline_already_passed']
        ]
        makespan = float(details['makespan_minutes'])
        labor_used = float(details['labor_used_minutes'])
        labor_capacity = present_operator_count * makespan
        utilization = labor_used / labor_capacity if labor_capacity > 0 else 0.0
        raw_ceiling = (
            usable_machine_count / present_operator_count
            if present_operator_count > 0 else 0.0
        )
        ceiling = min(1.0, raw_ceiling)
        machines_in_reserve = max(
            0, int(df_machines['asset_id'].nunique()) - int(usable_machine_count)
        )

        return {
            'label': label or 'best',
            'fitness': float(fitness[0]),
            'fitness_components': details['fitness_components'],
            'phenotype_hash': details['phenotype_hash'],
            'high_risk_reroutes': 0,
            'labor_utilization_rate': round(utilization, 4),
            'labor_utilization_ceiling': round(ceiling, 4),
            'binding_resource': 'labour' if raw_ceiling > 1.0 else 'machines',
            'machines_per_operator_on_duty': round(raw_ceiling, 3),
            'labor_utilization_vs_ceiling': round(
                utilization / ceiling if ceiling > 0 else 0.0, 4
            ),
            'idle_employee_minutes': round(
                max(0.0, labor_capacity - labor_used), 0
            ),
            'employee_minutes_used': round(labor_used, 0),
            'present_operators': present_operator_count,
            'operator_sourcing': operator_tier_counts,
            'understaffed_operations': 0,
            'understaffed_detail': [],
            'execution_feasible': True,
            'operator_busy_minutes_out': details['operator_busy_minutes'],
            'operator_reservations_out': details['final_operator_reservations'],
            'labor_minutes_by_shift': {
                key: round(value, 0)
                for key, value in labor_minutes_by_shift.items()
            },
            'labor_minutes_by_sourcing_tier': {
                key: round(value, 0)
                for key, value in labor_minutes_by_sourcing.items()
            },
            'machines_held_in_reserve': machines_in_reserve,
            'orders_breaching_deadline': len(late_order_ids),
            'total_lateness_hours': round(
                sum(
                    row['late_by_hours'] or 0.0
                    for row in order_completions
                ), 1
            ),
            'orders_with_deadline': len(with_deadline),
            'on_time_rate': round(
                1.0 - len(added_late) / len(with_deadline)
                if with_deadline else 1.0, 4
            ),
            'orders_made_late_by_this_schedule': len(added_late),
            'orders_already_past_deadline': len(already_passed),
            'mean_tardiness_hours_of_late_orders': round(
                sum(added_late_hours) / len(added_late_hours)
                if added_late_hours else 0.0, 1
            ),
            'max_tardiness_hours': round(
                max(added_late_hours) if added_late_hours else 0.0, 1
            ),
            'load_balance_stdev_minutes': round(
                details['load_balance_stdev_minutes'], 1
            ),
            'makespan_minutes': round(makespan, 1),
            'schedule_file': output_path,
        }

    def _export_order_completions(self, order_final_op: dict, order_deadline_abs: dict,
                                   label=None, base_time: datetime = None):
        """
        Rolls the per-operation schedule up to ONE row per order: when the
        order is actually projected to finish (its final operation's
        cushioned completion, i.e. already including each operation's own
        GNN-delay-risk buffer), plus a coarser order-level "quote" cushion
        (ORDER_QUOTE_CUSHION_DAYS) on top of that for what gets told to the
        customer — separate from any deadline already committed in the DB.

        Previously nothing like this existed: the model only ever predicted
        per-OPERATION duration/delay/priority, and each order's true
        completion time was implicitly computed inside the GA simulation
        (order_ready_minutes) but thrown away at the end of the loop instead
        of being surfaced anywhere.
        """
        base_time = base_time or datetime.now()
        order_completions = []
        breached_orders = []
        already_overdue_orders = []

        for order_id, final_op in order_final_op.items():
            predicted_completion = final_op['cushioned_end_time']
            quoted_delivery_date = predicted_completion + timedelta(days=ORDER_QUOTE_CUSHION_DAYS)

            committed_deadline = order_deadline_abs.get(order_id)

            # Separate "this schedule makes the order late" from "the order
            # was already past its date before we started". Only the first is
            # something the optimizer can be held responsible for, and only
            # the first is what ga/fitness.py penalises (it clamps deadlines
            # to now for exactly this reason). Conflating them is what
            # produced the old headline number of 450,827 lateness-hours,
            # which was mostly pre-existing breach and told a manager nothing
            # about the schedule in front of them.
            already_overdue = committed_deadline is not None and committed_deadline < base_time
            if already_overdue:
                already_overdue_orders.append(order_id)

            breach_risk = committed_deadline is not None and predicted_completion > committed_deadline
            late_by_hours = None
            if breach_risk:
                late_by_hours = round((predicted_completion - committed_deadline).total_seconds() / 3600.0, 1)
                breached_orders.append(order_id)

            # Lateness attributable to THIS schedule: measured against the
            # clamped deadline, matching the GA's own objective.
            effective_deadline = max(committed_deadline, base_time) if committed_deadline is not None else None
            schedule_added_late_hours = None
            if effective_deadline is not None:
                schedule_added_late_hours = round(
                    max(0.0, (predicted_completion - effective_deadline).total_seconds() / 3600.0), 1
                )

            order_completions.append({
                'production_order_id': str(order_id),
                'predicted_completion': predicted_completion.isoformat(),
                'quote_cushion_days': ORDER_QUOTE_CUSHION_DAYS,
                'customer_quoted_delivery_date': quoted_delivery_date.isoformat(),
                'existing_committed_deadline': committed_deadline.isoformat() if committed_deadline is not None else None,
                'deadline_already_passed': already_overdue,
                'breach_risk': breach_risk,
                'late_by_hours': late_by_hours,
                'schedule_added_late_hours': schedule_added_late_hours,
            })

        if breached_orders:
            # Only a sample of order IDs is logged. Dumping all of them inline
            # is what grew logs/agent.log past 9 MB — a single warning could
            # carry 1,400 IDs, and it appears once per candidate schedule. The
            # full list is in the JSON this function writes.
            newly_breached = [o for o in breached_orders if o not in set(already_overdue_orders)]
            sample = sorted(breached_orders)[:10]
            logger.warning(
                f"⚠️ {len(breached_orders)} of {len(order_completions)} order(s) projected to finish "
                f"after their committed deadline — of which {len(already_overdue_orders)} were ALREADY "
                f"past that date before this schedule was built, and {len(newly_breached)} are made late "
                f"by this schedule. Sample: {sample}"
                f"{' ...' if len(breached_orders) > len(sample) else ''}. Full detail in the "
                f"order_completions JSON. Deadline lateness is a weighted soft penalty in the GA "
                f"(ga/fitness.py ZETA / SERVICE_LEVEL_WEIGHT), not a hard constraint, so the optimizer "
                f"can still return a schedule like this if every alternative is worse."
            )

        output_path = os.path.join(OUTPUT_DIR, f"order_completions{'_' + label if label else ''}.json")
        with open(output_path, "w") as f:
            json.dump(order_completions, f, indent=4)
        stamp_artifact(output_path, self.run_id)

        logger.info(f"✅ Order-level completion rollup ({len(order_completions)} orders) saved to {output_path}.")
        return order_completions

    def _log_optimization_metrics(self, fitness, logbook, pop_size, generations):
        """Logs GA performance to Optimization_Metrics_Log with exact execution parameters.

        NOTE (pre-existing, not introduced by the fitness-scale fix): the DB
        column is named optimized_makespan_minutes, but what's actually
        stored here is the full combined fitness score (makespan + delay
        penalty + priority reward + load balance + lateness, see
        ga/fitness.py), not pure makespan-in-minutes — that's been true
        since before this file was touched this session. Worth renaming the
        column or splitting it into separate fields if anything downstream
        reads it expecting a literal minutes value; flagging rather than
        silently changing the DB schema without knowing what else consumes
        it.
        """
        session = next(get_db_session())
        try:
            sql = text("""
                INSERT INTO Optimization_Metrics_Log 
                (ga_generation_count, ga_population_size, optimized_makespan_minutes)
                VALUES (:gens, :pop, :fit)
            """)
            session.execute(sql, {'gens': generations, 'pop': pop_size, 'fit': abs(fitness)})
            session.commit()
            logger.info("✅ Optimization metrics logged to database.")
        except Exception as e:
            session.rollback()
            logger.error(f"❌ Failed to log metrics: {e}")
        finally:
            session.close()

if __name__ == "__main__":
    print("Starting Line-Based AI Decision Engine in Proposal Mode...")
    agent = AIDecisionEngine()
    # Simulate a manager prioritizing Order '1' via an API request payload
    test_manager_picks = ['1']
    print(f"Simulating frontend manager directive for orders: {test_manager_picks}")
    agent.trigger_rescheduling(manager_prioritized_orders=test_manager_picks)
    print("\n[OK] AI Agent successfully completed a full line-based optimization loop!")
