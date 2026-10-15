import pandas as pd
import json
from datetime import datetime
from typing import NamedTuple
from database.db import get_engine
from utils.logger import get_logger
from utils.shift_rotation import DAYS_PER_WEEK, NUM_SHIFTS

logger = get_logger("db_queries", "events.log")

def fetch_machine_state() -> pd.DataFrame:
    """
    Extracts current machine statuses, capabilities, and locations.
    Purpose: Forms the 'Machine Nodes' in the Heterogeneous Graph.
    """
    logger.info("Fetching machine state from Machinery_Assets...")
    query = """
        SELECT
            ma.asset_id,
            ma.factory_id,
            ma.line_id,
            ma.machine_model,
            ma.current_status,
            mc.operation_type,
            mc.max_rpm
        FROM Machinery_Assets ma
        LEFT JOIN Machine_Capabilities mc ON ma.asset_id = mc.asset_id;
    """
    return pd.read_sql(query, get_engine())

def fetch_worker_state() -> pd.DataFrame:
    """
    Extracts available operators and their specific skill efficiencies for the current shift.
    Purpose: Forms the 'Worker Nodes' and determines Assignment edges.

    IMPORTANT: this must return exactly ONE row per employee_id. A worker with
    several rows in Operator_Skill_Matrix (one per operation_type they're
    skilled in) would otherwise come back as several rows here, and
    graph/node_features.py builds one graph node per row — silently creating
    duplicate/orphaned worker nodes. skill_type is aggregated into a
    comma-separated list (edge_builder still matches machine-type prefixes
    against it) and skill_breadth/efficiency_score are aggregated per worker.
    """
    logger.info("Fetching workforce state and skill matrices...")
    query = """
        SELECT 
            e.employee_id AS worker_id,
            e.role,
            CASE WHEN ws.is_on_leave = True THEN 'Absent' ELSE 'Present' END AS status,
            GROUP_CONCAT(DISTINCT osm.operation_type SEPARATOR ',') AS skill_type,
            AVG(osm.efficiency_rating) AS efficiency_score,
            COUNT(DISTINCT osm.operation_type) AS skill_breadth
        FROM Employees e
        JOIN Work_Schedules ws ON e.employee_id = ws.employee_id
        LEFT JOIN Operator_Skill_Matrix osm ON e.employee_id = osm.employee_id
        WHERE e.is_active = True 
        AND ws.shift_date = (SELECT MAX(shift_date) FROM Work_Schedules)
        GROUP BY e.employee_id, e.role, ws.is_on_leave;
    """
    return pd.read_sql(query, get_engine())

def fetch_active_orders_and_routings() -> pd.DataFrame:
    """
    Extracts orders that are currently in production or delayed, alongside their routing sequence.
    Purpose: Forms the baseline 'Operation Nodes' and topological Precedence edges.

    order_deadline is the CUSTOMER commitment (Purchase_Orders.delivery_deadline).

    It previously read Production_Orders.scheduled_end_date, which is an
    INTERNAL plan milestone, not a promise to anyone outside the factory —
    and one that has almost always already elapsed. Measured on this
    database:

        status       orders   scheduled_end_date  delivery_deadline  avg days
                              already in the past  already past       to cust.
        Pending       1008          709                  0             +31.3
        Processing     320          320                  0             +30.9
        Delayed         82           82                  0             +31.3

    So 1,111 of 1,410 active orders were being scheduled against a deadline
    that was already blown before the optimizer started, which is why a
    normal run reported ~99.6% of orders breaching. The customer date was
    sitting unused in Purchase_Orders the whole time, joinable on po_id.

    scheduled_end_date is still returned, as internal_plan_date, because
    missing your own internal milestone is genuinely useful information for a
    manager — it just must not be the thing the GA optimises against.

    remaining_frac is the fraction of an operation's lots not yet 'Done'
    (1.0 = untouched, 0.0 = finished). The routing table describes every step
    of a product, so this query previously handed the scheduler every
    operation of every active order regardless of how much of it was already
    complete — 27% of all lot-rows in this database are already Done, and
    1,922 of 7,050 operations are partially complete. Callers should scale
    each operation's quantity by this so the GA plans the work that is
    actually left. See ga/scheduler.py.

    ORDER BY was previously ABSENT here, across a three-table JOIN plus a
    LEFT JOIN subquery — row order was therefore undefined by SQL semantics,
    not merely unspecified-but-stable. Confirmed as a REAL source of
    non-reproducibility, not theoretical: two identical GA runs (same seed,
    same frozen snapshot, same origin, same GNN checkpoint) produced
    genuinely different schedules, traced down to a single first-run-after-
    a-fresh-snapshot-build case reproducing differently from every
    subsequent run against the same (unchanged) data — direct evidence the
    row order this query returned depended on incidental table/query-plan
    state, not just content. Every downstream consumer already assumes
    operations arrive in routing order per order (ga/fitness.py's own
    precedence check, ga/operators.py::repair_precedence, both keyed on
    operation_sequence_number) — so ordering by (production_order_id,
    operation_sequence_number) is not an arbitrary tie-break, it is the
    ordering this query's own data already claims to have and every caller
    already assumes.

    BUG FIX (PIPELINE_METHODOLOGY_TRACE.md Stage 3 / FULL_CODEBASE_AUDIT.md):
    the Purchase_Orders join was a plain INNER JOIN, so an active order with
    no linked Purchase_Orders row was silently dropped from the graph and
    the GA entirely -- not just given no deadline, excluded from scheduling
    altogether, with nothing logged. Changed to LEFT JOIN; such an order now
    still enters the graph/GA with order_deadline=NULL (matching the
    existing "no deadline -> not penalized" convention every downstream
    caller already handles for orders whose Purchase_Orders row simply has
    no delivery_deadline set), and is logged below instead of silently
    disappearing. On the current database this changes nothing (0 of 1,029
    active orders lack a Purchase_Orders match, measured directly before
    this fix) -- kept as a correctness fix for any future dataset/reseed
    where that stops being true, not because it changed today's numbers.
    """
    logger.info("Fetching active production orders and routings...")
    query = """
        SELECT
            po.production_order_id,
            po.target_quantity,
            pu.delivery_deadline AS order_deadline,
            po.scheduled_end_date AS internal_plan_date,
            po.scheduled_start_date AS release_date,
            COALESCE(prog.remaining_frac, 1.0) AS remaining_frac,
            ro.operation_id,
            ro.operation_sequence_number,
            ro.operation_name,
            ro.standard_allowed_minutes as sam,
            ro.required_machine_type
        FROM Production_Orders po
        LEFT JOIN Purchase_Orders pu ON pu.po_id = po.po_id
        JOIN Routing_Operations ro ON po.product_id = ro.product_id
        LEFT JOIN (
            SELECT production_order_id,
                   operation_id,
                   SUM(execution_status <> 'Done') / COUNT(*) AS remaining_frac
            FROM Job_Shop_Schedules
            GROUP BY production_order_id, operation_id
        ) prog
          ON prog.production_order_id = po.production_order_id
         AND prog.operation_id = ro.operation_id
        WHERE po.status IN ('Pending', 'Processing', 'Delayed')
        ORDER BY po.production_order_id, ro.operation_sequence_number;
    """
    df = pd.read_sql(query, get_engine())

    # Orders with no Purchase_Orders match at all (po_id NULL or not found)
    # have order_deadline NULL for every one of their rows -- surfaced here,
    # once per order, rather than silently disappearing (the pre-fix INNER
    # JOIN behaviour) or silently blending into the existing "no deadline"
    # population with no visibility into how many there are.
    missing_po = df.loc[df['order_deadline'].isna(), 'production_order_id'].unique()
    if len(missing_po) > 0:
        logger.warning(
            f"{len(missing_po)} active order(s) have no matching Purchase_Orders row "
            f"(no customer delivery_deadline available) -- scheduled with order_deadline=NULL "
            f"(not penalized for lateness, same as any other missing-deadline order), not dropped: "
            f"{sorted(str(o) for o in missing_po)[:20]}"
            f"{' ...' if len(missing_po) > 20 else ''}"
        )
    return df

def fetch_current_shop_schedule() -> pd.DataFrame:
    """
    Extracts the exact schedule block execution state.
    Purpose: Identifies bottlenecks, calculates current WIP, and flags disruptions for the AI Agent.
    """
    logger.info("Fetching active job shop schedules...")
    query = """
        SELECT 
            production_order_id,
            operation_id,
            line_id,
            asset_id,
            employee_id,
            planned_start_time,
            planned_end_time,
            execution_status
        FROM Job_Shop_Schedules
        WHERE execution_status IN ('Running', 'Scheduled', 'Delayed', 'Failed');
    """
    return pd.read_sql(query, get_engine())

def fetch_operation_assignments() -> pd.DataFrame:
    """
    Distinct (operation -> machine, worker) assignments across EVERY lot of
    each active operation, including lots already marked 'Done'.

    Purpose: the single leak-free source for both (a) the operation-level
    machine-parallelism feature and (b) the machine->operation /
    worker->operation graph edges.

    IMPORTANT — why this deliberately does NOT filter on execution_status,
    unlike fetch_current_shop_schedule() above:

    That function excludes 'Done' rows, and ANYTHING derived from that
    filtered row set leaks the delay label. An operation that is
    Delayed/Failed is by definition one that has already been worked on, so
    most of its ~60 lots are 'Done' and get filtered away, leaving only a
    handful; an untouched operation keeps all ~60. Measured against this
    database: a single threshold on the Done-filtered distinct-machine count
    predicts the delay label with 99.4% accuracy versus a 72.7% majority-class
    baseline, while the same count computed over ALL lots scores 72.8% — i.e.
    essentially every bit of that feature's apparent skill was the WHERE
    clause, not the factory.

    This is the same class of leak that graph/node_features.py already
    removed once (is_running / is_delayed / lot_delay_frac), arriving through
    a different door: not the column that was selected, but the rows that
    were excluded. Build parallelism features and assignment edges from here,
    never from fetch_current_shop_schedule().
    """
    logger.info("Fetching leak-free per-operation machine/worker assignments (all lots)...")
    query = """
        SELECT DISTINCT
            j.production_order_id,
            j.operation_id,
            j.asset_id,
            j.employee_id
        FROM Job_Shop_Schedules j
        JOIN Production_Orders p ON p.production_order_id = j.production_order_id
        WHERE p.status IN ('Pending', 'Processing', 'Delayed');
    """
    return pd.read_sql(query, get_engine())

def fetch_dispatch_context() -> pd.DataFrame:
    """
    Every lot's causal delay-risk context AS OF THE MOMENT IT WAS DISPATCHED
    -- machine health, employee availability, and queue congestion -- for the
    GNN's operation node FEATURES (not the label). See
    PROJECT_STATUS.txt Section 0E.

    Deliberately unfiltered on execution_status, same reasoning as
    fetch_operation_assignments()/fetch_actual_vs_planned_timing() above: any
    status-keyed restriction reintroduces a "has this operation started"
    confound.

    WHY THESE ARE SAFE FEATURES, not a third leak: each column is captured
    in data_generation/seed_database_fixed.py BEFORE the lot's own execution-
    variance/breakdown roll is applied -- machine_status_at_dispatch and
    machine_breakdown_count_at_dispatch are read right after that machine's
    repair check but before this specific lot can trigger/count a new
    breakdown; employee_on_leave_at_dispatch and queue_delay_mins_at_dispatch
    are both fully determined by the time the lot STARTS (queue_delay_mins
    specifically from earliest_possible vs. prev_end, captured before
    prev_end is reassigned to this lot's own outcome). All are causally
    upstream of the lot's own realized duration/lateness, never derived
    from it.

    machine_status_at_dispatch is kept for reference but is NOT a useful
    feature in this dataset: measured to read 'Breakdown' for a scheduled
    machine NEVER (machine_free_time bookkeeping means a broken machine
    can't be re-dispatched to before its repair check resets it to
    'Active') -- see machine_breakdown_count_at_dispatch, added to replace
    it with the machine's actual PRIOR breakdown-count history, a signal
    that genuinely varies. See PROJECT_STATUS.txt Section 0E / reversal 8.

    NULL for lots seeded before these columns existed; callers must treat
    that as "no dispatch-context evidence", not as "known good".

    planned_end_time ADDED (delay-label fix aggregation-mismatch follow-up,
    see chat/DELAY_LABEL_FIX_PROPOSAL.md): needed so
    graph/node_features.py can identify each operation's DECISIVE lot (the
    one with the latest planned_end_time -- a leak-safe, pre-outcome proxy
    for "the lot that actually finishes last", measured at 96.0% agreement
    with the true max-actual-end lot on the live database) and read THAT
    lot's own breakdown/leave exposure, instead of averaging across all
    ~19 lots regardless of whether they're the one driving the operation's
    overall lateness. planned_end_time is itself always safe (it's the
    committed schedule, never derived from any outcome) -- what makes the
    OLD mean-based aggregation problematic isn't this column, it's
    diluting a real per-lot signal across lots that mostly aren't decisive.
    """
    logger.info("Fetching per-lot dispatch-time causal context for GNN features...")
    query = """
        SELECT
            production_order_id,
            operation_id,
            planned_end_time,
            machine_status_at_dispatch,
            employee_on_leave_at_dispatch,
            queue_delay_mins_at_dispatch,
            machine_breakdown_count_at_dispatch
        FROM Job_Shop_Schedules;
    """
    return pd.read_sql(query, get_engine())

def fetch_actual_vs_planned_timing() -> pd.DataFrame:
    """
    For seeded data, actual timestamps are synthetic perturbations. Machine/
    worker availability and routing precedence advance using planned ends;
    actual overruns do not cascade into downstream operations.

    Every lot's planned vs. ACTUAL execution timing, for the GNN delay label
    (graph/labels.py) — and ONLY for the label, never for node features.

    Deliberately unfiltered on execution_status, unlike
    fetch_current_shop_schedule() above and for the identical reason
    fetch_operation_assignments() is unfiltered: any row-set restriction
    keyed to execution_status (or to whether an operation has "started" as of
    the seeder's now_time snapshot) is exactly the leak this replaces. The
    label this feeds is genuine plan-vs-actual lateness, computed the same
    way whether the operation is Done, Running, or still Scheduled — the
    seeder already authored a complete synthetic actual outcome for every
    lot regardless of where the live dashboard's "now" pointer sits, so the
    label has no reason to be blind to any of it. See PROJECT_STATUS.txt
    Section 0D and data_generation/seed_database_fixed.py's
    sample_execution_variance_multiplier.

    actual_start_time/actual_end_time are NULL for any database seeded
    before this column existed; callers must handle that (missing actual
    timing means "no lateness evidence", not "on time").
    """
    logger.info("Fetching per-lot planned-vs-actual timing for the delay label...")
    query = """
        SELECT
            production_order_id,
            operation_id,
            planned_start_time,
            planned_end_time,
            actual_start_time,
            actual_end_time
        FROM Job_Shop_Schedules;
    """
    return pd.read_sql(query, get_engine())

def fetch_concurrent_operator_capacity() -> int:
    """
    How many operators are on duty AT ANY ONE MOMENT — the correct denominator
    for a labour utilisation rate.

    The factory runs three back-to-back 8-hour shifts covering the full 24
    hours. Only one shift is on the floor at a time, so the whole-day roster
    (~8,302) overstates concurrent labour capacity by 3x. Using the roster made
    the utilisation ceiling look like 2,790 machines / 8,302 people = 33.6%,
    and produced the alarming but meaningless finding that "5,512 operators
    have nothing to work on" — they were simply the two shifts not currently at
    work.

    The two populations are counted differently, because their availability
    comes from different places (see fetch_assignable_operators):

    - CREW MEMBERS follow their batch. A crew works exactly one of the three
      bands in any week and rests one day in seven, so the expected fraction
      of crew headcount on the floor at a random moment is (1/3) x (6/7).
    - FLOATERS follow Work_Schedules, one fixed band each, so the expected
      fraction is 1/number-of-bands.

    Band and rest-day counts are read from the data rather than assumed, so an
    uneven roster or a change to two shifts is followed rather than silently
    mis-modelled.
    """
    engine = get_engine()

    crew_sql = """
        SELECT COUNT(DISTINCT bm.employee_id) AS n
        FROM Batch_Members bm
        JOIN Employees e ON e.employee_id = bm.employee_id
        JOIN Work_Schedules ws ON ws.employee_id = e.employee_id
        WHERE e.is_active = TRUE AND ws.is_on_leave = FALSE
          AND ws.shift_date = (SELECT MAX(shift_date) FROM Work_Schedules);
    """
    floater_sql = """
        SELECT ws.start_time, ws.end_time, COUNT(DISTINCT e.employee_id) AS n
        FROM Employees e
        JOIN Work_Schedules ws ON ws.employee_id = e.employee_id
        LEFT JOIN Batch_Members bm ON bm.employee_id = e.employee_id
        WHERE e.is_active = TRUE AND ws.is_on_leave = FALSE
          AND bm.employee_id IS NULL
          AND ws.shift_date = (SELECT MAX(shift_date) FROM Work_Schedules)
        GROUP BY ws.start_time, ws.end_time;
    """
    crew_total = int(pd.read_sql(crew_sql, engine).iloc[0]['n'])
    floaters = pd.read_sql(floater_sql, engine)
    floater_total = int(floaters['n'].sum()) if not floaters.empty else 0
    bands = max(1, len(floaters)) if not floaters.empty else NUM_SHIFTS

    crew_concurrent = crew_total * (1.0 / NUM_SHIFTS) * ((DAYS_PER_WEEK - 1) / DAYS_PER_WEEK)
    floater_concurrent = floater_total / bands
    concurrent = int(round(crew_concurrent + floater_concurrent))

    logger.info(
        f"Roster: {crew_total} crew members (rotating, 1 rest day in "
        f"{DAYS_PER_WEEK}) + {floater_total} floaters across {bands} fixed band(s) "
        f"-> ~{concurrent} on duty at any moment "
        f"({crew_concurrent:.0f} crew + {floater_concurrent:.0f} floater)."
    )
    return concurrent


def fetch_present_operator_count() -> int:
    """
    Total operators rostered today across ALL shifts, not concurrently.

    Kept for reporting headcount. Do NOT use as a utilisation denominator —
    see fetch_concurrent_operator_capacity() for why that overstates capacity
    by the number of shifts.
    """
    query = """
        SELECT COUNT(DISTINCT e.employee_id) AS n
        FROM Employees e
        JOIN Work_Schedules ws ON ws.employee_id = e.employee_id
        WHERE e.is_active = TRUE
          AND ws.is_on_leave = FALSE
          AND ws.shift_date = (SELECT MAX(shift_date) FROM Work_Schedules);
    """
    df = pd.read_sql(query, get_engine())
    return int(df.iloc[0]['n']) if not df.empty else 0


def apply_planning_horizon(df_routings, horizon_orders: int = None):
    """
    Restricts a routings frame to the earliest-released N orders.

    WHY A HORIZON AT ALL: the GA optimises a chromosome of 3 x num_operations
    genes against a population of 50. The full order book is 6,640 operations,
    i.e. ~19,900 genes, and at that size the GA only just edges past a plain
    FIFO dispatch rule — the search cannot cover the space. A bounded window is
    also how factories actually plan: you release work in batches, you do not
    re-plan the entire book every morning.

    ORDERED BY RELEASE DATE, NOT DEADLINE. A "next N days of deadlines" window
    would select nothing here: every deadline sits 14-45 days out, so a
    fortnight's window is empty and a six-week one is the whole book. Release
    date is the natural ordering for a shop floor anyway — you schedule what
    has been released to you.

    horizon_orders=None returns everything, so the full-book behaviour is
    unchanged unless a horizon is asked for.
    """
    if horizon_orders is None or df_routings.empty:
        return df_routings

    order_release = (
        df_routings.groupby('production_order_id')['release_date'].min().reset_index()
    )
    # Stable tiebreak on order id so the window is deterministic when several
    # orders share a release date.
    order_release = order_release.sort_values(['release_date', 'production_order_id'])
    keep = set(order_release['production_order_id'].head(int(horizon_orders)))
    windowed = df_routings[df_routings['production_order_id'].isin(keep)].copy()

    logger.info(
        f"Planning horizon: {len(keep)} of {df_routings['production_order_id'].nunique()} "
        f"orders ({len(windowed)} of {len(df_routings)} operations) by earliest release date."
    )
    return windowed


def fetch_line_factory_map() -> dict:
    """
    line_id -> factory_id, for every active line, via Production_Lines ->
    Departments.

    This is authoritative and complete. It replaces inferring the mapping from
    the factory_id of operators who happen to belong to a batch on that line,
    which only covered lines that HAD a crew: after batches became shift-pure
    rotating crews, only 46 of 99 lines had one, so 53 lines had no factory and
    silently skipped the factory tier of operator sourcing entirely, jumping
    straight to company-wide. That accounted for all 3,507 'anywhere'
    assignments in a measured run.
    """
    query = """
        SELECT pl.line_id, d.factory_id
        FROM Production_Lines pl
        JOIN Departments d ON d.department_id = pl.department_id
        WHERE pl.is_active = 1;
    """
    df = pd.read_sql(query, get_engine())
    return {int(r.line_id): int(r.factory_id) for r in df.itertuples(index=False)}


def fetch_assignable_operators() -> pd.DataFrame:
    """
    Operators who can actually be put on an operation, with everything needed
    to pick one: the skill they hold, the line whose batch they belong to, and
    their factory.

    Returns one row per (employee, operation_type) skill pairing, so an
    operator skilled in two categories appears twice — callers index by skill.

    line_id is the line of the employee's ACTIVE BATCH, which is how the
    seeder assigns work and how this factory actually staffs a line: an
    operator belongs to a batch, and a batch belongs to a line. It is NULL for
    operators not in any batch (roughly half of them), which is why callers
    need the fallback tiers described in
    agent/decision_engine.py::_assign_operators.

    AVAILABILITY HAS TWO SOURCES, depending on whether the operator is in a
    crew:

    - Batch members: rotation_offset and rest_day_offset are authoritative.
      The crew moves between the three shift bands weekly and takes a
      staggered weekly rest day, both derived in utils/shift_rotation.py.
      Their shift_start/shift_end are returned but must be IGNORED — a static
      row cannot represent a rotating calendar, so it will disagree with where
      the crew actually is in any week but one.
    - Floaters (batch_id NULL): no crew to follow, so shift_start/shift_end
      from Work_Schedules remain authoritative.

    See agent/decision_engine.py::_available_at, which implements exactly this
    split.
    """
    logger.info("Fetching assignable operators (skill x batch x line x shift)...")
    query = """
        SELECT
            e.employee_id,
            e.factory_id,
            osm.operation_type,
            osm.efficiency_rating,
            eb.line_id,
            eb.batch_id,
            eb.rotation_offset,
            eb.rest_day_offset,
            ws.start_time AS shift_start,
            ws.end_time   AS shift_end
        FROM Employees e
        JOIN Work_Schedules ws ON ws.employee_id = e.employee_id
        JOIN Operator_Skill_Matrix osm ON osm.employee_id = e.employee_id
        LEFT JOIN Batch_Members bm ON bm.employee_id = e.employee_id
        LEFT JOIN Employee_Batches eb ON eb.batch_id = bm.batch_id AND eb.is_active = TRUE
        WHERE e.is_active = TRUE
          AND ws.is_on_leave = FALSE
          AND ws.shift_date = (SELECT MAX(shift_date) FROM Work_Schedules);
    """
    return pd.read_sql(query, get_engine())


def fetch_inventory_state() -> pd.DataFrame:
    """
    Extracts real-time stock levels for fabrics and accessories.
    Purpose: Feeds the RAG-Enabled Conversational Stock Intelligence Agent.

    NOTE: this previously queried a table named `Inventory` with columns
    sku_id / item_name / category / current_stock / reorder_threshold /
    unit_of_measure / is_active. No such table exists in this schema and none
    of those columns do either — the seeder creates `Inventory_Stock`
    (stock_id, material_id, supplier_id, current_quantity, warehouse_location,
    last_restocked_date) joined to `Raw_Materials` for names. The old query
    therefore failed on every call and was silently swallowed by the except
    below, so this function always returned an empty DataFrame. Rewritten
    against the real schema; the returned column NAMES are kept as-is so any
    future RAG consumer keeps a stable contract.

    `reorder_threshold` has no equivalent column in this schema, so it is
    returned as NULL rather than invented.
    """
    logger.info("Fetching real-time inventory state for RAG Agent...")
    query = """
        SELECT
            ist.stock_id            AS sku_id,
            rm.name                 AS item_name,
            rm.material_type        AS category,
            ist.current_quantity    AS current_stock,
            NULL                    AS reorder_threshold,
            rm.unit_of_measurement  AS unit_of_measure
        FROM Inventory_Stock ist
        JOIN Raw_Materials rm ON rm.material_id = ist.material_id;
    """
    # SILENT-DEFAULT FIX (audit #5c). This used to catch EVERY exception,
    # warn, and return an empty DataFrame -- and this function's own docstring
    # above records that exact handler hiding a total failure for an unknown
    # length of time: the query named a table and columns that do not exist in
    # this schema, so it raised on every single call and the caller saw a
    # clean, empty, plausible result. The query was fixed; the handler that
    # concealed it was not. A broken query, a dropped table or a lost
    # connection are all real failures and now propagate.
    return pd.read_sql(query, get_engine())

if __name__ == "__main__":
    # Diagnostic test to ensure queries execute cleanly and data is formatted correctly
    # before integrating this into the PyTorch layers or the web portal.
    print("Testing Data Extraction Pipeline...")
    
    try:
        machines_df = fetch_machine_state()
        print(f"[OK] Extracted {len(machines_df)} machine capabilities.")
        
        workforce_df = fetch_worker_state()
        print(f"[OK] Extracted {len(workforce_df)} operator skill records.")
        
        routings_df = fetch_active_orders_and_routings()
        print(f"[OK] Extracted {len(routings_df)} active routing operations.")
        
        schedule_df = fetch_current_shop_schedule()
        print(f"[OK] Extracted {len(schedule_df)} active schedule blocks.")
        
        inventory_df = fetch_inventory_state()
        print(f"[OK] Extracted {len(inventory_df)} inventory SKUs for RAG Agent.")
        
    except Exception as e:
        print(f"[FAIL] Database Extraction Failed: {e}")

# Reservation classification (P1). A committed interval is either work that
# has genuinely begun and therefore cannot be moved, or work that is merely
# planned and is ABOUT TO BE RESCHEDULED by this very run.
RESERVATION_FIXED = 'fixed'
RESERVATION_REPLACEABLE = 'replaceable'


class ReservationRecord(NamedTuple):
    """A committed interval WITH the identity needed to reason about it.

    The old loader returned only (employee, start, end). Discarding schedule
    and order identity made the central defect unrepairable downstream: the
    exporter reserved workers for the OLD plan while trying to place the
    replacement of that same work, and nothing had enough information to
    tell the two apart. Every field below exists to make that decidable.
    """
    employee_id: str
    start: object
    end: object
    schedule_item_id: int
    production_order_id: str
    operation_id: str
    asset_id: object
    execution_status: str
    classification: str
    segments: tuple = ()


class MachineReservationRecord(NamedTuple):
    """A fixed/replaceable machine interval with its last-order identity."""
    asset_id: int
    start: object
    end: object
    schedule_item_id: int
    production_order_id: str
    operation_id: str
    execution_status: str
    classification: str


def classify_reservation(planned_start, execution_status, now):
    """
    FIXED if the work is under way at `now`; REPLACEABLE if merely planned.

    The boundary comes from the seeder's own status derivation
    (data_generation/seed_database_fixed.py):

        if   actual_end   <= now_time: 'Done'
        elif actual_start >  now_time: 'Scheduled'     <- NOT STARTED
        else:                          'Running'/'Delayed'

    so 'Scheduled' means, by construction, that nothing has begun.

    BUT the stored label is frozen at SEED time and this project reschedules
    at a LATER origin -- measured on the authoritative snapshot, the seeder's
    now_time is 2026-09-09 23:59:59 while the scheduling origin is
    2026-09-11 12:39:33, and 44 rows carry 'Scheduled' despite having started
    before that origin. Trusting the label alone would free 44 genuinely
    in-flight intervals. So the label is used as ONE sufficient condition and
    the planned interval straddling `now` as another, and their union decides.

    actual_start_time is deliberately NOT consulted: the seeder writes actuals
    for every row of its historical simulation, so it is non-null on all
    143,456 reservation rows and would classify 100% of them as fixed.
    """
    from utils.schedule_status import IN_FLIGHT_STATUSES
    if execution_status in IN_FLIGHT_STATUSES:
        return RESERVATION_FIXED
    if planned_start is not None and not pd.isna(planned_start):
        if pd.Timestamp(planned_start).to_pydatetime() < now:
            return RESERVATION_FIXED
    return RESERVATION_REPLACEABLE


def fetch_operator_reservation_records(now):
    """Every committed worker interval at `now`, with identity and class."""
    from sqlalchemy import text, bindparam
    from utils.schedule_status import OCCUPYING_STATUSES
    query = text("""SELECT schedule_item_id, production_order_id, operation_id,
            asset_id, employee_id, planned_start_time, planned_end_time,
            planned_work_segments, execution_status
        FROM Job_Shop_Schedules WHERE employee_id IS NOT NULL
        AND execution_status IN :statuses AND planned_end_time > :now
        ORDER BY employee_id, planned_start_time""").bindparams(bindparam('statuses', expanding=True))
    rows = pd.read_sql(query, get_engine(), params={'statuses': OCCUPYING_STATUSES, 'now': now})
    records = []
    for row in rows.itertuples(index=False):
        if pd.isna(row.planned_start_time) or row.planned_end_time <= row.planned_start_time:
            raise ValueError(f'Invalid committed worker reservation for employee {row.employee_id}')
        raw_segments = row.planned_work_segments
        if raw_segments is None or pd.isna(raw_segments):
            raise ValueError(
                f"Missing planned worker segments for schedule item "
                f"{row.schedule_item_id}"
            )
        try:
            decoded = json.loads(raw_segments) if isinstance(
                raw_segments, str) else raw_segments
            segments = []
            planned_start = pd.Timestamp(
                row.planned_start_time).to_pydatetime()
            planned_end = pd.Timestamp(row.planned_end_time).to_pydatetime()
            for pair in decoded:
                seg_start = pd.Timestamp(pair[0]).to_pydatetime()
                seg_end = pd.Timestamp(pair[1]).to_pydatetime()
                if (
                    seg_end <= seg_start
                    or seg_start < planned_start
                    or seg_end > planned_end
                ):
                    raise ValueError("segment outside planned operation span")
                future_start = max(now, seg_start)
                if seg_end > future_start:
                    segments.append((future_start, seg_end))
        except (TypeError, ValueError, KeyError, IndexError) as exc:
            raise ValueError(
                f"Invalid planned worker segments for schedule item "
                f"{row.schedule_item_id}: {exc}"
            ) from exc
        if not segments:
            raise ValueError(
                f"No remaining worker segment for active schedule item "
                f"{row.schedule_item_id}"
            )
        records.append(ReservationRecord(
            employee_id=str(row.employee_id),
            start=segments[0][0],
            end=segments[-1][1],
            schedule_item_id=int(row.schedule_item_id),
            production_order_id=str(row.production_order_id),
            operation_id=str(row.operation_id),
            asset_id=row.asset_id,
            execution_status=str(row.execution_status),
            classification=classify_reservation(
                row.planned_start_time, str(row.execution_status), now),
            segments=tuple(segments)))
    return records


def reservation_composition(records):
    """Rows / orders / worker-minutes per class, for reporting."""
    out = {}
    for cls in (RESERVATION_FIXED, RESERVATION_REPLACEABLE):
        sel = [r for r in records if r.classification == cls]
        out[cls] = {
            'rows': len(sel),
            'orders': len({r.production_order_id for r in sel}),
            'operations': len({(r.production_order_id, r.operation_id) for r in sel}),
            'workers': len({r.employee_id for r in sel}),
            'worker_minutes': sum((e - a).total_seconds() / 60.0 for r in sel for a, e in r.segments),
        }
    out['total'] = {
        'rows': len(records),
        'orders': len({r.production_order_id for r in records}),
        'operations': len({(r.production_order_id, r.operation_id) for r in records}),
        'workers': len({r.employee_id for r in records}),
        'worker_minutes': sum((e - a).total_seconds() / 60.0 for r in records for a, e in r.segments),
    }
    return out


def fetch_committed_operator_reservations(now, keep=RESERVATION_FIXED):
    """
    {employee_id: [(start, end)]} for the intervals that must be RESPECTED.

    keep=RESERVATION_FIXED (the default, policy (a) "replace movable future
    work") retains only work already under way. Merely-planned intervals are
    dropped, because this run is rescheduling exactly that work: holding a
    worker for the old placement while trying to staff its replacement
    double-books the same labour against itself.

    WHY (a) AND NOT (b) "keep committed work fixed": machine state follows
    the same classifier through fetch_committed_machine_state(), and routing
    extraction re-offers the same work as remaining demand via remaining_frac.
    Measured on the authoritative snapshot, 100% of reserved
    (order, operation) pairs -- 7,349 of 7,349 -- also appear as non-Done
    remaining work. So (a) aligns the worker side with the two components
    that already agree; (b) would require inventing committed machine
    occupancy AND excluding 7,349 of 7,548 operation-pairs from demand,
    leaving ~199 to schedule and making the comparison vacuous.

    keep=None returns every interval (the pre-fix behaviour), kept only so a
    measurement can quantify the artefact rather than having to re-derive it.
    """
    records = fetch_operator_reservation_records(now)
    comp = reservation_composition(records)
    logger.info(
        "Committed worker reservations at %s: %d rows total "
        "(%d fixed / %d replaceable; %.0f / %.0f worker-minutes). keep=%s",
        now, comp['total']['rows'], comp[RESERVATION_FIXED]['rows'],
        comp[RESERVATION_REPLACEABLE]['rows'],
        comp[RESERVATION_FIXED]['worker_minutes'],
        comp[RESERVATION_REPLACEABLE]['worker_minutes'], keep)
    result = {}
    for r in records:
        if keep is not None and r.classification != keep:
            continue
        result.setdefault(r.employee_id, []).extend(r.segments)
    return result


def fetch_machine_reservation_records(now):
    """Every occupying machine interval at now, independent of staffing.

    This deliberately does not reuse fetch_operator_reservation_records().
    A machine remains occupied even when its schedule row has no employee;
    deriving machine state through WHERE employee_id IS NOT NULL silently
    released precisely that case.
    """
    from sqlalchemy import text, bindparam
    from utils.schedule_status import OCCUPYING_STATUSES
    query = text("""SELECT schedule_item_id, production_order_id, operation_id,
            asset_id, planned_start_time, planned_end_time, execution_status
        FROM Job_Shop_Schedules WHERE asset_id IS NOT NULL
        AND execution_status IN :statuses AND planned_end_time > :now
        ORDER BY asset_id, planned_start_time, schedule_item_id""").bindparams(
            bindparam('statuses', expanding=True))
    rows = pd.read_sql(
        query, get_engine(), params={'statuses': OCCUPYING_STATUSES, 'now': now})
    records = []
    for row in rows.itertuples(index=False):
        if pd.isna(row.planned_start_time) or row.planned_end_time <= row.planned_start_time:
            raise ValueError(
                f'Invalid committed machine reservation for asset {row.asset_id}')
        records.append(MachineReservationRecord(
            asset_id=int(row.asset_id),
            start=max(now, pd.Timestamp(row.planned_start_time).to_pydatetime()),
            end=pd.Timestamp(row.planned_end_time).to_pydatetime(),
            schedule_item_id=int(row.schedule_item_id),
            production_order_id=str(row.production_order_id),
            operation_id=str(row.operation_id),
            execution_status=str(row.execution_status),
            classification=classify_reservation(
                row.planned_start_time, str(row.execution_status), now),
        ))
    return records


def fetch_committed_machine_state(now, keep=RESERVATION_FIXED):
    """Return (busy_until_minutes, last_order) for committed machines.

    For an asset with several retained rows, the row ending last determines
    both values. Keeping them paired matters: the decoder applies changeover
    based on the order that actually leaves the machine last.
    """
    free = {}
    last_order = {}
    latest_end = {}
    for record in fetch_machine_reservation_records(now):
        if keep is not None and record.classification != keep:
            continue
        if record.end > latest_end.get(record.asset_id, now):
            latest_end[record.asset_id] = record.end
            free[record.asset_id] = max(
                0.0, (record.end - now).total_seconds() / 60.0)
            last_order[record.asset_id] = record.production_order_id
    return free, last_order


def fetch_committed_machine_reservations(now, keep=RESERVATION_FIXED):
    """
    {asset_id: minutes_from_now_until_free} for machines holding FIXED work.

    Exists for CONSISTENCY, which is the whole point of this fix. Machine
    state previously started completely idle while worker state started fully
    booked -- the same physical operation was simultaneously free on its
    machine, booked on its worker, and pending as demand. Under policy (a)
    both resources release merely-planned work and both hold work already
    under way.

    Returns minutes-from-now to match ga/fitness.py's machine_free
    representation ("minutes-from-now when it next frees up"), taking the
    latest fixed end per asset.
    """
    free, _last_order = fetch_committed_machine_state(now, keep=keep)
    return free
