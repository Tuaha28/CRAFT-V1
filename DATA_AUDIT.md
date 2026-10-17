# Data Foundation Audit — Phase 1 (Audit Only, No Code Changes)

**Purpose.** Catalog every table `data_generation/seed_database_fixed.py`
generates, field by field: what it currently does, what real source could
replace it, how completely, and what breaks if it changes shape. This is
the map for deciding what to rebuild and in what order — nothing in this
document changes any code or reseeds the database.

**Method.** Read the seeder's full generation logic for every table (not
inferred from schema alone), cross-checked against `database/queries.py`
and the rest of the live pipeline (`graph/`, `ga/`, `agent/`, `models/`,
`frontend/`) for what actually consumes each table's output, and against
the four real files' actual columns (re-verified, not assumed from memory).

**Real file inventory** (`data_generation/`, all four already loadable):

| File | Rows x Cols | Columns | Currently read by seeder |
|---|---|---|---|
| `WorkStation.xlsx` | 141 x 16 | ID, Name, DisplayName, Department, CompanyId, BrId, LineId, LineName, LayoutType, LineType, **Worker Capacity**, **WorkHour**, PhotoBytes, ActiveDate, DeActiveDate, IsEnabled | `ID`, `LineName`, `Department`, `IsEnabled`, `Worker Capacity` (as of the routing-fix session). **Not read**: `WorkHour`, `LayoutType`, `LineType`, `DisplayName`, `CompanyId`, `BrId`, `ActiveDate`/`DeActiveDate` |
| `grid.xls` | 5,132 x 22 | Company, Line, BuyMonth, **StyleId**, PO, Color, Size, **GarmentType**, Group, **Layout**, PlanTarget, BalanceQty, **StartDate**, EndDate, ETADate, ExFtyDate, PPCExFty, ETADiff, ExFtyDiff, Count, Revision | `StyleId`, `GarmentType` (weighted, as of routing-fix). **Not read**: `StartDate`/`EndDate`/`ETADate` (real release-date shape — Section 1.2.2's still-open gap), `Layout` (too granular to be a routing template, established in 1.2.1), `PlanTarget`/`BalanceQty` (real quantities), `Line`, `Group` |
| `Plan.xlsx` | 1,382 x 64 | CompanyId, BrId, Line, LineId, LineName, BuyMonth, StyleId, OrId, Season, SrNo, PO, OldPO, POType, ColorCode, ColorDesc, ItemSize, SplitQtyCount, **OrderQty**, POrderQty, OutPutQty, **BalanceQty**, SplitQty, ShipType, **OrderedDate**, PlanETADate, **ETADate**, PlanExFactoryDate, **ExFactoryDate**, PPCExFactoryDate, PPCETADate, LayoutDocNo, LayoutType, **GarmentType**, GroupNo, **HourlyTarget**, PlanedTarget, IntPlanedTarget, **WorkHour**, **DayTarget**, ChangeOverDay, Efficency, Carder, **GarmentSmv**, RGBColor, Post, CloseStatus, ..., **StartTime**, **EndTime**, EndDateTime, ... | `GarmentType`, `GarmentSmv` (as of routing-fix). **Not read**: `OrderQty`/`BalanceQty` (real order quantities), `OrderedDate`/`ETADate`/`ExFactoryDate` (real date fields — a SECOND, order-level source for the release-date fix beyond grid.xls), `HourlyTarget`/`DayTarget`/`WorkHour` (real per-line throughput targets — see Job_Shop_Schedules below), `LineId`/`Line` (real product-to-line assignment history), `Efficency` (real line efficiency, vs. our hardcoded 85%) |
| `PPC-1.xlsx` | 4,458 x 20 | SrNo, **Date**, CompanyId, Day, BrId, **Line**, **LineId**, **GarmentType**, BuyMonth, ETADate, Style, PO, ColorCode, ItemSize, **Output**, Qty@103, **PlanedQty**, **TotalQty**, HandOverDate, **WorkHrs** | **Not read at all.** This is real DAILY per-line production output — the richest untapped file. See Job_Shop_Schedules below: it could directly ground the (currently entirely invented) duration/throughput formula, and it's the source of the "only 14-15 of 99 lines producing per day" concurrency fact from Section 1.2.5. |

---

## Table-by-table audit

Grouped by generation stage, matching the seeder's own section numbering.
Bucket key: **(a)** replaceable with real data now, **(b)** derivable from
literature/reasoning with citation (no direct real source), **(c)** no
source available in the four files — must stay synthetic, documented as
such.

### Group 1 — Org/line structure (seeder Section 2)

#### `Companies`
- **Generates:** 1 hardcoded row ("Needle Craft Ltd", a fabricated
  registration number and address).
- **Real source:** none of the four files name the company or give a
  registration number/address. This is scene-setting, not modeled data.
- **Buckets:** name — **(c)**, fictional but harmless (never read
  downstream by any consumer besides Factories' FK). registration_number/
  address — **(c)**, pure flavor text.
- **Dependency:** root table; Factories FKs to it. Changing/removing rows
  here is low-risk — nothing computes off company identity.

#### `Factories`
- **Generates:** one row per distinct `WorkStation.xlsx` `Department`
  value, name = `"{Department} Branch"`, address fabricated (Faker-style
  fixed string, not from Faker itself).
- **Real source:** `WorkStation.xlsx.Department` — **already used**,
  directly, for factory identity. The fabricated address text is the only
  invented part.
- **Buckets:** factory existence/grouping — **(a)**, already real. address
  string — **(c)**.
- **Dependency:** everything with a `factory_id` (Departments,
  Machinery_Assets via line, Employees) traces back here. Stable; this
  table already reflects real data and isn't a rebuild target.

#### `Departments`
- **Generates:** one row per (factory, WorkStation.xlsx row's Department),
  name = `"{Department} Floor"`, `floor_number` hardcoded to 1.
- **Real source:** derived 1:1 from the same `WorkStation.xlsx.Department`
  column as Factories — effectively a duplicate grouping, not independent
  data.
- **Buckets:** existence/naming — **(a)** (real, if redundant with
  Factories). `floor_number` — **(c)**, no real source, always 1.
- **Dependency:** Production_Lines FKs here. Low-risk to touch.

#### `Production_Lines`
- **Generates:** one row per `WorkStation.xlsx` enabled row (`IsEnabled ==
  1`, 99 of 141): `line_id` = real `ID`, `line_number` = real `LineName`,
  `target_efficiency_percentage` = **hardcoded 85.00 for every line**,
  `is_active` = **hardcoded True for every line**.
- **Real source:** `WorkStation.xlsx` maps almost directly — `ID`,
  `LineName`, `Department` are already used. **Not yet used**: `WorkHour`
  (Section 1.2.3's still-deferred shift-count fix), `LayoutType` (P/H/W),
  `LineType` (E/L) — both real, uninterpreted categorical fields with no
  established meaning yet (worth investigating: `LineType` E/L is a
  plausible candidate for "which garment-type family this line normally
  runs," which would let Production_Lines constrain routing/product
  assignment realistically instead of the current fully-decoupled random
  line assignment at Job_Shop_Schedules time). `Plan.xlsx.Efficency` is a
  second real source for per-line efficiency the current hardcoded 85%
  ignores entirely.
- **Buckets:** line existence/identity/department — **(a)**, already real.
  `target_efficiency_percentage` — **(a) available, not yet used**
  (`Plan.xlsx.Efficency`). `is_active` (always True) — **(a) available,
  not yet used** (`WorkStation.xlsx.IsEnabled` is already the *filter*
  used to select which 99 rows become lines at all, but the *seeded*
  `is_active` flag is a separate hardcoded constant that could instead
  read `ActiveDate`/`DeActiveDate` for a genuinely time-varying signal —
  currently unused). `LayoutType`/`LineType` — **(a) available, uninterpreted**.
- **Dependency — HIGH.** Confirmed by direct reading, not assumed: line_id
  is a foreign key baked into Machinery_Assets (line assignment),
  employee_batches (per-line crew formation), and indirectly into every
  Job_Shop_Schedules row (`asset_to_line_map`, `line_to_batches`) via
  those two. This is the single most upstream real-data-native table in
  the schema — see rebuild-order recommendation below.

### Group 2 — Products & routing (seeder Section 3)

#### `Products`
- **Generates:** one row per real `grid.xls.StyleId`, `category` =
  GarmentType, now **weighted** by real per-category frequency (routing-fix
  session), name = fabricated "NeedleCraft Elite {cat} Style {style}".
- **Real source:** `StyleId` and `GarmentType` (weighted) are both real,
  already used. Fabricated: the display `name` string only.
- **Buckets:** style_code, category — **(a)**, already real. name — **(c)**,
  cosmetic.
- **Dependency:** Product_Variants, Routing_Operations, Bill_of_Materials,
  Production_Orders all FK to product_id.

#### `Product_Variants`
- **Generates:** every product x 4 sizes x 4 colors (16 variants/product,
  fixed), SKU string built from style/size/color.
- **Real source:** `grid.xls` has `Size`/`Color` columns per real row —
  **not read**. The current 4x4 grid is an invented cross-product, not
  derived from which size/color combinations actually occurred per style.
- **Buckets:** size/color vocabulary and their real per-style combinations
  — **(a) available, not yet used**. The fixed 4x4 assumption itself —
  **(c)**, would need replacing with the real per-style variant list.
- **Dependency:** LOW. Confirmed by grep: `variant_id`/Product_Variants is
  not read by `database/queries.py` or any of `graph/`, `ga/`, `agent/`,
  `models/`. Only feeds table existence, not the pipeline. Low rebuild
  priority regardless of bucket.

#### `Routing_Operations`
- **Generates (as of the routing-fix session):** literature-derived
  per-garment-type routing (`data_generation/routing_tables.py`, 6-10
  steps), SAM split evenly from real `GarmentSmv` (`Plan.xlsx`) across that
  routing's own steps.
- **Real source:** garment-type selection and total SAM are now real.
  **Not real**: the exact operation SEQUENCE and machine-type assignment
  within each group — those are `routing_spec.md`'s literature citations
  (bucket b), explicitly not the company's own process sheets (unavailable,
  per `routing_spec.md`'s own stated limitation).
- **Buckets:** garment-type-to-total-SAM mapping — **(a)**, real. routing
  STRUCTURE (step sequence, machine assignment) — **(b)**, cited
  literature, cross-validated against real SMV magnitude/ordering but not
  the company's own process. SAM-per-step SPLIT (equal division) — **(b)**,
  a documented modeling choice, not a real per-operation SMV breakdown
  (none of the four files contain one — see Section 1.2.1's finding that
  `LayoutDocNo`/`Layout` are per-run document references, not embedded
  process data).
- **Dependency — CONFIRMED SAFE, already exercised.** Variable step count
  is now live in production and was verified (before the routing-fix
  reseed) that no pipeline code assumes a fixed length. This precedent
  directly informs the rebuild-order question below: routing/schema shape
  changes are now a KNOWN-SAFE class of change in this codebase, not a
  ground-up risk.

#### `Bill_of_Materials`
- **Generates:** every product x 3 fixed material_ids (fabric/thread/label),
  `required_quantity` = `random.uniform(1.2, 2.5)` (fabric) or
  `random.uniform(0.02, 0.08)` (thread/label), `wastage_allowance_percentage`
  hardcoded 5.00.
- **Real source:** none of the four files contain a bill-of-materials or
  fabric-consumption-per-unit figure. Fully invented.
- **Buckets:** all fields — **(c)**.
- **Dependency:** LOW. Not read by `database/queries.py` or the ML/GA
  pipeline — only consumed inside the seeder itself (to decrement
  `Inventory_Stock`, itself also unread downstream). Confirmed via grep:
  zero consumers outside `data_generation/`.

### Group 3 — Supply chain (seeder Section 6) — entirely decorative

#### `Suppliers`, `Raw_Materials`, `Inventory_Stock`
- **Generate:** 3 hardcoded suppliers, 5 hardcoded materials, and a
  5-lots-per-(material,supplier) inventory grid with `random.uniform`
  quantities, decremented by BOM consumption during order generation.
- **Real source:** none of the four files touch supply chain/inventory at
  all. Fully invented, including the supplier names/emails (fabricated,
  not Faker).
- **Buckets:** all fields — **(c)**.
- **Dependency:** LOWEST priority in the whole schema. Confirmed via grep:
  **zero consumers anywhere outside the seeder** — not `database/queries.py`,
  not `graph/`/`ga/`/`agent/`/`models/`, not `frontend/dashboard.py`. These
  three tables plus the BOM table above exist for schema completeness
  only; no measured pipeline outcome depends on any field in them. Not
  worth rebuild effort under this project's stated goal (fidelity where it
  affects measured outcomes) unless the audit's scope is meant to include
  presentation-layer completeness for its own sake — worth confirming with
  you before investing here.

### Group 4 — Workforce (seeder Sections 4, 4.5)

#### `Employees`
- **Generates:** 9,000 rows, `employee_id` 1-9000, `factory_id` = **uniform
  random** choice among factories (not weighted by anything real),
  `first_name`/`last_name` via Faker, `role` = 8,500 Operators + 500
  Line_Supervisor/Mechanic (invented split), `hire_date` = random 30-730
  days ago (invented), `is_active` always True.
- **Real source:** none of the four files contain individual employee
  records (names, roles, hire dates) — confirms your expectation, this
  stays bucket (c) for identity. BUT the **aggregate headcount** (9,000,
  currently an arbitrary constant) is NOT unconstrained: summing real
  `WorkStation.xlsx.Worker Capacity` across all 99 enabled lines x a real
  shift count would give a real, defensible total headcount target,
  distinct from inventing individual people. Worth flagging as a partial
  exception to the expected (c): the COUNT can be grounded, the ROSTER
  cannot.
- **Buckets:** individual identity/name/hire_date/role-per-person — **(c)**,
  confirmed no real source, as you expected. **Aggregate headcount** —
  **(a) available, not yet used** (derivable from Worker Capacity sums).
  `factory_id` assignment (currently uniform-random) — **(c)**, though it
  COULD be weighted by real per-factory line-capacity totals instead of
  uniform, a cheap partial improvement.
- **Dependency:** Work_Schedules, Operator_Skill_Matrix, employee_batches
  (via the operator pool), and every Job_Shop_Schedules row's
  `employee_id` trace back here. Changing employee COUNT (not identity)
  is low-risk structurally — nothing keys off a specific employee_id
  numerically meaning anything.

#### `Work_Schedules`
- **Generates:** one row per employee, `shift_date` = **today only**
  (single date, always — the table has never held more than one calendar
  day across the whole roster), start/end time from a fixed 3-band
  rotation by `employee_id % 3`, `is_on_leave` = 8% random.
- **Real source:** none. The 8% leave rate and the shift-band assignment
  are both invented constants.
- **Buckets:** all fields — **(c)**. Note: this table's `shift_date =
  MAX(shift_date)` pattern in `database/queries.py` is only harmless
  (per 0H.4) because it's a single date always — a real multi-date
  calendar would need that read pattern revisited too, a cross-table
  consequence worth flagging if this table is ever made to vary by date.
- **Dependency:** read by `database/queries.py` for present-operator-count
  and skill-matrix queries (`fetch_worker_state`,
  `fetch_concurrent_operator_capacity`, `fetch_present_operator_count`,
  `fetch_assignable_operators`) — all gated on `shift_date = MAX(...)`,
  currently a no-op given the single-date reality.

#### `Operator_Skill_Matrix`
- **Generates:** each Operator employee gets 1-2 skills from the 5
  canonical operation_types, `efficiency_rating` = random uniform 70-98%.
- **Real source:** none of the four files contain per-employee skill data.
  The skill VOCABULARY (5 operation types) is inherited from the machine
  taxonomy, not independently sourced.
- **Buckets:** all fields — **(c)**, confirmed no real source, as expected.
- **Dependency:** feeds operator-assignment tiering throughout the seeder's
  own simulation loop and `agent/decision_engine.py`'s `_assign_operators`.
  Structurally stable regardless of value realism — nothing depends on
  specific efficiency numbers being real, only on the table's shape.

#### `employee_batches` / `batch_members`
- **Generates:** up to 3 crews per line (`CREWS_PER_LINE = 3`, hardcoded —
  the Section 1.2.3 shift-count gap), crew size now **real**
  (`Worker Capacity`, routing-fix session), `rotation_offset`/
  `rest_day_offset` assigned deterministically (round-robin, not random —
  this part was never invented in the "random" sense), `primary_skill` =
  random choice among the 5 types, `supervisor_id` = random choice among
  supervisors.
- **Real source:** crew SIZE is now real. Crew COUNT per line
  (`CREWS_PER_LINE = 3`) is the same invented-shift-structure gap as
  Section 1.2.3 — 84% of real lines run 2 shifts, not 3, so up to 1/3 of
  crews formed here don't correspond to a real shift band on those lines.
- **Buckets:** crew size — **(a)**, real, already used. crew count/shift
  structure — **(c)**, the known, already-assessed gap (Section 1.2.3).
  `primary_skill`/`supervisor_id` — **(c)**.
- **Dependency — this is where the shift-count gap actually bites
  structurally.** `line_to_batches`, used by the massive Job_Shop_Schedules
  simulation loop to find "the crew on duty," assumes exactly 3 rotation
  slots per line. Fixing shift count per line (Section 1.2.3) means this
  table's row count per line becomes 2 or 3, not a fixed 3 — a real,
  non-trivial structural change to the crew-formation loop AND to
  `utils/shift_rotation.py`'s rotation-offset arithmetic
  (`% NUM_SHIFTS`, currently a global constant = 3). Confirms Section
  1.2.3's original cost assessment; nothing new found that changes it.

### Group 5 — Machines (seeder Section 5)

#### `Machinery_Assets`
- **Generates:** 3,000 machines, **exactly 600 per each of the 5 canonical
  machine types (uniform, hardcoded)**, `line_id` = **uniform random**
  choice among all 99 lines (not weighted, not type-aware), `machine_model`
  = fabricated "Juki-XXXX-NN" string, `serial_number` = random UUID,
  `purchase_date` = random 100-1000 days ago, `current_status` = random
  weighted 90/5/3/2% (Active/Maintenance/Idle/Breakdown).
- **Real source:** **none of the four files contain a machine-count-by-type
  or machine-inventory table at all** — confirms your expectation exactly.
  `WorkStation.xlsx` describes LINES, not the machines on them; no column
  in any file says "line 47 has N Overlock machines."
- **Buckets:** all fields — **(c)**, confirmed, as you expected. IMPORTANT
  NUANCE: this is also the table Section 1.3's newly-discovered Body-
  Assembly imbalance traces to directly — the uniform 600/type allocation
  was already flagged as the mechanism. Since there's no real source for
  machine-count-by-type, any fix here would be bucket **(b)** at best
  (allocate proportionally to real routing-derived demand, a reasoned
  derivation, not a directly-sourced real number) — worth being precise
  that this is not the same as "replace with real data," it's "replace an
  arbitrary constant with a principled one."
- **Dependency — HIGH, same tier as Production_Lines.** `asset_to_line_map`
  is built directly from this table and used throughout the Job_Shop_
  Schedules simulation loop (`machine_heaps`, `capable_machines_by_type`).
  Rebalancing per-type counts is schema-shape-neutral (same columns, same
  row count even) — this is the CHEAPEST high-impact rebuild candidate in
  the entire schema: no downstream code depends on the count being 600,
  only on the table existing with `asset_id`/`line_id`/`operation_type`
  populated consistently.
- **`current_status` dependency status UPDATED (2026-08-25)**: was
  effectively unread by any capacity-affecting logic when this audit was
  written (`utils/machine_reserve.py::build_reserve` ignored it entirely,
  by then-documented design). As of `WLC_RELEASE_GATE_PROPOSAL.md` Section
  9's live-capacity fix, it is now a CONFIRMED READ, same tier as
  `Machine_Capabilities` above — `build_reserve`'s `usable_cap_by_type`
  (an MC-gene ceiling bound, and the intended capacity source for any
  future workload-norm gate) now subtracts currently-Breakdown/Under_
  Maintenance machines from the usable count. Still bucket **(c)** — no
  real source exists for this field in any of the four company files
  (unchanged from this entry's original finding) — but "no real consumer
  reads it" is no longer true, so if this parameter is ever revisited for
  grounding, it can no longer be deprioritized on that basis. **Flagged,
  not urgent**: the seeder's own weights (`machine_status_weights =
  [90, 5, 3, 2]`, i.e. 7% Under_Maintenance+Breakdown at initial
  assignment) plus dynamic mid-simulation breakdowns
  (`data_generation/seed_database_fixed.py`'s cumulative-runtime breakdown
  roll, persisted back to `Machinery_Assets.current_status`) combine to a
  REAL, measured 10.1% Breakdown+Under_Maintenance rate on the current
  live database (303/3,000 machines) — already above the 7%
  `MACHINE_RESERVE_FRACTION` the standby-reserve policy was sized around.
  Whether this rate should eventually be checked against real company data
  the same way other parameters in this document have been is worth
  keeping in view for a future session — genuinely blocked on the same gap
  this entry already found (no real source file contains it), so this
  would need new information from the factory, not a re-read of the
  existing four files.

#### `Machine_Capabilities`
- **Generates:** one row per machine, `operation_type` derived positionally
  from `machine_types.index(m_type)` (still valid — this list is unchanged,
  see Group 2 routing entry), `max_rpm` = random choice among
  [3500,4000,4500,5000].
- **Real source:** none. RPM figures are invented.
- **Buckets:** operation_type mapping — **(a)**, structurally tied to real
  taxonomy. `max_rpm` — **(c)**.
- **Dependency — CONFIRMED READ, not decorative.** `database/queries.py::
  fetch_machine_state()` LEFT JOINs this table directly
  (`ON ma.asset_id = mc.asset_id`), feeding `graph/node_features.py`'s
  machine node features — `operation_type` and `max_rpm` both reach the
  GNN. Unlike the Group 3/7 tables above, this one is NOT safe to
  deprioritize on "no consumer" grounds; its `max_rpm` values being
  invented is a real (if probably minor) feature-quality question worth
  keeping in view, even though it wasn't in your originally-flagged
  gap list.

#### `Machine_Status_Logs`
- **Generates:** one row per machine at seed time plus one more per dynamic
  mid-simulation breakdown, `status_changed_to` + a fixed reason string per
  status.
- **Real source:** none.
- **Buckets:** all fields — **(c)**.
- **Dependency:** LOW. Not read by `database/queries.py` or the pipeline
  (grep-confirmed); a pure audit trail.

### Group 6 — Orders (seeder Section 7)

#### `Purchase_Orders`
- **Generates:** `client_name` via Faker, `po_number` fabricated
  (`PO-JOR-NC-{n}`), `order_date` = random 5-20 days before base_date,
  `delivery_deadline` = random 30-60 days after base_date, `po_status`
  derived later from the schedule simulation's outcome.
- **Real source:** `grid.xls`/`Plan.xlsx` both have real `PO`, order-date,
  and ETA/deadline-adjacent columns (`grid.xls.PO`/`StartDate`/`ETADate`;
  `Plan.xlsx.PO`/`OrderedDate`/`ETADate`) — **not read at all** for this
  table. This is the direct mechanism behind Section 1.2.2's release-date
  finding: real dates exist in TWO files and neither is used here.
- **Buckets:** client_name — **(c)** (no real customer-name data would be
  appropriate to use even if present, for privacy — reasonable to keep
  fabricated). `order_date`/`delivery_deadline` — **(a) available, not
  yet used**, the direct fix for 1.2.2.
- **Dependency:** Production_Orders FKs here (`po_id`); `fetch_active_
  orders_and_routings()` joins `Purchase_Orders.delivery_deadline` as
  `order_deadline`, the deadline every downstream lateness metric in this
  whole project is measured against. HIGH real-world stakes, LOW
  structural risk to change (same columns, just real values instead of
  random ones).

#### `Production_Orders`
- **Generates:** `target_quantity` = random 1,500-12,000 (invented),
  `scheduled_start_date` = the ~25-30-day-spread synthetic release date
  (Section 1.2.2's finding), `status` derived from simulation outcome.
- **Real source:** `grid.xls.PlanTarget`/`BalanceQty` and
  `Plan.xlsx.OrderQty`/`BalanceQty` are real order-quantity figures — not
  read. Same for the release-date columns already noted above.
- **Buckets:** target_quantity — **(a) available, not yet used**.
  scheduled_start_date — **(a) available, not yet used** (this IS 1.2.2).
- **Dependency:** `remaining_frac` (via `Job_Shop_Schedules` join in
  `fetch_active_orders_and_routings`), every capacity-floor and on-time
  calculation in Sections 1.1-1.3 traces to this table's quantity and date
  fields. Same profile as Purchase_Orders: high stakes, low structural
  risk.

### Group 7 — Schedule execution simulation (seeder Section 8) — by far the largest and most invented table

#### `Job_Shop_Schedules`
This is the biggest table (12,677 operations x 60 lots = ~760K rows in the
current reseed) and the most heavily invented, despite being the table the
GNN labels and the live schedule floor both ultimately derive from.
- **`duration_mins`**: `int(1.3 * (stage_qty * sam) / (efficiency * 50.0))`,
  clamped [30, 480]. The constants `1.3` (DURATION_CALIBRATION_FACTOR) and
  `50.0` are **both invented**, with no cited derivation — this is the
  single largest source of invented numeric content in the whole schema,
  since it determines every lot's actual simulated timing. **Real source
  available and unused**: `PPC-1.xlsx.Output`/`PlanedQty`/`TotalQty`/`WorkHrs`
  are REAL observed daily per-line output figures — the calibration
  constants here could be fit against real throughput instead of asserted.
  This is the highest-value use of PPC-1.xlsx anywhere in the schema.
- **`asset_id`/`line_id`**: machine chosen by heap (least-loaded of its
  type) — mechanism is sound regardless of data realism, but draws from
  the uniform-600-per-type pool (Group 5's finding).
- **execution variance, breakdown probability (15% after 1,200 runtime
  minutes), leave (8%)**: all invented constants, documented with
  reasoning but no real citation. **Buckets: (c)**, though `PPC-1.xlsx`
  could in principle inform a real breakdown/downtime rate if it recorded
  reasons for shortfalls (it doesn't appear to — only Output vs Planned,
  not cause).
- **`employee_id` selection tiering (batch/factory/global)**: mechanism is
  sound; the underlying skill/availability data it draws from is Group 4's
  invented data.
- **Buckets, summary:** machine/employee SELECTION mechanism — **(a)**,
  sound and unaffected by upstream realism. Duration FORMULA and its
  constants — **(c)**, but with a real, currently-untapped calibration
  source (PPC-1.xlsx). Breakdown/leave/variance rates — **(c)**.
- **Dependency — MAXIMUM.** This table is downstream of every other table
  in the schema (Production_Orders, Routing_Operations, Machinery_Assets,
  Employees, employee_batches) and upstream of everything the ML/GA
  pipeline reads: `fetch_current_shop_schedule`, `fetch_operation_
  assignments`, `fetch_actual_vs_planned_timing`, `fetch_dispatch_context`
  all read this table directly, feeding both GNN labels and features. ANY
  upstream table change (routing length, machine allocation, employee
  count, shift structure) flows through here before it's visible anywhere
  else. This is why the routing change alone (upstream: Routing_Operations)
  already produced a 12,677-vs-7,500-operation, ~760K-row change here
  without this table's own generation code changing at all — worth
  internalizing before rebuilding anything further upstream: every
  upstream fix reseeds this table's full ~760K rows regardless of size.

#### `WIP_Tracking`, `Quality_Logs`
- **Generate:** per-lot completion/defect records, `yield_loss_pct`
  (0.5-2%, invented), defect rate scaled by operator skill (`(100 -
  efficiency)/100 * 0.12 + 0.01`, invented formula), `defect_types` = one
  of 4 fixed strings, random choice.
- **Real source:** none of the four files contain quality/defect data.
- **Buckets:** all fields — **(c)**.
- **Dependency:** LOWEST, same tier as the supply-chain group. Confirmed
  via grep: zero consumers outside the seeder. Not worth rebuild effort
  under the stated fidelity-where-it-matters goal.

#### `Delivery_Logistics`
- **Generates:** carrier fixed to "Aramex Logistics", `tracking_number`
  fabricated, ship/arrival dates derived from the order's own simulated
  completion.
- **Real source:** none.
- **Buckets:** all fields — **(c)**.
- **Dependency:** LOW, not read downstream (grep-confirmed).

#### `Optimization_Metrics_Log`
- **Not seeded at all** — truncated by the seeder (in `tables_to_reset`)
  but never populated by it. Written live by
  `agent/decision_engine.py::_log_optimization_metrics` during actual
  GA runs, read back only by the frontend (not core pipeline). Out of
  scope for this audit — it isn't seed data, it's a live operational log.

---

## Cross-table dependency graph (topological, by real generation order)

```
Companies ─┬─ Factories ── Departments ── Production_Lines ─┬─ Machinery_Assets ── Machine_Capabilities
           │  (WorkStation.xlsx: Department)  (WorkStation.xlsx: ID/LineName)     Machine_Status_Logs
           │                                                │
           │                                                ├─ Employees ─┬─ Work_Schedules
           │                                                │  (uniform    ├─ Operator_Skill_Matrix
           │                                                │   factory)   
           │                                                │
           │                                                └─ employee_batches ── batch_members
           │                                                   (line-scoped, real crew SIZE only)
Suppliers ── Inventory_Stock                                
Raw_Materials ┘        ↑ (BOM consumption, no downstream reader)

Products (grid.xls) ─┬─ Product_Variants
 (StyleId, GarmentType│
  weighted)           ├─ Routing_Operations (routing_tables.py + Plan.xlsx GarmentSmv)
                      └─ Bill_of_Materials

Purchase_Orders ── Production_Orders
 (invented dates,      (invented qty, invented
  real data unused)     release date, real data unused)

              [[ ALL OF THE ABOVE ]]
                       │
                       ▼
              Job_Shop_Schedules  <── the maximum-dependency table;
                 │    │    │          every upstream change reseeds
                 │    │    │          its ~760K rows regardless of size
                 ▼    ▼    ▼
      WIP_Tracking  Quality_Logs  Delivery_Logistics
      (no downstream readers -- audit-trail only, all three)
```

**Tables with zero downstream Python consumers** (confirmed by grep across
`database/`, `graph/`, `ga/`, `agent/`, `models/`, `frontend/`), regardless
of bucket: `Product_Variants`, `Bill_of_Materials`, `Suppliers`,
`Raw_Materials`, `Inventory_Stock`, `WIP_Tracking`, `Quality_Logs`,
`Delivery_Logistics`, `Machine_Status_Logs`. Nine of twenty-six tables.
Rebuilding these would improve schema-level realism but would not move
any metric this project measures — worth explicitly deciding whether
that's in scope before spending effort there.

---

## Recommended rebuild order

**Production_Lines first, confirmed — the dependency analysis agrees with
your expectation, not against it.** It is the most upstream table that (a)
already maps almost directly to a real file, (b) structurally constrains
the two other highest-leverage tables (Machinery_Assets, employee_batches),
and (c) is cheap to extend (two real, currently-unread columns —
`LayoutType`, `LineType` — sitting right there, plus `Plan.xlsx.Efficency`
for the hardcoded 85%).

Proposed order, by leverage (how many other tables/findings it constrains)
divided by cost (confirmed against the dependency graph above, not
guessed):

1. **Production_Lines** — extend to use `WorkHour` (feeds Section 1.2.3
   directly), `Efficency` (replaces the hardcoded 85%), and investigate
   `LayoutType`/`LineType` as a possible real line-to-garment-family
   constraint. Highest leverage: constrains Machinery_Assets and
   employee_batches downstream.
2. **Machinery_Assets** — rebalance per-type machine counts away from the
   uniform 600/type, informed by Routing_Operations' real per-type demand
   (bucket b: derived, not directly sourced, per the nuance above — no
   real machine-count-by-type file exists). Directly addresses Section
   1.3's newly-found Body-Assembly imbalance. Cheapest high-impact change
   in the schema: same columns, same row count, no downstream code depends
   on the 600 constant.
3. **Purchase_Orders / Production_Orders** — real dates (`grid.xls`/
   `Plan.xlsx` StartDate/OrderedDate/ETADate) and real quantities
   (PlanTarget/BalanceQty/OrderQty). This is Section 1.2.2's already-scoped
   fix, now with two real source columns identified precisely.
4. **Employees / employee_batches** — ground the aggregate headcount in
   real per-line Worker Capacity sums (not individual identities, which
   stay synthetic per your own expectation); revisit `CREWS_PER_LINE=3`
   only if/when Production_Lines' `WorkHour` fix (step 1) is actually
   implemented, since they're the same underlying gap (Section 1.2.3).
5. **Job_Shop_Schedules' duration formula** — the biggest invented
   surface area in the schema, and the only step where `PPC-1.xlsx`'s real
   daily output data would actually get used. Deliberately last: every
   upstream step above reseeds this table's ~760K rows anyway, so
   calibrating its own formula before the upstream tables are settled
   would mean doing that calibration twice.
6. **Everything with zero downstream consumers** (the nine tables listed
   above) — deprioritized regardless of bucket; flag for an explicit
   in/out-of-scope decision rather than defaulting either way.

No code changed and no reseed run to produce this document.
