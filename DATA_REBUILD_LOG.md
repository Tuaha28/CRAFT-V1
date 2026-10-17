# Data Foundation Rebuild — Module-by-Module Log

Companion to `DATA_AUDIT.md` (the audit) and `PROJECT_STATUS.txt` (the
project's authoritative record, updated once the full rebuild is done).
This file tracks each module's implementation and isolated measurement as
it happens, per the explicit instruction to test and report each module
separately rather than batch them into one combined reseed.

Rebuild order: Production_Lines → Machinery_Assets → Purchase_Orders/
Production_Orders → Employees/employee_batches → Job_Shop_Schedules
duration formula → the nine zero-consumer tables.

All comparisons below are against the routing-fix run (seed=42): on-time
16.7%, makespan 228.4d, throughput 6.6/d, mean tardiness 2172.1h, labour
26.6%, GA margin 7.2%, GNN AP 0.067, ROC AUC 0.494.

---

## Module 1 — Production_Lines

**Implemented** (`data_generation/seed_database_fixed.py`):
- `target_efficiency_percentage`: was hardcoded 85.00 for every line.
  Attempted the obvious fix (join `Plan.xlsx.Efficency` per `LineId`) and
  caught a real problem before implementing it: `Plan.xlsx`'s own
  `LineName`/`Line` text columns are a single placeholder value ("Dummy
  Line"/"LINE-DUMMY") across all 1,382 rows, and the same numeric `LineId`
  recurs under up to 10 different `BrId` (branch) values — impossible for
  a real physical line. That column cannot identify a specific real line.
  Corrected mid-implementation: each line's efficiency is now sampled from
  the real observed distribution (`Plan.xlsx.Efficency`, mean 77.8%, std
  5.6%, n=1,382) rather than falsely attributed per line. Bucket (a) at
  the granularity the source actually supports — company-wide real
  distribution, not per-line real values.
- `layout_type` / `line_type`: added as new real columns
  (`WorkStation.xlsx`). Investigated whether either gates which garment
  types run on a line (cross-tabbed against `Plan.xlsx`'s real
  GarmentType-by-line history) — no discriminating signal found (79 "E"
  vs 20 "L" lines; virtually all real production rows ran on "E"
  regardless of garment type). Stored as real informational data, NOT
  wired into a routing dependency the data doesn't support.
- **Also fixed, infrastructure**: `execute_complete_seed()` never fixed a
  random seed. Every reseed mixed a real code-change effect with fresh
  random noise — added `random.seed(42)` at the top. This run is the one
  transition case where the comparison is confounded by two things at
  once (seed newly fixed + Module 1's own changes); every module from
  here on compares cleanly against the one before it.

**Backup:** `backups/pre_module1_production_lines_20260818_152944.sql`

**Measured** (seed=42, full pipeline, single run):

  metric                   routing-fix run   Module 1 run
  ------------------------------------------------------
  on-time rate                16.7%             15.7%
  makespan                    228.4d            247.0d
  throughput                  6.6/d             6.1/d
  mean tardiness              2172.1h           2461.8h
  max tardiness               4489.2h           4885.9h
  labour utilisation          26.6%             28.1%
  GA margin over baseline     7.2%              3.5%
  local search contribution   +0.51%            +0.48%
  per-window on-time     100/98/77/7.3/0.3%(*)  78.7/0/0/0/0%
  GNN delay AP                 0.067             0.087
  GNN delay ROC AUC            0.494             0.551

  (*) routing-fix run's own per-window shape for reference; the routing-fix
  vs Module 1 comparison line directly above uses the closer 83.7/0/0/0/0%
  reading from the same run.

**Attribution — quantitatively consistent with the efficiency change being
the dominant driver, not noise:** the naive prediction from the efficiency
change alone (85.00 -> ~77.8% average, a 1.093x ratio) implies makespan
should inflate to roughly 228.4 x 1.093 ≈ 249.6d absent any other effect.
Observed: 247.0d — within 1% of that prediction. This is a real, causally
understood degradation, not an artifact of the seed-fix or random
re-draw: less average line efficiency means more real minutes per
operation, which is exactly what should make everything worse, and did,
by almost exactly the predicted amount.

**Not explained by the efficiency arithmetic alone:** GA margin over
baseline dropped sharply (7.2% -> 3.5%) and GNN AP/ROC AUC both improved
(0.067 -> 0.087 AP, 0.494 -> 0.551 ROC AUC). Neither is predicted by the
efficiency-ratio arithmetic above. Reported as observed, not yet
explained — a single run, not a multi-seed result, so before drawing any
conclusion from either movement it would need the same multi-seed
confirmation the routing-fix result got. Flagged rather than
interpreted.

**Verdict:** implemented, measured, real and quantitatively-understood
degradation on the headline metrics (as expected — the standing caution is
that realistic data makes performance numbers worse, not better). Two
loose ends flagged rather than swept in: the margin drop and the AP/ROC
AUC movement, neither yet attributed. Holding here per instruction, not
proceeding to Module 2 without review.

---

## Process change after Module 1

Full pipeline runs per module were producing single-seed noise (the
margin drop, the AP movement above) neither attributable nor
decision-relevant during a fidelity rebuild -- performance isn't being
optimised for here, so per-module metrics don't inform anything. Switched
to: implement -> reseed only (~16 min, not ~78) -> verify at the data
level (direct DB queries against the real source) -> full test suite ->
a cheap structural smoke test (graph build + one trivial GA window, no
training) -> an arithmetic sanity check where the change has a
predictable effect -> report. One full multi-seed pipeline run reserved
for the end, once every module is in.

---

## Module 2 — Machinery_Assets

**Implemented:** replaced the uniform 600-machines-per-type allocation
with one proportional to real routing-derived demand. BUCKET (b), stated
precisely per instruction: no file gives a real machine-count-by-type, so
this is NOT "sourced from real data" -- it is a principled derivation
from two real inputs (`routing_tables.py`'s literature-derived routings x
`grid.xls`'s real per-category population share), computed structurally
from routing step counts (not per-order work-minutes, since Machinery_
Assets is seeded before Production_Orders' real quantities exist).
Cross-checked against the routing-fix run's actual measured operation-
count shares before adopting it as the allocation basis: structural
29.6/30.1/15.0/13.4/11.9% vs measured 31.0/29.3/14.0/13.9/11.8% for
SNLS/Overlock/Flatlock/Tacker/FOA -- close enough that the gap is real
per-order quantity weighting, not a modelling error.

**Bug caught by the cheaper process, not by a wasted 78-minute run:**
first implementation referenced `machine_types` before its definition
point in the seeder (the demand computation was placed too early). Failed
immediately at reseed start (~20s in), not after a long run. Moved the
demand-share block to right after `machine_types`/`optype_by_machine_type`
are defined. Recompiled, dry-run verified against real files before
reseeding again.

**Backup:** `backups/pre_module2_machinery_assets_20260818_164316.sql`

**Verification 1 -- data level, direct query.** Machinery_Assets now
holds exactly the predicted allocation:

  type                   count (was 600 each)
  ------------------------------------------
  Seam Edge Overlock            903
  Body Assembly                 888
  Flatlock Hemming               450
  Bar-Tack Pocketing            402
  Tubular Sleeve Join            357
  TOTAL                        3,000

**Verification 2 -- test suite:** 353/353 passing, unchanged.

**Verification 3 -- structural smoke test:** `FactoryGraphBuilder().
build_graph()` succeeded (12,830 operations, 858,006 disjunctive edges);
`utils/machine_reserve.py`'s reserve computation adapted correctly to the
new non-uniform counts with no code change needed (e.g. Tubular Sleeve
Join: 357 total -> 332 usable, 25 reserved, still exactly 7%); a tiny GA
window (60 orders, pop=8, gen=3) ran to completion with no crash, best
fitness 1087.4. Confirms the rebalancing is schema-shape-neutral, exactly
as predicted in `DATA_AUDIT.md` -- no downstream code depended on the 600
constant.

**Verification 4 -- did it actually fix the imbalance? Recomputed the
supply/demand floor table (0H.6/Section 1.3 methodology) on the newly
reseeded data:**

  type                   floor (days/machine)
  -------------------------------------------
  bar-tack pocketing            64.0
  body assembly                  58.3
  seam edge overlock             53.5
  tubular sleeve join            51.8
  flatlock hemming               48.6

Floor spread: 48.6-64.0 days/machine, a **1.32x ratio** -- down from
**2.82x** (30.6-86.4 days/machine) before rebalancing. Whole-book naive
floor essentially unchanged (54.2d -> 55.4d, expected: same total work,
same total machines, just redistributed). This is the intended, measured
effect: capacity now tracks real demand far more evenly across types.

**Verdict:** implemented and verified at a fraction of the previous
process's cost (~20 minutes including the bug-fix cycle, vs ~78 minutes
for Module 1). The imbalance Section 1.3 flagged is directly, measurably
addressed. Whether this actually improves the full-pipeline scheduling
metrics (on-time, makespan) is deliberately NOT measured yet -- reserved
for the end-of-rebuild multi-seed run, per the revised process. Holding
here, not proceeding to Module 3 without review.

---

## Module 3 — Purchase_Orders / Production_Orders

**Implemented:** replaced three invented ranges with real `grid.xls`
distributions.

- `target_quantity`: was `random.randint(1500, 12000)`. `grid.xls.PO` is
  mostly missing (only 139 of 5,132 rows have one; `BalanceQty` turned out
  to be non-numeric, not usable as a quantity). Grouped the 139 rows that
  DO have a real PO and summed `PlanTarget` per PO -- real order-level
  quantity, n=139, mean 452, median 330, range 52-3,300. Sampled from this
  real distribution per synthetic order. Real range is far smaller than
  the old invented one -- a genuine finding (per the standing caution:
  expected to look different, not better).
- `scheduled_start_date` / release date: was a ~25-30-day invented spread
  (Section 1.2.2's flagged gap). Sourced from `grid.xls.StartDate`,
  row-level, n=5,131, real span 2024-01-02 to 2025-02-04 (~399 days),
  steeply front-loaded (median only 54 days after the min).
- `scheduled_end_date`: was `start_date + random(5,15)` days. Sourced from
  real `EndDate - StartDate` gaps (n=5,131, mean 3.2 days, median 1 --
  much SHORTER than the old invented range).
- `delivery_deadline`: was `random(30,60)` days after `base_date`,
  unrelated to the order's own start. `ETADate` was checked and rejected
  as the source: it PRECEDES `StartDate` for most rows (mean -26 days),
  consistent with a materials-arrival date, not a customer commitment.
  `ExFtyDate` (ex-factory/ship date) was adopted instead: mostly positive
  gap from `StartDate` (mean 23.8 days), filtered to positive values only.

**Anchor-choice finding, measured before deciding, not assumed:** the
real 2024-2025 dates can't be used literally (this simulation's "now" is
2026). First tried mapping the real dataset's LATEST observed date to
today -- the most literal "faithful to the file's own as-of point" choice.
Measured consequence before adopting it: **1,492 of 1,500 (99.5%)**
simulated orders came out with a deadline already in the past relative to
today, because the whole front-loaded 399-day real spread then lands
almost entirely before "now" -- a degenerate, uniformly-overdue order
book. Rejected. Anchored the real MEDIAN start date to today instead:
dry run predicted 49.9% already-started / 32.4% already-past-deadline; the
actual reseeded DB measured **47.5% already-started / 31.1% already-past-
deadline** -- a live, bounded, plausible snapshot, not a degenerate one.

**Two bugs, both caught within seconds by the cheaper process, not after
a wasted 78-minute run:**
1. Module 2's `machine_types`-before-definition ordering bug (already
   logged above).
2. This module's own: referenced `base_date` (previously first defined in
   Section 4, Employees) from a new block placed in Section 1's setup,
   before that point. Fixed by hoisting `base_date = datetime.now().date()`
   to the top of `execute_complete_seed()` -- a pure, dependency-free
   statement, safe to compute early.

**Backup:** `backups/pre_module3_orders_20260818_202455.sql`

**Verification 1 -- data level:**

  field                    old (invented)      new (real)
  ---------------------------------------------------------------
  target_quantity           1,500-12,000        52-3,300 (mean 499)
  scheduled_start_date      ~25-30 day spread    2026-06-25 to 2027-07-16
  delivery_deadline         30-60d after today   2026-06-26 to 2027-12-05
  already started                 --             47.5%
  already past deadline           --             31.1%

**Verification 2 -- test suite:** 353/353 passing, unchanged.

**Verification 3 -- structural smoke test:** graph build succeeded
(12,815 operations, 876,776 disjunctive edges); a tiny GA window (60
orders, pop=8, gen=3) ran to completion, best fitness 714.5. Notably, the
"earliest 60 orders by release date" smoke window itself now scores only
15.0% on-time for EVERY baseline heuristic including EDF -- not a crash or
a bug, but a direct, expected consequence of the new real order book: the
earliest-released orders are drawn from the front-loaded real cluster,
many already carrying tight or already-passed deadlines, so even the
"easiest" (earliest) window is no longer the near-100%-on-time case it
was under the invented ~25-30-day spread. Flagged as an expected shape
change, not a defect.

**Verdict:** implemented and verified (~25 minutes including the bug-fix
cycle). Real order-level quantities, dates and deadline-lead-time now
drive Purchase_Orders/Production_Orders, replacing three separate invented
ranges -- this is also the direct fix for Section 1.2.2's previously-open
release-date gap. Full-pipeline scheduling effect deliberately not
measured yet, reserved for the end-of-rebuild run. Holding here, not
proceeding to Module 4 without review.

---

## Module 4 — Employees / employee_batches

**Implemented:** replaced the arbitrary 9,000-employee constant with a
real aggregate headcount, and weighted factory assignment by real
capacity.

- `TOTAL_EMPLOYEES`: real per-line Worker Capacity (`worker_capacity_by_
  line`, already used for crew SIZE since the routing-fix session) summed
  across all 99 enabled lines x `CREWS_PER_LINE` (3, still an assumption --
  Section 1.2.3) = **15,066** -- LARGER than the old 9,000, not smaller.
  Worth stating plainly: fidelity doesn't always shrink a number, and this
  is a case where it grew.
- `factory_id` assignment: was `random.choice` uniform across factories.
  Now weighted by each factory's real total capacity (per-line Worker
  Capacity summed via `line_to_factory_map`), so larger real factories get
  proportionally more of the workforce.
- Operator:Supervisor:Mechanic ratio: no real source distinguishes role
  mix in any of the four files -- preserved the previous ratio (8,500:500
  of 9,000, ~94.4% Operator) as an explicitly documented synthetic choice
  (bucket c), applied to the new real total rather than re-derived from
  nothing.
- Two stale hardcoded references caught and fixed in the same pass
  (`employee_free_time` dict keyed `range(1,9001)`; a fallback
  `random.randint(1,8500)` employee pick) -- both would have silently
  under-covered the new, larger workforce if missed. Swept the whole
  codebase (`agent/`, `ga/`, `graph/`, `models/`, `database/`, `utils/`)
  for other hardcoded 9000/8500 assumptions before reseeding -- none found
  outside the seeder itself.

**Backup:** `backups/pre_module4_employees_20260818_205005.sql`

**Verification 1 -- data level:** `Employees` COUNT(*) = 15,066, exactly
as predicted. Role split: 14,229 Operator / 449 Line_Supervisor / 388
Mechanic (matches the preserved ratio). `factory_id` distribution
(5125/3451/2221/187/3888/194 across the 6 factories) is proportional to
the real per-factory capacity weights computed from `WorkStation.xlsx`,
not uniform.

**Verification 2 -- test suite:** 353/353 passing, unchanged.

**Verification 3 -- structural smoke test:** graph build succeeded; a
tiny GA window (60 orders, pop=8, gen=3) ran to completion, best fitness
641.6.

**Verification 4 -- did real sizing fix the pool-exhaustion problem the
seeder's own comments already flagged?** Yes, directly and substantially:

  metric                        before (9,000)      after (15,066)
  ------------------------------------------------------------------
  lines with all 3 crews staffed      39 of 99            93 of 99
  crew slots left empty                 176                  16
  batch-member assignment hits       272,440              623,584
  same-factory fallback hits         495,582              146,336

This wasn't a targeted fix for crew understaffing -- it fell directly out
of sizing the workforce to real demand instead of an arbitrary constant.
The old 9,000 wasn't just unrealistic, it was too small for the real
per-line capacity structure it was supposed to staff.

**Verdict:** implemented and verified in a single, clean pass (~20
minutes, no bugs this time). Real aggregate headcount and real factory
weighting both landed correctly, and fixed a genuine, previously-flagged
operational shortfall as a direct consequence rather than a separate
effort. Full-pipeline scheduling effect still deliberately not measured,
reserved for the end-of-rebuild run. Holding here, not proceeding to
Module 5 without review.

---

## Module 5 — Job_Shop_Schedules duration formula (PPC-1.xlsx calibration)

The largest fidelity gain flagged in `DATA_AUDIT.md`: the previous
`DURATION_CALIBRATION_FACTOR=1.3` and its accompanying, separately-applied
`/50.0` divisor were both invented with no cited derivation, and this
formula determines every lot's actual simulated timing -- the single
largest source of fabricated numeric content in the schema.

**`PPC-1.xlsx` Output-field investigation, done before calibrating
anything:** the file was previously loaded nowhere in the seeder at all.
Its `Output` column looked at first like literal daily units produced,
but investigation found it caps hard at exactly 100.0 for 72.7% of rows
(3,240 of 4,458), with the remainder clustering on clean fractions (50,
25, 75, 33.33, 66.67, 40, 20, 80, 16.67, 83.33...) -- the signature of a
percentage/achievement-rate field, not a raw count. A cleaner grouping
attempt (sum real hours per real PO, divide by that PO's real total
quantity) was also tried and rejected: `TotalQty` turned out NOT to be
constant within a PO (~9.7 distinct values per PO on average -- it's
per-style/variant, not a PO-level total), so a `.first()`-based grouping
would have silently paired mismatched totals.

**Resolution adopted, documented rather than asserted as certain:**
treated `Output` as literal units at ROW level (the simpler, still-
defensible reading -- small real daily batches plausibly produce clean-
fraction outputs too, e.g. half a typical day's target). Computed
`WorkHrs*60/Output` per valid row (`Output>0`, `WorkHrs>0`: 4,052 of
4,458), giving a real minutes-per-unit distribution: median **2.51**,
mean 4.35 (right-skewed, max 48) -- median used as the calibration
target, more robust to the skew.

**Scale-mismatch reasoning, needed before using this number directly:**
this real figure reflects a whole PRODUCTION LINE's aggregate daily rate
-- many operators doing different operations in parallel -- not one
operator's single-operation SMV. Using it to directly replace
`GarmentSmv` would conflate two different concepts (confirmed by
magnitude: real median 2.51 min/unit vs. real weighted mean per-step SAM
2.36 min -- comparable scale to each other, NOT to the ~18-27 min whole-
garment `GarmentSmv`). Instead it calibrates the formula's overall SCALE:

  `FACTOR = real_median_min_per_unit x mean_efficiency / mean_sam_per_step`
  `       = 2.51 x 0.778 / 2.36 = 0.8301`

Computed fresh inside `execute_complete_seed()` from the already-loaded
real data (not hardcoded), printed at reseed time for visibility. The old
`/50.0` divisor -- never separately justified -- is folded into this one
derived constant and removed from the formula entirely.

**Backup:** `backups/pre_module5_duration_20260818_211239.sql`

**Verification 1 -- data level:** `Job_Shop_Schedules` planned duration
distribution (n=769,920 rows): mean 39.3 min, but the shape matters more
than the mean here --

  30-min floor (exactly 30)         685,733 rows   89.1%
  strictly between 30 and 480       84,135 rows    10.9%
  480-min ceiling (exactly 480)          1 row      0.0%

**IMPORTANT FINDING, flagged rather than absorbed silently:** the
calibration is real and correctly derived, but the 30-minute floor clamp
still overrides it for 89.1% of rows. Root cause: `N_LOTS=60` is fixed
regardless of order size, and Module 3's real order quantities are now
mostly small (median 330) -- a typical lot now carries only ~5-8 units,
and even the calibrated formula (~21 min for a typical lot) lands under
the floor. This is NOT a new problem Module 5 created: the old formula's
effective factor (1.3/50 = 0.026, vs the new 0.8301 -- a 32x difference)
would have floor-clamped even MORE often, so this module measurably
reduced floor-domination from what was almost certainly ~99%+ before to
89.1% now -- a real improvement, just not a full fix. Root cause
identified: `Plan.xlsx`'s `SplitQtyCount`/`SplitQty` columns look like
they should carry real lot-splitting data but are entirely empty (zero/
null across all 1,382 rows) -- there is no real source to size lots from,
confirming `N_LOTS=60` is bucket (c) with no available real replacement,
only a better-reasoned derivation. Discussed with the user directly
(see chat): recommended replacing fixed lot COUNT with a fixed-ish lot
SIZE (deriving lot count from order size instead) as the targeted fix,
scoped as a separate follow-on (Module 5b) rather than folded into this
module's already-completed scope. Decision pending.

**Verification 2 -- test suite:** 353/353 passing, unchanged.

**Verification 3 -- structural smoke test:** graph build succeeded; a
tiny GA window (60 orders, pop=8, gen=3) ran to completion, best fitness
612.0, makespan 3.3-4.5d across baselines, on-time 13.3% -- consistent in
scale with Module 4's smoke window (fitness 641.6), no anomalies.

**Verdict:** implemented and verified (~25 minutes). The calibration
itself is sound, real-data-derived, and correctly wired in -- but its
practical effect on the actual schedule is currently muted by a separate,
still-unaddressed interaction with `N_LOTS=60`. This is the last of the
five originally-planned modules (Production_Lines, Machinery_Assets,
Purchase_Orders/Production_Orders, Employees, Job_Shop_Schedules
duration). Remaining before the end-of-rebuild multi-seed pipeline run:
the nine zero-consumer tables, and a decision on Module 5b (the N_LOTS
fix). Holding here, not proceeding further without explicit go-ahead.

---

## Module 5b — Fixed lot COUNT replaced with order-scaled lot SIZE

Direct follow-on to Module 5's own flagged finding, discussed with the
user and approved before implementing (see chat): the 30-minute floor was
overriding Module 5's real calibration for 89.1% of rows because
`N_LOTS=60` split every order into exactly 60 lots regardless of size,
giving most (now-realistically-small, post-Module-3) orders tiny lots
that the calibrated formula could never push past the floor.

**Implemented:** `N_LOTS=60` (module-scoped, used only inside the order-
generation loop) replaced with `TARGET_LOT_SIZE=20` and
`MAX_LOTS_PER_ORDER=60` -- lot count is now DERIVED per order:
`n_lots = max(1, min(60, round(target_qty / 20)))`, instead of fixed.
No real source exists for lot/batch sizing in any of the four files
(confirmed already in Module 5: `Plan.xlsx`'s `SplitQtyCount`/`SplitQty`
are entirely empty) -- bucket (c), a principled constant chosen so a
TYPICAL lot's calibrated duration clears the 30-min floor with real
headroom, not a sourced real number. `TARGET_LOT_SIZE=20` derived from:
`duration>=30` needs `lot_qty >= 30*mean_efficiency/(FACTOR*mean_sam)
=~ 11.9` units; 20 gives comfortable margin above that threshold.
Checked before implementing: `ga/scheduler.py`'s `GA_LOTS_PER_OPERATION`
(also 60, `utils/config.py`) is dead code there now -- referenced only in
a historical bug-fix comment, not used in any live computation -- so this
is a seeder-internal change with no cross-file consistency requirement.

**Dry-run prediction, verified before reseeding:** computed the resulting
lot count/duration across the real order-quantity range (52 to 3,300) --
every case cleared the floor: qty=52 -> 3 lots, 42.7 min; qty=330
(median) -> 16 lots, 50.3 min; qty=3,300 (max) -> capped at 60 lots (same
as before, unaffected by the cap), 138.2 min.

**Backup:** `backups/pre_module5b_lotsize_20260818_214501.sql`

**Verification 1 -- data level:** duration distribution, before/after:

  metric                        Module 5 (fixed 60 lots)   Module 5b (order-scaled)
  ------------------------------------------------------------------------------
  total Job_Shop_Schedules rows      769,920                    248,181
  at 30-min floor                    89.1%                       19.1%
  strictly between (real signal)     10.9%                       80.9%
  at 480-min ceiling                  0.0%                        0.0%
  median duration                    30 min                      50 min
  mean duration                      39.3 min                    60.5 min

Row count dropped by two-thirds (fewer, bigger, more realistic lots per
order) -- expected and correct, not a defect: most real orders now get a
double-digit lot count instead of a fixed 60 regardless of size.

**Verification 2 -- test suite:** 353/353 passing, unchanged.

**Verification 3 -- structural smoke test:** graph build succeeded; tiny
GA window (60 orders, pop=8, gen=3) ran to completion, best fitness
598.4, makespan 3.7-4.3d across baselines -- consistent in scale with
Modules 4/5's smoke windows (641.6, 612.0), no anomalies.

**Verdict:** fixed the interaction Module 5 itself flagged, directly and
substantially -- floor-domination down from 89.1% to 19.1%, real
differentiated signal up from 10.9% to 80.9%. Module 5's real PPC-1.xlsx
calibration now actually surfaces in the data for the large majority of
records, which it did not before this follow-on. All five originally-
planned modules plus this fix are now complete. Only the nine zero-
consumer tables remain before the end-of-rebuild multi-seed pipeline run.
Holding here, not proceeding further without explicit go-ahead.

---

## Module 6 — Blouse/Dress short-sleeve routing variant (bug fix, not a
## rebuild module)

Surfaced by the EDA review, not by the audit: `routing_spec.md` Section
3.3 specified an 8-step short-sleeve variant for Blouse/Dress (omit "Cuff
attach"), and `routing_tables.py`'s original comment documented it as
deliberately not implemented -- but "no real field distinguishes short-
from long-sleeve" was true and remains true, while "so don't implement the
structure at all" was an unjustified extra step. Every Blouse/Dress
product was silently defaulting to the 9-step base. Re-verified the "no
real field" premise before implementing (`grid.xls` Group/Layout/StyleId,
`Plan.xlsx` Season/GroupNo/MasterGroup -- Season is 100% null across all
1,382 rows, the rest are batch/production codes with no sleeve-length
pattern in 20 sampled Blouse/Dress values): still holds.

**Implemented:**
- `routing_tables.py`: added `WOVEN_SHORT_SLEEVE` (`WOVEN` with step 7,
  "Cuff attach", removed -- 8 steps, same terminal `Automated Pattern
  Tacker`, FOA still strictly mid-sequence). `routing_for()` gained a
  `short_sleeve: bool = False` parameter, applying only to Blouse/Dress.
- `SHORT_SLEEVE_SHARE = 0.5`: DELIBERATE SYNTHETIC CHOICE, bucket (c). No
  real field can key which specific real garments are short-sleeved, so
  which variant an individual product gets is necessarily invented -- an
  even split is the simplest defensible assumption absent any signal
  either way. Applied per-product (`random.random() < 0.5`) in the
  products loop.
- Blended the same 50/50 split into the two aggregate pre-computations
  that run before individual products exist -- machine-type demand
  (Section 3) and the PPC-1.xlsx duration calibration's mean SAM-per-step
  -- rather than leaving them assuming 100% long-sleeve. Effect is small
  (Blouse+Dress are ~11% of products) but free to do correctly.

**Backup:** `backups/pre_routing_shortsleeve_20260818_225306.sql`

**Verification 1 -- data level:**

  check                                          result
  --------------------------------------------------------------
  8-step routings exist                          21 products (was 0)
  overall n_steps distribution                   6:22:7:103:8:21:9:272:10:67
  Blouse/Dress short-sleeve share                40.4% (16/35 Blouse, 5/17 Dress)
  "Cuff attach" present in any 8-step routing     No (confirmed absent)
  terminal op for all 8-step routings             Automated Pattern Tacker (unchanged)

  40.4% against a 50% draw at n=52 is within normal sampling noise for
  that sample size, not a bug.

**Verification 2 -- test suite:** 365/365 passing (was 353) -- 12 new
tests added directly for the variant (8-step selection, default/False
still returns the 9-step base, the flag is a no-op outside Blouse/Dress,
the short variant is exactly the base minus "Cuff attach" and nothing
else, `SHORT_SLEEVE_SHARE` is a valid probability).

**Verification 3 -- structural smoke test:** graph build succeeded
(12,757 operations, 71s); GNN inference reused the existing checkpoint
(not retrained -- structure-only check); rather than a single tiny
window, `run_rolling_horizon` was run across the FULL order book (25
windows of 60 orders, pop=8, gen=3) and completed all 25 without error --
a stronger structural check than previous modules' single-window smoke
tests. Per-window on-time shape unchanged from the established pattern
(crowded early windows 8-25%, clearing to 100% by window 18+), no anomaly
introduced by the routing change.

**Verdict:** fixed as a genuine implementation gap, not a rebuild module
in the audit's original sense -- no new real data was grounded, an
already-cited spec requirement that was never wired up now is. Small,
expected downstream ripple in Modules 2/5's own aggregate numbers, from
the demand/calibration blending above: machine allocation shifted from
SNLS 888/Overlock 903/Flatlock 450/Tacker 402/FOA 357 to SNLS 878/Overlock
909/Flatlock 450/Tacker 404/FOA 359 (largest move 10 machines, SNLS down
~1.1%); `DURATION_CALIBRATION_FACTOR` shifted from 0.8301 to 0.8255
(-0.6%). Both within noise, neither module's own verdict changes. Holding
here per instruction -- the order-count module (below) is next, sequenced
as its own separate reseed so its effect stays attributable on its own.

---

## Module 7 — Purchase_Orders/Production_Orders COUNT (was invented flat
## 1,500)

Surfaced during the EDA review's PO-vs-SKU-lot granularity check (see
chat): Module 3 grounded what's inside each order (dates, quantity) but
never grounded the order COUNT itself -- the same category of gap
employee headcount had before Module 4. `for i in range(1, 1501)` was a
flat invented constant, untouched by Module 3.

**Method (same triangulation used to answer the granularity question):**
`grid.xls.PO` is a genuine real order identifier (verified: PO 5324 is
one StyleId+Color split across 33 Size rows, summing to a clean 3,300 --
exactly the "one order, split by size" shape expected) but only 334 of
5,132 rows (6.5%) carry one. For the other 93.5%, disambiguated using
StyleId+Color+BuyMonth: checked against the 139 rows that DO have a real
PO first, and confirmed `BuyMonth` is NEVER split within one known-real
PO (139/139 constant) while `StartDate` IS split within a single real PO
for 31 of 139 orders (up to 14 distinct dates -- different colour/size
splits landing on different lines/days) -- ruling `StartDate` out as a
safe order boundary and confirming `BuyMonth` as the right one.

`TOTAL_ORDERS = len(real PO groups) + len(StyleId+Color+BuyMonth groups
among the unlabeled rows) = 139 + 890 = 1,029`, replacing the flat 1,500
(a ~31% reduction). Implemented as a computed constant in
`seed_database_fixed.py`, both loop bounds (`Purchase_Orders`/
`Production_Orders` row count, and the later job-shop-schedule generation
loop's `prod_order_ids`) now derived from it instead of the literal 1501.

Provenance clarification: this calibrates the number of resampled synthetic
records, not an import of 139 original orders plus 890 original proxy tuples.
The generation loop independently samples attributes for all 1,029 records.

Correction on the record: in reporting this triangulation to the user
mid-investigation, the number given verbally (890) was a transcription
error -- the interactive analysis that produced it had already printed
the correct total (1,029) one line below the one quoted. The seeder code
itself was written directly from the correct calculation and was never
wrong; only the spoken summary was. Flagged here rather than left quiet.

**Backup:** `backups/pre_module6_ordercount_20260819_012856.sql`

**Verification 1 -- data level:**

  field                     before (1,500)          after (1,029)
  -------------------------------------------------------------------
  Purchase_Orders rows          1,500                    1,029
  Production_Orders rows        1,500                    1,029
  target_quantity range      52-3,300 (mean 446)     52-3,300 (mean 446, unchanged shape)
  po_status split          Confirmed 831/Delayed     Confirmed 509/Delayed
                            659/In_Prod 10            504/In_Prod 16
  already started               47.5%                    50.5%
  already past deadline         31.1%                    32.8%
  Job_Shop_Schedules rows      248,181                  167,691

  `target_quantity`'s distribution is unchanged in shape (same real
  `real_po_quantities` pool sampled either way) -- only the row COUNT
  changed, as expected from a pure count fix touching no other field's
  source. `already started`/`already past deadline` shifted a couple of
  points (expected: same real date pool, different resample at a smaller
  n, not a new date mechanism). Job_Shop_Schedules dropped proportionally
  with the order count, same mechanism as every prior module -- every
  upstream count change reseeds this table's full row count.

**Verification 2 -- test suite:** 365/365 passing, unchanged (this module
touches no logic any existing test covers -- a pure count constant).

**Verification 3 -- structural smoke test:** graph build succeeded (8,745
operations, 29s); GNN inference reused the existing checkpoint;
`run_rolling_horizon` completed the full order book across 18 windows (17
full windows of 60 + one 9-order remainder = 1,029 exactly) with no
errors. Same established on-time-rises-across-windows shape (13%→100% by
window 11), no anomaly from the smaller order book.

**Verdict:** implemented and verified. Order count now derived from the
same real `grid.xls` evidence Module 3 already used for what's inside
each order, closing the last open "was an invented flat constant" gap in
Purchase_Orders/Production_Orders. Confirms the reviewer's instinct that
something in the order-count/order-size relationship didn't add up, while
landing on a different, evidence-based number (1,029, a ~31% reduction)
rather than the coarser estimate first suggested in chat (890, which
turned out to be a reporting error, not a modelling one). This closes the
EDA-review-driven fixes. Full-pipeline scheduling effect deliberately not
measured yet, reserved for the end-of-rebuild multi-seed run.

---

## The nine zero-consumer tables — documented as deliberate synthetic
## choices, not real-data-grounded

Before touching any of the nine, checked precisely (traced every
reference, not just grepped table names) whether "not currently
consumed" also meant "not usefully consumable" -- a different question.
Verdict, table by table:

**Not useful even in principle, given how this model works:**
`Product_Variants` (size/color doesn't affect duration/machine assignment
in this model, only garment TYPE does), `WIP_Tracking` (duplicates
Job_Shop_Schedules' own execution_status/remaining_frac, already read by
the live pipeline), `Machine_Status_Logs` (duplicates
machine_breakdown_count_at_dispatch, already a real GNN feature),
`Delivery_Logistics` (pure post-production shipping; on-time is already
measured against the customer deadline directly, not a shipping-lag
concept).

**Genuinely useful in principle, real garment-industry mechanisms this
project doesn't model at all -- but not grounded here, deliberately:**
`Bill_of_Materials`/`Raw_Materials`/`Suppliers`/`Inventory_Stock`
together (material-availability constraints -- a real, common cause of
production delay) and `Quality_Logs` (defect/rework rates -- real
capacity consumption nothing in the duration model accounts for). The
catch: for both, the TABLES aren't what's missing -- nothing consumes
them, so grounding the data alone would add nothing. The value would only
come from building new scheduling logic (a material-availability check in
the GA, a rework-inflates-effective-duration factor), which is feature
work, not data-fidelity work, and belongs to a later module in the user's
own planned sequence (this data-rebuild phase, then graph, then
presumably GA/models) -- not built now. Flagged here explicitly so it
carries forward rather than getting lost in a documentation comment.

`Inventory_Stock` specifically also has one query function,
`fetch_inventory_state()` (`database/queries.py`) -- traced to its only
caller: that same file's own `__main__` diagnostic block, written "for
the RAG-Enabled Conversational Stock Intelligence Agent," a feature this
project's own status record confirms is configured but does not exist.
Nothing in the live pipeline calls it.

**Implemented:** no code behavior changed, no reseed needed. Added
permanent, explicit "DELIBERATE SYNTHETIC CHOICE" comments at each of the
nine tables' generation sites in `seed_database_fixed.py`, stating why
(no consumer; separately, no real source in any of the four files for
most of them) and, for the two genuinely-useful-in-principle clusters,
what would need to be built for real-data grounding to actually matter.

**Verification:** compiles clean; full test suite 353/353 (unchanged, as
expected for a comments-only change).

**Verdict:** all nine tables now carry an explicit, permanent record of
why they're synthetic rather than being silently unexplained. Two
real opportunities (material availability, quality/rework) are on record
for whenever the user's planned module-by-module review reaches the
GA/scheduling layer. This closes out the data-foundation rebuild's table
coverage entirely -- every one of the 26 tables has now been either
grounded in real data (Modules 1-5b) or explicitly, permanently
documented as a deliberate synthetic choice (this section). Remaining:
the end-of-rebuild multi-seed pipeline run for the final authoritative
numbers.
