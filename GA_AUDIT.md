# GA Module Audit

Same discipline as `DATA_AUDIT.md`/`DATA_PROFILE.md`: describe what currently
exists precisely, with real numbers measured against the live (post-Module-6,
1,029-resampled-synthetic-order) database — not what the code was originally designed to do,
and no changes made here. Three specific questions were flagged before this
audit started; each has its own section with real numbers, not an assumption.

---

## 1. What currently exists

**`ga/chromosome.py` — `ChromosomeFactory`**: builds each operation's line
eligibility (by canonical `operation_type`, not a machine-model string match),
machine pools per line (reserved/standby machines already excluded), and the
MC (machine-count) gene's ceiling — one combined `min()` over three limits:
`GA_MAX_MACHINES_PER_OPERATION` (business cap, 40), usable machines of the
required type on the assigned line, and 93% of that type's company-wide total
(the standby-reserve cap). A 4th, narrower limit applies only to the window's
single most-congested type when `MC_PROTECTION_FRACTION < 1.0` (currently
1.0 — inactive by default, see `ga/schedule_utils.py`).

Chromosome shape: `[OS array][LA array][MC array]`, each length `num_ops`.
OS = operation visit order (repaired for routing precedence at birth, not
just after crossover/mutation). LA = line assignment, chosen by
work-per-machine load balancing (`choose_line_least_loaded`, top-k=2 for
population diversity), not uniform random. MC = machines to run this
operation's lots across, in `[1, ceiling]`.

**`ga/scheduler.py` — `GAScheduler`**: owns the DEAP toolbox, per-window
setup (deadlines, urgency bands, manager-directive fuzzy boost), and the
optimization loop. Mutation rate is **derived from problem size**, not fixed:
`indpb = min(0.1, 6.0 / num_ops)` — on the real ~2,500-2,700-operation
per-window scale this measured session (see Section 3), that's `indpb≈0.0022-
0.0024`, i.e. ~6 expected gene mutations per individual regardless of window
size, not the ~600+ a fixed 0.1 rate would cause. Elitism (top 4% cloned
forward unchanged each generation) + random immigrants (10% of offspring
replaced with fresh random individuals each generation) are both explicit,
deliberate fixes for documented failure modes (`PROJECT_STATUS.txt`): plain
`eaSimple` has no elitism (best fitness could regress generation-to-generation),
and elitism alone let the population collapse to near-clones under
`tournsize=3` (reduced to 2). `optimize_islands()` is a fully parallel-fair
alternative (same total population/generations, only structure differs) —
not currently the default path (`ga/rolling_horizon.py` calls `optimize()`).

**`ga/fitness.py`**: not read in full this pass (already exercised
extensively by the fair-fight/gated-buffer work above) — the reward/penalty
term structure (on-time, makespan, labour idle, margin-vs-baseline) is
unchanged by anything in this document.

**`ga/schedule_utils.py`**: `RISK_BUFFER_AT_CERTAINTY = 0.0` (disabled;
gated-buffer ablation this session found a confidence-gated re-enable
plausible, not yet adopted — see `DELAY_LABEL_FIX_PROPOSAL.md`).
`MC_PROTECTION_FRACTION = 1.0` (bottleneck softening inactive by default).
`calculate_operation_work()` is the single duration formula every caller
shares: `setup + (serial_processing_mins / machine_count)`.

**`ga/rolling_horizon.py` — `run_rolling_horizon()`**: slides a fixed-size
`commit_size` window through the order book (sorted by release date),
carrying `machine_free`/`machine_last_order` state forward between windows.
Default production settings: `commit_size=300` (`PLANNING_HORIZON_ORDERS`),
`lookahead_size=commit_size` (L==C), `pop_size=50`, `generations=50`,
`polish_evals=400`, `seed=42`.

---

## 2. Employee coverage (flagged question 1)

**14,067 of 15,066 total employees (93.37%) appear as `employee_id` on at
least one row in the live `Job_Shop_Schedules` table.** Measured directly
(`SELECT COUNT(DISTINCT employee_id)` vs. `SELECT COUNT(*) FROM Employees`),
not estimated. This is a high coverage figure — the concern behind the
question (a workforce sitting mostly idle) does not hold up against the
current real data. The remaining ~6.6% uncovered is worth a follow-up look
(which skill/shift/line combination they belong to) if there's appetite, but
is not the large gap the question anticipated.

---

## 3. Seasonal order concentration as a candidate explanation for
   per-window collapse (flagged question 2)

**Historical per-window collapse** (`PROJECT_STATUS.txt`, the most recent
full run under the OLD invented-1,500-order book, 5 windows of commit=
lookahead=300): on-time rate **100.0% → 98.0% → 77.0% → 7.3% → 0.3%** across
windows 1-5. Diagnosed at the time (0H.3) as carried-forward per-(line,type)
machine congestion compounding window-over-window — a real mechanism, but
one that leaves open WHY the pressure compounds as sharply as it does.

**Checked directly against the CURRENT resampled synthetic order book** (1,029 orders,
Module 6's grounded count, real seasonal shape): splitting into the same
`commit_size=300` windows and measuring each window's actual order density —

| window | orders | date span | orders/day |
|---|---|---|---|
| 1 | 300 | 2026-06-27 → 2026-07-17 (20 days) | **15.00** |
| 2 | 300 | 2026-07-17 → 2026-08-28 (42 days) | **7.14** |
| 3 | 300 | 2026-08-29 → 2026-12-14 (107 days) | **2.80** |
| 4 | 129 | 2026-12-14 → 2027-07-18 (216 days) | **0.60** |

**Window density drops 25x from window 1 to window 4, monotonically.** This
is a real, structural, previously-unmeasured fact about the current order
book, and it is at least consistent with — very plausibly a root contributor
to — the 0H.3 contention-compounding mechanism: windows 1-2 sit inside the
front-loaded demand burst (per `DATA_PROFILE.md`, 60.1% of all orders land in
one quarter), so the carried-forward machine-free state handed to window 3
onward is already the debt of serving that dense burst, not an idle floor.
Window 4's orders are sparse in count but spread across 216 days — meaning
even a modest capacity backlog inherited from windows 1-2 can push their
individual (nearer-term-relative-to-their-own-release) deadlines out of
reach, independent of how many orders window 4 itself contains.

**Confirmed with a fresh run (2026-08-24), against today's real order book,
current split-encoder production checkpoint, same production settings
(commit=lookahead=300, pop=50, gen=50, polish=400, seed=42), export=False:**

| window | orders | on-time | margin vs. baseline |
|---|---|---|---|
| 1 | 300 | **11.3%** | +8.5% |
| 2 | 300 | 52.0% | +4.5% |
| 3 | 300 | 99.0% | +2.5% |
| 4 | 129 | **100.0%** | +2.4% |

Aggregate: 59.9% on-time, 30.1-day makespan, 34.1 orders/day throughput,
68.7h mean tardiness, 4.49% mean GA margin over baseline, 1,216s total
runtime.

**The seasonal-density hypothesis is confirmed as a real driver of
per-window performance — but the direction is the OPPOSITE of the old
collapse pattern.** Old data: on-time DECREASED window-over-window
(100%→98%→77%→7.3%→0.3%), later windows collapsed. Today's real data:
on-time INCREASES window-over-window (11.3%→52%→99%→100%) — the DENSE
early windows are the ones failing now, and the sparse late windows are
near-perfect.

Mechanism: window 1 starts from a completely idle machine-free floor (no
carried-forward state at all — this is the FIRST window), so 0H.3's
carried-forward-congestion mechanism cannot be what's causing its 11.3%
on-time. This is a raw demand-vs-throughput overload: 300 order releases
compressed into 20 real calendar days means their deadlines are also
compressed into roughly that window, asking the factory to clear 300
orders' worth of work faster than it can sustain. Window 4's 129 orders,
spread across 216 days, give each order far more calendar slack, hence
near-100%.

**This changes what fix the finding points toward.** "Demand-aware window
sizing" is still the right direction, but the specific problem is not
"later windows run dry for lack of orders" — it's that a fixed-size commit
window asks the factory to absorb an unsustainable release rate during the
dense early period specifically. Smaller, more numerous windows during the
dense period (not larger ones during the sparse tail) is the version of the
fix this data actually motivates. Not implemented — this section reports
the measured finding, per audit scope.

---

## 4. GA duration/efficiency assumptions vs. the rebuilt data foundation
   (flagged question 3)

**`GA_LOTS_PER_OPERATION = 60`** (`utils/config.py`): imported into
`ga/scheduler.py` but **confirmed dead** — referenced only inside a
historical bug-fix comment (the "double-division" fix), never in a live
computation. This was already checked and flagged by the seeder's own code
comment (`seed_database_fixed.py` line ~1266) before this audit, not a new
finding — included here for completeness since it's exactly the kind of
cross-file consistency question asked. The REAL average lots-per-operation,
measured directly against `Job_Shop_Schedules` (`GROUP BY production_order_id,
operation_id`, count rows): **mean 19.2, min 3, max 60** — matches what the
delay-label feature-engineering work already assumed (`~19 lots`) elsewhere
in this project. No live inconsistency: the dead constant and the real
measured average simply happen to both exist, one unused.

**`DEFAULT_LINE_EFFICIENCY = 0.85`** (`ga/scheduler.py`): a fallback only.
`GAScheduler.__init__` computes `avg_efficiency` from the REAL
`target_efficiency_percentage` column on active `Production_Lines` whenever
it's present and non-empty — which it is (99 active lines, real values).
Measured real distribution: **min 56.8%, max 98.5%, mean 77.5%**. So in
normal operation the GA uses 0.775, not the 0.85 fallback — the fallback
constant is never actually exercised on this database, but if it ever were
(e.g. a reseed that leaves the column empty), it would silently use a value
7.5 points higher than the real mean. Not a live bug, but worth knowing the
fallback itself doesn't match reality if it's ever triggered.

**`GA_MAX_MACHINES_PER_OPERATION = 40`**: business-preference ceiling, not
derived from data — no real-data consistency check applies to a policy
constant like this one.

**Net finding**: no LIVE inconsistency between the GA's duration/efficiency
assumptions and the rebuilt data foundation was found. Both places checked
(lot count, line efficiency) either already read real data directly or have
a dead/unexercised fallback rather than an active wrong number.

---

## 5. Fresh capacity floor, measured against today's real order book
   (2026-08-24) — a NEW number, not a lookup of the old 23-29 day estimates

Old floor estimates (23.1d, 30.1d) in `PROJECT_STATUS.txt` Section 1.2.2
were computed against the old invented (1,500-order, ~25-day release
spread) dataset and do not apply to the current, rebuilt real order book
(1,029 orders, 386-day sampled release spread). Recomputed from scratch
(`scratch/capacity_floor_current.py`, live DB, real routing, real
`utils/machine_reserve.py` usable-machine counts — same production code
paths `ga/scheduler.py`/`ga/chromosome.py` use, not a second duration
formula):

| bound | value | what it measures |
|---|---|---|
| Floor A (pooled, same-day availability) | 2.2 days | 6,101.2 remaining machine-days / 2,787 usable machines, all 5 types pooled — the loose, history-continuity number |
| Floor A-tight (per-type, same-day availability) | **2.8 days** | same, but per machine type (types don't share machines); bottleneck: bar-tack pocketing (1,066.8 machine-days / 375 usable) |
| Floor B / B-tight (release-date-aware) | 386.0–386.2 days | see below — mechanically correct, but NOT a meaningful comparison |

**The release-date-aware bound is real but uninformative on this dataset,
for a reason worth stating plainly**: it is dominated almost entirely by
the single latest-released order (release day 386.0, own remaining work
only 0.2 days) — i.e. it says "the whole book can't finish before its own
last order is even released," true but vacuous. Checked directly in the
production code (`ga/rolling_horizon.py`, `ga/fitness.py`) before trusting
this: `release_date` is used ONLY to sort orders into rolling-horizon
windows (`order_release` in `run_rolling_horizon`) — it is **never enforced
as an operation ready-time floor** inside a window's actual simulation
(`evaluate_schedule`'s `order_ready_times` starts every order at 0.0,
gated only by precedence, never by its own release date). So a
release-date-*gated* floor would be measuring a constraint the production
scheduler does not itself impose — not the right comparison for what the
GA actually achieves. This is itself a real, previously-unmeasured finding
about the current system, not a modeling choice made for this audit.

**The correct comparison, given what the system actually does**: Floor
A-tight, **2.8 days**, against GA_AUDIT Section 3's just-measured achieved
makespan, **30.1 days** — a **10.75x gap**. This is meaningfully worse than
any gap previously measured on the old dataset (~3.0-3.1x, PROJECT_STATUS.txt
Section 2.1/1.2.2) or the newer-routing dataset (~4.2x, Section 1.3). Same
caveat those sections carried applies here too — this is a lower bound that
ignores precedence chains and list-scheduling gaps, not a target the
scheduler should be expected to hit exactly — but the size of the gap
relative to history is real and not yet explained. The seasonal-density
finding already measured in Section 3 above (window 1's 11.3% on-time,
demand compressed into 20 days) is the leading candidate: a fixed-size
commit window absorbing an unsustainable release rate would show up
exactly as extra idle-then-scramble time inflating the achieved makespan
well past a capacity-only floor. Not confirmed as the sole cause — reported
as a fresh measurement, per audit scope, not a diagnosis.

**Superseded by Section 8, not just extended**: this section's floor and
10.75x gap were measured against a simulation later found (Section 8) to
never enforce release_date as a ready-time constraint at all — the 2.8-day
same-day floor was the right comparison ONLY because the simulation being
compared against also (bug-for-bug) ignored release dates. After Section
8's fix, the correct comparison is the release-date-aware floor computed
against the same origin the run uses, which came out to 327.3 days against
a 327.6-day achieved makespan — see Section 8 for the full, re-verified
picture; do not cite the 2.8d/10.75x numbers below as the current state.

---

## 6. Confidence-gated risk buffer — GO/NO-GO DECISION: ADOPTED (2026-08-24)

The 3-arm ablation (`scratch/gated_buffer_ablation.py`,
`scratch_gated_buffer_ablation.log`, run against the current production
split-encoder/Option-B checkpoint) had already run and passed before this
session; this was a decision, not new measurement work.

| arm | on-time | makespan | throughput | mean-tard | labour | margin |
|---|---|---|---|---|---|---|
| A_disabled | 60.3% | 32.2d | 32.0/d | 68h | 5.2% | +5.6% |
| B_broad_0.5 | 60.2% | 33.2d | 31.0/d | 72h | 5.3% | +5.0% |
| C_gated_0.5 | 60.2% | **31.4d** | **32.8/d** | 70h | 5.4% | **+5.9%** |

Gated (C) beats disabled (A) on makespan, throughput, and margin, at a
within-noise on-time cost (-0.1pp) and a small labour/tardiness cost.
Broad (B) is worse than disabled on every metric but on-time — confirming
the original 0H.5 diagnosis still holds (the model's real-but-modest signal
isn't discriminative enough to pad indiscriminately) and that gating on
confidence, not re-enabling the buffer concept alone, is what makes this a
net positive.

**Decision: ADOPT.** Implemented in `ga/schedule_utils.py`:
`RISK_BUFFER_AT_CERTAINTY` 0.0 → 0.5, new `RISK_BUFFER_GATE_THRESHOLD = 0.14`
(the confidence-gate band identified in
`scratch/delay_calibration_and_confidence_gate.py`), `apply_risk_buffer`
now returns the duration unchanged below the gate. `tests/test_scheduling_core.py`'s
`test_risk_cushion_is_currently_disabled` (which existed specifically so
re-enabling would be "a conscious act with a failing test to update, not
something that drifts back silently") rewritten to
`test_risk_cushion_is_gated_by_confidence` and asserts the new gated
behaviour. Full suite re-run: 404/404 passing.

**Caveat carried forward, not resolved here**: this was a single-seed
ablation (unlike the split-encoder promotion, which was confirmed across
two independent seeds). The direction is consistent with the diagnosed
mechanism rather than a surprise, but the exact magnitudes (31.4d vs 32.2d,
etc.) should be treated as provisional until re-confirmed under a second
seed. `RISK_BUFFER_GATE_THRESHOLD=0.14` is also a property of the CURRENT
checkpoint's predicted-probability distribution, not a universal constant —
flagged in-code to re-derive after any future delay-head retrain.

---

## 7. Demand-aware window sizing — SCOPED, NOT BUILT (2026-08-24)

Per instruction: scope only. Section 3's finding is that a fixed
`commit_size=300` window asks the factory to absorb an unsustainable
release rate specifically during the dense early season (window 1: 300
orders/20 days = 15.0 orders/day, vs. window 4's 129 orders/216 days = 0.60
orders/day — a 25x density range), and on-time rate rises monotonically
window-over-window as a direct consequence (11.3% → 52% → 99% → 100%).

**Direction the finding actually motivates**: smaller, more numerous commit
windows during the dense early period specifically — NOT larger windows
during the sparse tail (the opposite of what "demand-aware sizing" might
naively suggest, and the opposite of what Section 6's already-tried-and-
rejected overlapping-lookahead/commit-shrinking experiments did uniformly
across the whole book — `PROJECT_STATUS.txt` Section 2.2 / 0B, both ruled
out when applied UNIFORMLY. This is a different proposal: vary the window
SIZE by local order-density, not apply one fixed geometry choice
everywhere).

**Sketch, not a design**: a density-aware `commit_size(window_start_date)`
function — e.g. hold `commit_size / (orders_released_per_day near
window_start)` roughly constant instead of holding `commit_size` constant,
so window 1's ~15 orders/day gets a much smaller `commit_size` (tens, not
300) while window 4's ~0.6 orders/day keeps something close to the current
300. Open questions a real design would need to resolve, not resolved
here: (a) whether this should be computed once from the release-date
histogram before the run, or adapted window-to-window from what's actually
observed; (b) whether shrinking window 1 specifically reintroduces the
exact commit-shrinking failure mode Section 2.2/0B already measured as
worse (that finding was on the OLD dataset's release distribution and may
not transfer — would need re-measuring on this dataset, not assumed); (c)
interaction with `RISK_BUFFER_GATE_THRESHOLD`/Section 6's now-adopted
buffer, untested together. Not implemented — this section is a scoping
note per instruction, not a design proposal ready to build from.

---

## 8. Release-date enforcement — IMPLEMENTED, VERIFIED (2026-08-24)

Section 5 found `release_date` was used ONLY for rolling-horizon window
sorting, never as an operation ready-time floor inside the simulation
itself. Before implementing a fix, the mechanism was confirmed with real
numbers (per instruction — not assumed from the plausible story alone):
`scratch/release_date_enforcement_check.py` replicated window 1's real
300-order/20-day release spread and found it is itself bursty (65/300
orders, 21.7%, release on day 0 alone) and that replacing "all 300
available at t=0" with real per-order release-date arrival reduces peak
same-instant machine-type backlog **80–86% across all 5 types** (e.g. body
assembly: 304.9 → 43.5 machine-days). Mechanism confirmed before writing
any fix.

**Implemented**: `order_release_minutes` threaded through
`GAScheduler.__init__` → `ga/fitness.py::evaluate_schedule`'s ready-time
computation (`order_ready = max(order_ready_times.get(order_id, 0.0),
release_floor)` — binds only on an order's first operation; later
operations are already governed by precedence past the floor).
`ga/rolling_horizon.py` and `run_baselines()` both pass it through the
same call, so the GA, its own local search, and the FIFO/EDF/CriticalRatio/
RandomRepaired baselines all read the identical ready-time definition —
one place computes this, not several. `agent/decision_engine.py`'s
`_export_proposed_schedule` (which mirrors, rather than calls,
`evaluate_schedule`'s ready-time logic, to decode the winning chromosome)
needed the identical fix independently, or the exported schedule would
have silently diverged from what was scored — exactly the failure class
this file's own comments already warn about for `select_machines_and_start`.
4 new regression tests (`tests/test_release_date_ready_time.py`); full
suite 408/408 passing.

**Single consistent re-run** (`scratch/corrected_simulation_run.py`): capacity
floor, GA, and all four baselines computed together, one process, one
origin, one seed — not baselines re-added piecemeal on top of a
still-broken assumption.

| | before (release date unenforced) | after (enforced) |
|---|---|---|
| window 1 on-time | 11.3% | 11.3% (unchanged) |
| window 2 on-time | 52.0% | 51.0% |
| window 3 on-time | 99.0% | 93.0% |
| window 4 on-time | 100.0% | 96.1% |
| aggregate on-time | 59.9% | 57.3% |
| makespan | 30.1d | **327.6d** |
| throughput | 34.1 orders/d | 3.1 orders/d |
| labour utilisation | ~4-5% | 0.5% |
| margin vs. baseline | +4.49% | +2.6% |

**Why makespan jumped 10.9x, and why that is correct, not a regression**:
56.4% of the book's operations (4,929/8,745) have a real release date
already in the past as of the run's origin (today) — the book's earliest
release is 2026-06-27, ~58 days before this run — and clamp to
release_days=0 exactly as intended. The remaining 43.6% release across the
following ~327 days. The old (buggy) simulation scheduled ALL of that
future-released work as if available immediately, which is not a
scheduling achievement, it is asking the model to complete orders that in
reality do not exist yet. 327.6 days is the honest number for how long it
takes to finish today's real, currently-known order book, including orders
not released until next year.

**Window-by-window mechanism, confirmed by the SAME re-run, not assumed**:
window 1's on-time is EXACTLY unchanged (11.3% both times) — its orders are
among the earliest-released in the whole book, so under the real origin
they were ALREADY clamped to release_days=0 even before this fix; window
1's poor on-time was never actually caused by the release-date bug, and
Section 3's original diagnosis (dense early-season demand overload)
stands, unweakened, as the explanation for window 1 specifically. Windows
3-4's on-time DROPPED (99.0%→93.0%, 100.0%→96.1%) rather than improving —
diagnosed, not assumed: the old bug gave those windows' orders an
unrealistic head start (starting them upon whatever wall-clock-equivalent
minute the carried-forward machine state reached, potentially months
before their real release date), which manufactured artificial deadline
slack. Enforcing the real, later release date removes that unearned slack,
so the post-fix 93-96% is the more trustworthy number even though it is
numerically lower. The pre-implementation mechanism check (peak-backlog
reduction from spreading arrivals) was real and is not contradicted by
this — it applies to FUTURE-released orders, which now correctly wait; it
does not apply to window 1's already-elapsed backlog, which is a different
slice of the book than the diagnostic's window-relative framing implied.

**Capacity floor, recomputed against the SAME origin the real run used**
(`scratch/capacity_floor_origin_fix.py` — an origin bug was caught and
fixed first: the initial floor script measured release offsets from the
book's earliest release date, not from "now" with past dates clamped, and
so was not directly comparable to the achieved makespan; recomputing
against the real run's exact origin timestamp resolved an apparent
achieved-beats-the-floor impossibility that was flagged and chased down
before being reported):

  Floor B-tight (release-date-aware, per-type, real origin): **327.3 days**
  Achieved makespan (same run):                              **327.6 days**
  Ratio: 1.001x

The corrected simulation now lands almost exactly on the theoretical
floor. Read this precisely, not as "the schedule is now perfectly
efficient throughout": both numbers are dominated by the same single
latest-released order (release day ~327.3) — the floor is a statement
that nothing can finish before that order even exists, and the achieved
makespan finishes shortly after it does. It is not evidence of even,
optimal utilisation across the whole 327-day span — the labour-utilisation
collapse (4-5%→0.5%) and the in-flight-order counts below both show the
opposite: the factory is genuinely under-loaded relative to elapsed
calendar time for most of the run, because real demand actually is this
sparse across most of the year (consistent with, and further evidence
for, [[seasonal-concentration-ga-window]]'s 60%-in-one-quarter finding).

**Orders genuinely in flight over time** (real per-order release/completion
times from this run, `ga/rolling_horizon.py`'s aggregate now carries
`order_release_minutes`/`order_completion_minutes`, merged across windows
the same way `lateness` already was):

| day | in-flight | released (cum.) | completed (cum.) |
|---|---|---|---|
| 0 | 577 | 577 | 0 |
| 1 | 573 | 578 | 5 |
| 2 | 488 | 583 | 95 |
| 5 | 221 | 609 | 388 |
| 10 | 70 | 631 | 561 |
| 15 | 34 | 659 | 625 |
| 20 | 42 | 675 | 633 |
| 30 | 52 | 707 | 655 |
| 45 | 35 | 750 | 715 |
| 60 | 44 | 803 | 759 |
| 90 | 35 | 870 | 835 |

Peak simultaneous in-flight: **577 of 1,029 orders (56.1%), on day 0** —
replacing the old model's implicit "all 1,029 at once." This is not itself
a modelling artefact: 577 orders' real release dates genuinely already
passed as of today (matching the 56.4% operation-level clamp rate above),
so they are legitimately all real, currently-outstanding, not-yet-complete
orders competing for capacity simultaneously in reality. After that
initial backlog clears (in-flight drops under 100 by day 10), the factory
runs with only 30-70 orders in flight at a time for nearly the entire
remaining ~317 days — the real, corrected answer to how this order book
actually asks the factory to operate.

**This replaces any "300 orders at a time" framing anywhere in this
project's documents.** `commit_size=300` is a rolling-horizon WINDOW-SIZE
config value (how many orders get planned/committed together per pass) —
it was never a claim about how many orders are simultaneously competing
for capacity on the shop floor, but earlier sections of this document
(and PROJECT_STATUS.txt) described the demand picture in per-window terms
without this real concurrent-count data to check it against. The real
number is 577 at the peak, collapsing to 30-70 for nearly the whole run —
use this table, not a "300 at once" mental model, when reasoning about
actual factory load going forward.

### GA vs. baselines — the first apples-to-apples comparison this project
    has produced

Every earlier GA-vs-baseline comparison in this project (PROJECT_STATUS.txt,
`GA_AUDIT.md` Sections 1-7) was measured against the "all orders exist on
day one" distortion — baselines and the GA both benefited/suffered from it
equally in relative terms, but neither was being judged against the real
problem. This run's baselines came from the SAME corrected simulation as
the GA (`run_baselines()` and the GA both call `evaluate_schedule` with the
identical `order_release_minutes`), captured directly, not re-run
separately:

| window | FIFO | EDF | CriticalRatio | RandomRepaired | **GA (post-polish)** | GA margin |
|---|---|---|---|---|---|---|
| 1 | 946.3 / 9.0d / 10.7% on-time | 896.5 / 8.4d / 10.0% | 833.0 / 7.9d / 10.3% | 882.5 / 7.2d / 11.3% | **802.0** / 11.3% on-time | **+3.7%** vs CriticalRatio |
| 2 | 1255.2 / 20.8d / 52.0% | 1254.7 / 23.3d / 53.3% | 1071.2 / 16.5d / 51.0% | 1157.9 / 17.8d / 53.0% | **1012.4** / 51.0% on-time | **+5.5%** vs CriticalRatio |
| 3 | 5776.3 / 118.9d / 30.3% | 3462.9 / 119.6d / 96.3% | 4691.8 / 121.5d / 40.7% | 6247.2 / 118.8d / 19.7% | **3431.9** / 93.0% on-time | **+0.9%** vs EDF |
| 4 | 12949.9 / 331.3d / 36.4% | 8415.2 / 327.6d / 96.1% | 9952.5 / 327.6d / 58.1% | 15236.3 / 335.3d / 13.2% | **8403.7** / 96.1% on-time | **+0.1%** vs EDF |

(fitness / makespan / on-time; lower fitness is better; `UrgencyTiered`
omitted — it is numerically identical to `CriticalRatio` in every window
here, expected when no orders are manager-prioritized, not a new finding.)
Aggregate mean margin across all 4 windows: **+2.6%**.

**The real finding: the GA's advantage over simple dispatching rules is
load-dependent, not a flat edge.** In the tight, demand-dense early windows
(1-2, where even the GA's own on-time is far from 100%), the GA beats the
best baseline by a meaningful +3.7-5.5%. In the slack, sparse tail windows
(3-4, where release dates now spread work across months), that margin
collapses to +0.9% and then +0.1% — in window 4, the GA's own pre-polish
best fitness (8415.2) is EXACTLY EDF's baseline fitness (EDF was seeded
into the initial population and nothing the search tried beat it; local
search itself closed only 0.14% more). When slack is abundant, a simple
earliest-deadline-first rule is very nearly as good as the full GA search.
This is a genuinely new result — every prior margin number in this
project's history was measured under the release-date bug and should not
be read as still representative of the GA's real value over baselines.
