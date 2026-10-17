# Data Profile — Every Table, Every Column

Full statistical pass over the rebuilt database. Originally run
post-Module-5b; updated in place after two EDA-review-driven fixes —
Module 6 (Blouse/Dress short-sleeve routing, a genuine implementation bug
`routing_spec.md` had already specified) and Module 7 (Purchase_Orders/
Production_Orders count, grounded from an invented flat 1,500 to a real
1,029) — both described in their own sections below and in
`DATA_REBUILD_LOG.md`. Every number below is from a real query against
the current database, not summarized from memory or carried over
unchecked from the pre-Module-6/7 version of this file. A companion
visual version with 15 charts is published as an Artifact (link shared in
chat) and reflects the pre-Module-6/7 state — not yet regenerated, since
none of its charts' underlying shapes changed enough to warrant it before
the end-of-rebuild run.

**Scale**: 26 tables, 397,382 total rows (was 512,549 before Module 7 —
the row-count drop is almost entirely Job_Shop_Schedules and its
downstream tables scaling down with the corrected, smaller order count,
not data loss). 7 tables real-data-grounded (Modules 1–5b, deepened by
Modules 6–7), 9 tables explicitly documented as deliberate synthetic
choices (no downstream consumer, and for most, no real source either),
the rest (`companies`/`factories`/`departments`, `work_schedules`,
`operator_skill_matrix`, `employee_batches`/`batch_members`) a mix of
real structure with synthetic detail.

---

## 1. Organization & line structure

**`companies`** (1 row), **`factories`** (6), **`departments`** (6) — real
company/factory/department identity from `WorkStation.xlsx`'s Department
column (NCL, CW, FIN, NC8, PCI, CW3). Fixed-scale reference tables.

**`production_lines`** (99 rows) — 99 of 141 real `WorkStation.xlsx` lines
are enabled. `line_number` has only 42 distinct values across 99 rows
(LINE01–LINE0N repeats across departments, a real naming convention).
`target_efficiency_percentage`: real, sampled from `Plan.xlsx`'s true
distribution (56.8–98.5, median 78.7, σ 5.31) — NOT a false per-line join
(see Module 1's `Plan.xlsx.LineId` integrity finding). `layout_type`:
H:50 P:48 W:1. `line_type`: E:79 L:20 — both real, stored, investigated
for a routing dependency and found to have none.

---

## 2. Products & routing

**`products`** (485 rows) — `category` weighted by `grid.xls`'s real
per-type frequency: Pant 200 (41.2%), U Neck 97 (20.0%), Full Zip Mock
Neck 39, Blouse 34, Short 30, Tank Top 19, Dress 19, 1/2 Zip Mock Neck 17,
V Neck 8, Hoodie 7, Full Zip Hoodie 4, Polo 4, Shirt 3, Jump Suit/Tube/
1/2 Zipper Hoodie/Helmet 1 each — matches the real 5,132-row source shape
closely.

**`routing_operations`** (4,139 rows) — the single biggest structural
change in the rebuild. Was a fixed 5-step template for every product;
now 6–10 steps (median 9), varying by garment-type group: 6:22, 7:103,
8:21, 9:272, 10:67 products. The 8-step count is Module 6's short-sleeve
Blouse/Dress fix (`routing_spec.md` 3.3) — previously 0 products at 8
steps; every Blouse/Dress product silently defaulted to the 9-step base
until this fix. `required_machine_type` share: Overlock Serger 29.8%,
Single-Needle Lockstitch 29.7%, Flatlock Coverstitch 14.5%, Automated
Pattern Tacker 14.2%, Feed-off-the-Arm Special 11.7%. **Confirmed**:
Feed-off-the-Arm (Tubular Sleeve Join) never appears as a terminal step
in any routing — 0.0% of the 1,029 resampled synthetic orders terminate there; 72.3%
terminate on Automated Pattern Tacker, 27.7% on Flatlock Coverstitch,
still matching `routing_spec.md`'s predicted ~70/~30/0% closely.
`standard_allowed_minutes`: 1.36–4.87, median 2.97, only 18 distinct
values (expected — derived from a small set of real GarmentSmv values
divided by a small set of routing lengths, now including the 8-step
divisor).

**`product_variants`** (7,760 rows) — fixed 4×4 grid (S/M/L/XL ×
Black/Navy/Grey/White, 1,940 of each). Real Size/Color data exists in
`grid.xls` (74 real size codes, 148 real color codes) but is sparse per
style and — decisively — this table has zero downstream consumers, so
grounding it would change nothing measured. Documented as deliberate
synthetic in the seeder.

---

## 3. Workforce

**`employees`** (15,066 rows, was 9,000) — total headcount now equals
real per-line Worker Capacity summed across 99 lines × 3 crews.
`factory_id` weighted by real per-factory capacity (NCL and PCI largest).
`role`: Operator 14,229 (94.4%), Line_Supervisor 449, Mechanic 388 — ratio
preserved from the old 9,000 baseline (no real source for role mix).
`first_name`/`last_name`: 676/1,000 distinct, Faker-generated (no real
employee-identity source exists or should be used).

**`work_schedules`** (15,066 rows) — one row per employee, `shift_date`
always today (single date — this table has never held more than one
calendar day). `is_on_leave`: 7.85% (matches the documented 8% invented
rate). `start_time`/`end_time`: exactly 3 distinct values (06:00/14:00/
22:00 bands), 5,022 rows each — perfectly even 3-way split.

**`operator_skill_matrix`** (21,345 rows) — 1–2 skills per operator from
the 5 canonical types, roughly even across types (4,223–4,322 each).
`efficiency_rating`: 70–98, median 84 — matches the documented invented
range exactly. 14,229 distinct employees hold at least one skill (every
Operator).

**`employee_batches`** (281 rows) / **`batch_members`** (14,229 rows) —
crew SIZE is real (Worker Capacity), crew COUNT per line stays fixed at 3
(the deferred shift-count fix, Section 1.2.3). 93 of 99 lines now have
all 3 crews staffed (was 39 before Module 4), only 16 crew slots empty
(was 176). `rotation_offset`/`rest_day_offset`: deterministic round-robin,
not random — 3 and 7 distinct values respectively, evenly spread.

---

## 4. Machines

**`machinery_assets`** (3,000 rows) / **`machine_capabilities`** (3,000
rows) — was uniform 600 per type; now demand-proportional (bucket b, a
principled derivation — no real machine-count-by-type source exists in
any file): Overlock Serger 909, Body Assembly 878, Flatlock Coverstitch
450, Automated Pattern Tacker 404, Feed-off-the-Arm Special 359 (shifted
slightly from Module 2's original allocation after Module 6's short-
sleeve routing fix rebalanced the underlying per-type demand share by
under 1.1%). **Confirmed**: supply/demand floor spread dropped from
2.82× (30.6–86.4 days/machine) to 1.32× (48.6–64.0 days/machine) after
the original Module 2 rebalancing. `current_status`: Active 2,627
(87.6%), Breakdown 158 (5.3%), Under_Maintenance 135 (4.5%), Idle 80
(2.7%) — close to the documented 90/5/3/2 weights, with Breakdown
elevated by the dynamic mid-simulation breakdown mechanism.
`machine_model`: 446 distinct fabricated "Juki-XXXX-NN" strings.

**`machine_status_logs`** (7,480 rows, was 10,385) — pure audit trail,
zero downstream consumers, no real source in any file. Row count tracks
Job_Shop_Schedules' own drop (Module 7's order-count fix reduced the
schedule timeline these events fire against). 4,480 of 7,480 rows (59.9%)
are mid-simulation breakdown events (a byproduct of the invented
15%-after-1,200-runtime-minutes breakdown model).

---

## 5. Supply chain (entirely decorative)

**`suppliers`** (3), **`raw_materials`** (5), **`inventory_stock`** (75),
**`bill_of_materials`** (1,455) — zero downstream consumers anywhere in
the live pipeline; `inventory_stock`'s one query function
(`fetch_inventory_state`) is only called by a diagnostic self-test for a
RAG stock agent that doesn't exist yet. None of the four real files
contain supply-chain data. Notable quirk: `bill_of_materials.material_id`
only ever references 3 of the 5 real materials (fabric, thread, label —
buttons and zippers are never used in any BOM row).

**Flagged, not built**: material-availability constraints (fabric/trim
shortages causing real delay) are a genuine, unmodelled garment-industry
mechanism. The tables aren't the gap — the consuming logic in the GA is.
Candidate for a future scheduling-layer module, out of scope here.

---

## 6. Orders

**`production_orders`** (1,029, was 1,500) / **`purchase_orders`** (1,029,
was 1,500) — order COUNT grounded in Module 7 (previously an invented
flat 1,500, the same category of gap employee headcount had before
Module 4). Real order-equivalent count from `grid.xls`: 139 genuine real
`PO`-tagged orders (only 6.5% of rows carry one) plus 890
`StyleId+Color+BuyMonth` groups for the other 93.5% — `BuyMonth` checked
and confirmed to never split within a single known-real PO, unlike
`StyleId+Color` alone (too coarse, merges separate real orders) or
`StyleId+Color+StartDate` (too fine, splits a single real order — real
`StartDate` varies within one known PO 31/139 times). `target_quantity`:
real per-PO totals (n=139 real POs), 52–3,300, median 330, mean 446 (was
uniform 1,500–12,000) — distribution shape unchanged from Module 3, only
the row count changed. `scheduled_start_date`: real `grid.xls` StartDate
shape (399-day row-level span, front-loaded), median-anchored to today (the
max-anchor attempt was measured and rejected: it produced 99.5%
already-overdue orders).

**Provenance clarification:** The 139 genuine PO groups and 890 proxy groups
calibrate the count only. All 1,029 seeded records independently resample
attributes; they do not preserve the original source tuples. Source-group
order dates below differ from the row-level distribution used in generation.

**Acknowledged ambiguity, not resolved further (checked, not assumed):**
the 399-day span above is the ROW-level range (rows include Size splits
and EndDate/ExFtyDate activity extending into early 2025). The
ORDER-level range (min StartDate per one of the 1,029 source order-equivalent
units) is shorter: 2024-01-02 to 2024-10-22, 294 days — genuinely NEW
order starts stop appearing 105 days before the row-level window ends.
Two explanations were considered and NEITHER was ruled out: (a) real
demand for new orders tapered off toward the end of the export's period
(order-start rate in the final 60 days runs at 1.47/day vs. 2.39/day in
the preceding stretch — consistent with a genuine slowdown, not an
uptick), or (b) the export was pulled while later new-order entry was
still incomplete, a common lag in operational systems, which would
undercount true new-order volume near the extract's own cutoff without
reflecting real demand. A 3-part check (PO/company concentration, garment-
type mix, end-of-window trend — see chat, 2026-08-23) found no evidence of
a bulk-contract/export artifact for the EARLY 54-day burst specifically
(spread across 511 distinct orders, 6 companies, no single buyer
dominating), which is why the 1,029 total order count itself was kept
as-is rather than discounted — but that check does not extend to
resolving which explanation accounts for the order-level window being 105
days shorter than the row-level one. Left open on purpose.

The front-loaded shape itself was confirmed to survive re-anchoring intact
in the live, currently-seeded database (not just assumed from the source
file's shape): Q3 of the re-anchored year holds 619 of 1,029 orders
(60.1%), with a single calendar month (the peak month) holding 273 orders
(26.5% of the whole dataset). Re-anchoring only shifts the distribution by
a constant date offset, so this concentration was always going to carry
through unchanged — flagged here because it is a real, sharp seasonal
concentration a rolling-horizon GA window will actually encounter, not a
smoothed-out artifact of the reseed. `delivery_deadline`: real ExFtyDate–StartDate
lead time, not a flat 30–60 days. Live state: 50.5% of orders already
started, 32.8% already past deadline — a bounded, plausible snapshot, not
degenerate (both a couple of points higher than Module 3's original
47.5%/31.1%, an expected resample-at-smaller-n effect, not a new date
mechanism). `po_status`: Confirmed 509, Delayed 504, In_Production 16.

---

## 7. Schedule execution

**`job_shop_schedules`** (167,691 rows, was 248,181 before Module 7, was
769,920 before Module 5b) — the largest table in the schema. Duration
formula recalibrated against real `PPC-1.xlsx` daily output (median 2.51
min/unit → `DURATION_CALIBRATION_FACTOR`=0.8255, was an invented 1.3 with
an undocumented /50.0 divisor; shifted from Module 5's original 0.8301
after Module 6's short-sleeve fix rebalanced the underlying SMV-per-step
mean by under 1%). Lot sizing changed from a fixed 60-lots-per-order to a
real order-scaled count (`TARGET_LOT_SIZE`=20). **Confirmed**: 19.5% of
rows sit at the 30-minute floor (was 89.1% before the Module 5b fix,
19.1% just after it — Module 7's order-count change left this
essentially unmoved, as expected: it changes ROW COUNT, not the per-lot
quantity distribution the floor interacts with), 80.5% show real
differentiated signal; median duration 48 min. `execution_status`:
Scheduled 50.1%, Done 28.7%, Delayed 20.2%, Failed 1.0%, Running 0.0%.
`batch_id` null rate: 19.2% — still matches the reseed log's
same-factory-fallback count, a real explainable characteristic not a
defect. `machine_status_at_dispatch`: 100% "Active" — confirmed dead
signal, as already documented in PROJECT_STATUS.txt Section 0E.
`queue_delay_mins_at_dispatch`: median 0, mean 318, max 13,402 min (~9.3
days, down from 15 days pre-Module-7 — a smaller order book means less
queueing congestion, expected) — still a long, real, heavily
right-skewed tail. **Own finding (see cross-cutting)**: before Module 7,
100% of 7+-day queue delays were concentrated on one machine type
(Overlock Serger, 4-Thread), fleet-wide across all 99 lines, not a rare
outlier — re-measure after the end-of-rebuild run rather than re-checked
here, since the underlying mechanism (routing demand share, not order
count) didn't change in Module 7.

**`wip_tracking`** (66,552 rows, was 81,719) — duplicates a signal
already tracked the real way (execution_status/remaining_frac). Zero
consumers. Row count tracks Job_Shop_Schedules' own drop.

**`quality_logs`** (66,552 rows, was 81,719) — defect rate scaled by
operator skill (invented formula), 4 fixed defect-type strings (roughly
even). `defective_quantity`: mean 0.55, median 0 — most inspections pass
clean, distribution shape unchanged from before Module 7. Zero consumers
today. **Flagged, not built**: same rationale as the supply-chain
cluster — quality/rework-adjusted effective capacity is real and
unmodelled, but the table isn't the gap.

**`delivery_logistics`** (1,029 rows, was 1,500) — pure post-production
shipping, zero scheduling relevance; on-time is already measured against
the customer deadline directly, not via this table.

**`optimization_metrics_log`** (0 rows) — not part of this rebuild; a
live operational log written during actual GA runs, not by the seeder.

---

## Cross-cutting findings

- **Overlock Serger queueing bottleneck** (own finding — a different signal
  than Module 2's capacity-floor result, not a restatement of it).
  Originally measured pre-Module-7 (1,500-order book): 6,091 rows (2.45%)
  exceeded 7 days' queue delay, p99 = 10.9 days. Re-measured against the
  current post-Module-7 database (1,029 orders) rather than left stale:
  935 rows now exceed 7 days (0.56% of 167,691 — smaller in absolute
  terms, as expected with a smaller, less congested order book) and none
  exceed 10 days (was 1.33%). **The mechanism is unchanged and still
  fully concentrated**: 100% of the 7+-day-delay rows, all 935 of them,
  still occur on exactly one machine type, Overlock Serger (4-Thread) —
  1.9% of all Overlock Serger operations (was 8.3%) — zero on any of the
  other four types, spread across 98 of 99 lines (not a handful of
  hotspots). Verdict unchanged: a routine, fleet-wide congestion pattern
  specific to one machine type, not a rare tail and not a localized
  hotspot — its magnitude scales with order-book size but its shape does
  not. Notably, Overlock wasn't the tightest type on Module 2's aggregate
  capacity-floor metric — that measured average supply vs. demand, this
  measures worst-case queueing — so the two results are complementary,
  not duplicative: a type can be adequately provisioned on average and
  still bottleneck at the tail.
- **LayoutType↔GarmentType: a real correlation the pipeline doesn't use.**
  `Plan.xlsx`'s `LayoutType` (H/P/W) does correlate with `GarmentType` —
  Blouse (66.7% H) and Dress (77.8% H) skew Hanging as expected for more
  complex garments, though Tank Top (98.8%), Polo (99%), and U Neck
  (75.2%) skew Hanging even more strongly despite being simpler basics, so
  the pattern doesn't reduce to a clean complexity axis — more likely
  reflects which physical lines happen to produce which garment families
  at this plant. Regardless of the shape of the correlation: this is a
  real, documented modeling gap, not a data problem. `production_lines.
  layout_type` is stored from real `WorkStation.xlsx` data (Module 1), but
  no routing, scheduling, or line-assignment logic anywhere in the
  pipeline reads it — confirmed by source search, `grep`-clean across
  `ga/`, `graph/`, `agent/`, `database/`. There is also no line-to-
  garment-type affinity mechanism of any kind today (orders aren't routed
  to lines by what they produce), so even a real signal here would have
  nowhere to attach. Flagged for the same future scheduling-layer review
  as material availability and quality/rework, not built here.
- **Consistency check passed**: Routing_Operations' real machine-type
  demand share (29.8/29.7/14.5/14.2/11.7%) and Machinery_Capabilities'
  resulting supply allocation (30.3/29.3/15.0/13.5/12.0%) still track each
  other within a point after Module 6's routing fix rebalanced both —
  Module 2's rebalancing mechanism held up exactly as designed, not just
  at its original measurement but under a structural change to the
  routing tables it depends on.
- **Redundancy**: 3 of the 9 documented-synthetic tables (WIP_Tracking,
  Machine_Status_Logs, partially Delivery_Logistics) duplicate a signal
  the live pipeline already tracks a different, already-real way.
- **Real, explainable, not a bug**: `batch_id`'s 19.2% null rate in
  Job_Shop_Schedules (was 19.1% pre-Module-7, essentially unmoved) is
  still explained by the same-factory-fallback mechanism from the reseed
  log — roughly 1 in 5 crews still can't cover their own line's work even
  after Module 4's real sizing.
- **Two real opportunities on record**: material availability and
  quality/rework, both flagged above, both carried forward for whenever
  the GA/scheduling module comes up for its own review.
- **Scale note**: Job_Shop_Schedules dropped from 769,920 to 248,181 rows
  between Module 5 and 5b (a two-thirds reduction from a single
  lot-sizing fix), then to 167,691 after Module 7's order-count fix (a
  further one-third reduction from a single count correction) —
  illustrating twice over how every upstream table change reseeds this
  table's full row count regardless of how small the change looks.
