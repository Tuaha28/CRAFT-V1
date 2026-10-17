# GA and Agent Module Post-Mortem — Real Execution Trace

Same standard as `DATA_AUDIT.md`/`GA_AUDIT.md`: every claim here is tied to
a real number pulled from an actual run against the live database, not a
description of intended behaviour. Four real runs produced everything in
this document:

1. `scratch/ga_agent_postmortem_part1_2.py` — window 1 (300 orders, dense
   early period), full production settings (pop=50, gen=50, polish=400,
   seed=42), current production defaults (risk buffer ON at 0.5/0.14 per
   `GA_AUDIT.md` Section 6; release gate OFF — not adopted, per
   `WLC_RELEASE_GATE_PROPOSAL.md` Section 11).
2. `scratch/ga_agent_postmortem_part3.py` — real disruption assessment
   against the top 15 real machines by queued/running work.
3. `scratch/ga_agent_postmortem_part3b.py` — follow-up against the
   scarcest machine type (Tubular Sleeve Join), to check whether Part 3's
   finding was an artefact of which types got sampled.

All three ran 2026-08-25, against the live database, real GNN predictions
from `outputs/checkpoints/gnn_multitask_latest.pth` (the split-encoder
production checkpoint).

---

## 1. Order intake — real numbers

### 1a. Day-0 eligible count

Real query against `Production_Orders`/`Routing_Operations`, ORIGIN =
2026-08-25T19:32:55:

**578 of 1,029 active orders (56.2%) have `release_date <= now`.**

This confirms `GA_AUDIT.md` Section 8's 577-order finding — 1 order of
drift, consistent with the ~1 day of real time that has passed between
that measurement and this one (order 209's release date, right at the
boundary in earlier checks, is exactly the kind of case a day's drift
moves across the line). **The finding still holds under the current
production config.** Because the release gate is OFF by default (Section
11's tradeoff was reported, not resolved), every one of these 578 orders
is immediately admitted into GA scheduling with zero workload-norm
pacing — this number is not hypothetical, it is what the live system
actually hands the GA on any given day.

### 1b. One order traced end-to-end: order 4

Order 4 has the earliest release date in the entire active book
(2026-06-27) — chosen so the trace shows an order that has been sitting
in the system the longest. Window 1 committed 7 of its operations. Real
exported decisions (`outputs/proposed_schedule_postmortem.json`,
`_export_proposed_schedule`'s actual XAI output — not a paraphrase):

| op | line | machines | employees (tier) | start | Delay Risk (GNN) |
|---|---|---|---|---|---|
| 2721 | LINE06 | 9 machines (905, 1041, 1045, ...) | 9 (batch) | 2026-08-26 05:36 | 0.23 |
| 2722 | LINE08 | 8 machines (957, 1053, ...) | 8 (batch) | 2026-08-26 14:50 | 0.17 |
| 2723 | LINE01 | 11 machines (42, 123, ...) | 11 (**factory**-sourced — line batch couldn't cover it) | 2026-08-26 21:02 | 0.12 |
| 2724 | LINE01 | 10 machines | 10 (batch) | 2026-08-28 07:16 | 0.08 |
| 2725 | LINE06 | 4 machines | 4 (batch) | 2026-08-28 07:31 | 0.09 |
| 2726 | LINE04 | 5 machines | 5 (batch) | 2026-08-28 07:55 | 0.07 |
| 2727 | LINE09 | 5 machines | 5 (batch) | 2026-08-28 08:10 | 0.14 |

Real, verbatim XAI reasoning for operation 2721 (the system's own
generated text, not summarized): *"Assigned to LINE06 (52.5h committed at
time of assignment) over 98 other eligible line(s) including LINE05
(21.2h, 31.2h less loaded) (Delay Risk: 0.23). At least one assigned
machine was last running a different order — 25min re-rigging charged to
it. Split across 9 machine(s) running in parallel on this line ... staffed
by 9 operator(s) ... Order's running completion trajectory is now
~1278.1h past its 2026-07-04 deadline."*

Every operation picked the line with real committed-hours displayed
against real alternatives (e.g. op 2723 chose LINE01 at 89.0h committed
over LINE02 at 19.7h — a genuinely more-loaded choice than the visibly
cheaper alternative, meaning the GA's own line-assignment gene, not the
export step, drove this; the exporter is reporting the GA's decision, not
making its own). No operation's Delay Risk crossed `HIGH_RISK_THRESHOLD`
(0.75), so `high_risk_reroutes: 0` for the whole window — the buffer
padded these durations by less than half a percent each (GNN delay
probabilities 0.07–0.23, all below the 0.14 gate... actually above it for
2721/2722/2723/2727, so those four DO carry the confidence-gated buffer;
2724/2725/2726 at 0.07–0.09 are below the 0.14 gate and carry zero
cushion). Release gate: N/A, off by current default.

Order 4's deadline (2026-07-04) was **already ~1278–1330 hours (53–55
days) in the past** before this schedule was even built — this order was
not made late by the GA; it arrived already unrecoverable. This is a real,
individual instance of the systemic finding below (267 of 300 orders in
this window are past deadline, 265 of those already past before
scheduling began).

---

## 2. GA — real generation-by-generation evolution

### 2a. Population seeding — real composition

`pop_size=50`, 5 strategies, round-robin — but slot 49 (the last) is
force-overwritten by the single best pure dispatching heuristic
(`ga/scheduler.py::optimize`). Real composition, this run:

```
{'workload-balanced': 10, 'edf': 10, 'critical-ratio': 10, 'fifo': 10, 'randomized': 9}
  + 1 CriticalRatio clone (fitness 873.5) overwriting the last 'randomized' slot
```

### 2b. Manager-priority gene fraction — a DIFFERENT real number than the
    historical claim

Order 4 (window 1's earliest-release order) set as `manager_prioritized_orders`.
Measured directly from `GAScheduler.gnn_predictions` after construction —
no full GA run needed for this specific number:

**7 of 2,541 operations in this window (0.275%) carry the priority boost.**

This does NOT match the "0.67%" figure referenced when this post-mortem
was requested. Searched for that figure's origin in `PROJECT_STATUS.txt`
and `GA_AUDIT.md` — not found in either; it isn't written into this
project's own standing docs, so it can't be reconciled against a
documented prior methodology. **Reporting the current real number (0.275%)
rather than confirming an untraceable one.** The mechanism is small by
construction: window 1 has 2,541 operations across 300 orders (~8.5
ops/order on average), so any single order's boost is inherently a small
fraction of the whole window regardless of which order is chosen — 7 ops
for this specific order is unremarkable for that average.

Real, measured effect on the same baseline heuristics (no full GA needed
— `run_baselines()` on two `GAScheduler` instances differing only in
`manager_prioritized_orders`):

```
              plain      forced     delta
FIFO          947.35     947.07     -0.279
EDF           898.23     897.96     -0.268
CriticalRatio 873.52     873.24     -0.279
UrgencyTiered 873.52     873.24     -0.279
RandomRepaired883.38     883.23     -0.150
```

A real, consistent, but genuinely tiny effect: every heuristic's fitness
improves by 0.15–0.28 (out of ~900) purely from the priority-reward term
on 7 operations. **"Diluted but real" is the right characterization,
confirmed freshly** — just at a different, smaller magnitude (0.275% of
genes, ~0.03% fitness shift) than the number this task cited.

### 2 (main). Real per-generation fitness trajectory — full log, not summarized

```
gen   0  nevals  50  avg  1059.22  min   808.08
gen   1  nevals  42  avg  1223.25  min   808.08
gen   2  nevals  48  avg  1231.43  min   808.08
...  (min stays EXACTLY 808.08 through every generation from 0 to 34)  ...
gen  34  nevals  41  avg  1135.69  min   808.08
gen  35  nevals  38  avg  1103.61  min   807.80   <- only change in 50 generations
gen  36  nevals  32  avg  1111.65  min   807.80
...  (min stays EXACTLY 807.80 through every remaining generation)  ...
gen  50  nevals  46  avg  1175.55  min   807.80
```

Full log in `scratch_ga_agent_postmortem_part1_2.log`. Real numbers:

- Gen-0 min: **808.08**. Final (gen 50) min: **807.80**.
- **Real improvement from evolution alone, 50 generations: 0.035%.**
- Exactly ONE improving event in the entire 50-generation run (gen 35),
  everything else is the elite/immigrant machinery holding the line flat.

**The "evolution improves almost nothing beyond construction" finding
still holds — and is sharper than previously documented.**
`ga/scheduler.py::polish`'s own docstring states "the GA's own operators
contribute ~0.4% over the initial population" as the prior measurement;
this fresh run on the fully-rebuilt system measures **0.035%**, roughly
an order of magnitude smaller. Local search polish immediately after
contributes **+0.615%** in 152 evaluations (accepted: 4 swap, 5 line, 3
machines; rejected 139) — **local search moved the fitness ~17.5x more
than 50 generations of evolution did.** This is not a new finding
(`ga/scheduler.py::optimize_islands`'s own docstring already frames the
GA's edge as coming from "WHERE work runs... not WHEN work is ordered"),
but this is the first time it's been re-measured end to end against the
current, fully-rebuilt system with real numbers, not inherited from
before the routing/label/split-encoder work.

### 2c. Multi-solution diversity — real fitness AND real decisions

3 solutions returned (`n_solutions=3`):

| solution | fitness |
|---|---|
| 1 | 807.80 |
| 2 | 812.36 |
| 3 | 817.59 |

Fitness gaps are small (solutions 1–3 differ by only 1.2%), but the
underlying decisions are NOT small variations of each other:

| pair | fitness diff | line assignment differs on |
|---|---|---|
| 1 vs 2 | 4.56 | **2,516/2,541 operations (99.0%)** |
| 1 vs 3 | 9.80 | **2,506/2,541 operations (98.6%)** |
| 2 vs 3 | 5.23 | **2,503/2,541 operations (98.5%)** |

Real, confirmed: these are three genuinely different ways to run the
factory (line assignment reshuffled on essentially every operation),
converging to near-identical fitness through completely different
resource-allocation paths — exactly what `_select_diverse_solutions`'s
Hamming-distance filter is designed to surface, and it is doing so on
real data, not a theoretical guarantee.

---

## 3. Agent — real disruption handling, traced end to end

**Scope decision, stated plainly**: `agent/event_handler.py::handle_machine_breakdown`
is the real top-level production entry point, but it MUTATES the live
database (`UPDATE Machinery_Assets SET current_status='Breakdown'`, and
`apply_local_patch`'s `UPDATE Job_Shop_Schedules`). Running that
repeatedly to build a "batch of simulated disruptions" would corrupt the
live database's real machine/schedule state as a side effect of an audit.
This section calls `agent/impact_assessor.py::assess_machine_breakdown`
directly instead — the identical real assessment logic (real DB queries,
real fuzzy-gate tier decisions, real candidate-slot search, real
donor-preemption logic), fully READ-ONLY. Nothing was written back.

### One real machine breakdown, traced

Machine 2576 (Bar-Tack Pocketing), the real machine with the most
queued/running work in the live database: 36 affected operations, 325
scheduled-capacity alternates, 23 standby-reserve machines available.

Real per-operation fuzzy-gate output (verbatim):

```
order 186 op 2347: slack=-2.8d (urgent=1.00, moderate=0.00, relaxed=0.00)
  impact=0.0h (negligible=1.00, moderate=0.20, severe=0.00)
  rule strengths: NO_ACTION=1.00, LOCAL_PATCH=0.00, FULL_RESCHEDULE=0.20
  -> tier=NO_ACTION, candidate_asset_id=2238
```

**All 36 of machine 2576's affected operations landed NO_ACTION.**
`impact_hours=0.0` for every one: an idle alternate machine (asset 2238,
among others) was found instantly at `ready_after` for every single
affected operation — the breakdown adds zero measurable delay. Several
affected orders already carry deeply negative slack (order 186:
-2.82 days, order 798: -4.82 days — both already overdue before this
breakdown), which is why `urgent=1.00` fires at full strength on nearly
every one (the fuzzy gate's own design: `urgency_score` saturates at 1.0
for anything at or past `SLACK_URGENT_FULL=3` days) — but since impact
stays at 0.0h regardless of how urgent the order already is, `R1: impact
is Negligible -> NO_ACTION` wins every time (`decide_tier`'s rule base,
`agent/fuzzy_logic.py`).

### Batch — real tier distribution across real machines, two samples

**Sample A** (15 real machines, ranked by real queued/running-op count —
dominated by Bar-Tack Pocketing/Body Assembly/Seam Edge Overlock, the
larger-alternate-pool types): 517 affected operations assessed.

**Sample B** (5 real Tubular Sleeve Join machines — the scarcest type,
only 300 live-usable machines company-wide per Section 9's live-capacity
fix — specifically chosen to check whether Sample A's result was an
artefact of sampling only large-pool types): 165 affected operations
assessed.

**Combined real result, 682 affected operations across both samples:**

| tier | count | % |
|---|---|---|
| NO_ACTION | 682 | **100.0%** |
| LOCAL_PATCH | 0 | 0.0% |
| FULL_RESCHEDULE | 0 | 0.0% |

**Real donor-preemption rate: 0/682 (0.0%). Real standby-reserve draw
rate: 0/682 (0.0%).**

This is a genuine, cross-type-confirmed finding, not a sampling fluke on
one machine type: given today's real machine counts (including the ~10%
live-breakdown rate Section 9 found) and real scheduled workload, a
single machine breaking down is *always* instantly absorbed by an idle
alternate, on every real machine checked, in the type with the most
alternates AND the type with the fewest. This is consistent with, and
further evidence for, this project's standing finding that the factory
has substantial spare machine capacity relative to real demand (labour
utilisation 0.5% under the corrected simulation, `GA_AUDIT.md` Section 8;
same-day capacity floors of 1.8–2.8 days against a much larger real order
book, Section 5).

### Manager-priority override, effect on a rush order

Covered in Section 2b above with real numbers (0.275% of genes, real
fitness deltas of -0.15 to -0.28) rather than repeated here — that
measurement already traces the override from `manager_prioritized_orders`
being set through to its real, measured effect on GA fitness, satisfying
this section's ask without a second expensive GA run duplicating the same
mechanism.

---

## 4. Honest gaps — stated directly, not papered over

1. **Donor preemption and LOCAL_PATCH were never observed firing.** The
   code path is real, implemented, and specifically designed (single-level
   preemption, re-verifying the donor's own safety before committing —
   `agent/impact_assessor.py::_attempt_donor_preemption`), but across 682
   real affected operations spanning the largest- and smallest-alternate-
   pool machine types, it never activated even once, because impact was
   0.0h in every single case. **Whether this code path is reachable at
   all under today's real data is genuinely unconfirmed** — it would need
   either a machine type at real capacity limits, or several simultaneous
   breakdowns competing for the same scarce alternates, neither of which
   this real sample happened to contain. Not fixed or forced here; flagged
   as a real observability gap, not silently assumed to work.
2. **No LOCAL_PATCH ever executed for real**, so "exactly which specific
   operations got moved where" (as asked) has no real instance to report
   from this run. What IS reported is what the code's own computed fields
   (`candidate_asset_id`, `candidate_start_time`, `donor_plan`) WOULD
   contain if one occurred — a real, live-computed answer to "what would
   happen", not an observed "what did happen", and the distinction is
   preserved rather than blurred in Section 3.
3. **`handle_machine_breakdown` and `handle_rush_order_insertion`
   themselves were not executed** (Section 3's scope decision) — the
   real, underlying assessment/GA-fitness mechanisms they call WERE
   exercised for real, but the top-level orchestration (DB writes, the
   full `trigger_rescheduling()` pass with `n_solutions=3` under a live
   manager-priority flag) was not run end to end as a single call. A
   deliberate scope choice to avoid mutating the live database as an
   audit side effect, not an oversight — but it means the very top layer
   of the agent stack is traced by its real component parts, not as one
   unbroken real invocation.
4. **The historical "0.67% of genes" figure could not be located** in
   `PROJECT_STATUS.txt` or `GA_AUDIT.md` to confirm against — only the
   "~0.4%" evolution-improvement figure was found (in
   `ga/scheduler.py::polish`'s own docstring). Section 2b reports a fresh,
   real, current number (0.275%) rather than a false confirmation of a
   citation that couldn't be traced.
5. **Parts 1–2 and Part 3 come from two separate real runs**, roughly 5
   minutes apart (19:32 vs 19:37–19:42). Both are real and current, but
   live machine-breakdown status and order deadlines drift minute to
   minute in this database, so the two parts are not from the exact same
   instant — noted rather than implied to be a single atomic snapshot.
6. **Everything in Parts 1–2 is window 1 only** (the 300 densest,
   earliest-release orders) — the generation trajectory, population
   seeding, and multi-solution diversity numbers are real for this window
   but were not re-measured across windows 2–4, which GA_AUDIT.md Section
   3 already showed behave very differently (window 1: 11.3% on-time;
   window 4: 100%). Whether the "0.035% evolution / 0.615% local search"
   split holds in a slack, sparse-demand window was not checked here.

---

## 5. Follow-up (2026-08-25) — the mutating path confirmed, and a real,
    previously-unknown finding it surfaced

Gap 1 and 2 above were closed, not left as permanent assumptions, per
explicit follow-up instruction: confirm the NO_ACTION/donor-preemption
result against the REAL mutating `agent/event_handler.py::handle_machine_breakdown`
path (not just the read-only assessment), and deliberately construct a
scenario to try to force donor preemption to fire for real.

**Method**: the live database was cloned in full (26 tables,
`scratch/clone_db_for_disruption_test.py` — `CREATE TABLE ... LIKE` +
`INSERT ... SELECT`, server-side, same local MySQL instance) into a
disposable `garment_fyp_db_postmortem_test` database. All mutating calls
ran against the clone only (`DB_NAME` env var override, asserted in code
before any write: `assert 'postmortem_test' in DB_NAME`). The clone was
dropped at the end — production was never written to.

### 5a. Real mutating confirmation — and a genuine, previously-unknown gap

Ran the actual `ShopFloorEventHandler.handle_machine_breakdown(2310)` (the
real top-level entry point, not the read-only assessment) against the
real busiest machine in the clone. Before: 36 real `Job_Shop_Schedules`
rows on asset 2310, status Active. After:

- `Machinery_Assets.current_status`: Active → Breakdown (real write,
  confirmed).
- **All 36 of the original rows still show `asset_id=2310`, unchanged —
  confirmed directly by re-querying, not inferred.**

*(An earlier version of this specific check joined on `production_order_id`
+ `operation_id` only and produced a nonsensical negative count — a bug
in the diagnostic script itself, caught and fixed before being reported:
`Job_Shop_Schedules` is LOT-level, not operation-level, so that join
matched far more rows than intended. Re-verified with a direct
`asset_id=2310` count instead — 36 before, 36 after, unambiguous.)*

**This is a real, previously-unknown finding: NO_ACTION performs ZERO
database writes.** `agent/event_handler.py::handle_machine_breakdown`'s
own loop (`if a['tier'] == Tier.NO_ACTION.value: continue`) skips
entirely — even though the assessment computed a real
`candidate_asset_id` with zero predicted impact, that candidate is never
applied. The operation's schedule row keeps pointing at the machine the
system has just marked `Breakdown`. **This means every one of the 682
NO_ACTION verdicts measured in Section 3 — and by extension, the entire
"breakdowns are trivially absorbed" finding — describes what the
ASSESSMENT predicts would happen, not something the system actually
executes.** Whatever mechanism (if any) re-resolves this before the
operation's real start time was not identified in this investigation —
flagged as a genuine, real gap, exactly the kind of thing the read-only
check alone could not have surfaced, which is why re-confirming against
the real mutating path mattered.

### 5b. Forcing scarcity on Tubular Sleeve Join — still no donor preemption,
    a real and informative negative result

Targeted the scarcest machine type specifically. Found a real TSJ
operation (order 790, op 761) with genuine, non-overdue slack (18 days —
required, since donor preemption needs a donor with STRICTLY lower
urgency than the affected order, which is structurally impossible if the
affected order is already urgency-saturated; most orders in this database
are, per Section 1b). Then, for real, in the clone:

- Broke **354 of 359 real Tubular Sleeve Join machines** company-wide
  (`UPDATE Machinery_Assets SET current_status='Breakdown'`), sparing only
  5 — a **98.6% simulated equipment loss** for this type, far beyond any
  naturally-occurring scenario.
- Confirmed via direct query: 5 real `current_status='Active'` TSJ
  machines remained company-wide.
- Ran `assess_machine_breakdown` against the target machine's 30 real
  affected operations under this real scarcity.

**Result: still 100% NO_ACTION, 0% donor preemption.** Even at 5 of 359
machines remaining for an entire type, the real committed schedules on
those 5 machines had enough genuinely idle time to instantly absorb all
30 affected operations with zero measured delay. This is a strong,
consistent reinforcement of this project's broader capacity finding
(`GA_AUDIT.md` Section 8: 0.5% labour utilisation under the corrected
simulation) — real demand is so far below real capacity that even a
near-total simulated loss of the scarcest machine type does not create a
genuine scheduling conflict. **Donor preemption remains unconfirmed as
reachable under this database's real demand profile even under
deliberately extreme, real, executed scarcity** — a negative result,
reported as such, not as a failure of the test design.

### 5c. A related, real, code-level observation — flagged with the right caveat

Across both this run and Section 3's original trace, the SAME
`candidate_asset_id` was recommended for multiple different affected
operations in a single assessment call (real, observed: machine 2576's
first 5 sampled assessments all returned `candidate_asset_id=2238`).
Reading `agent/impact_assessor.py::assess_machine_breakdown` explains why:
`committed_by_asset` (each alternate machine's committed schedule) is
fetched ONCE before the per-operation loop and never updated as the loop
tentatively assigns operations to slots — so two affected operations from
the same breakdown could both be assessed as fitting the same idle gap on
the same alternate, independently, each unaware of the other's claim.

**Stated precisely, not overclaimed**: this is a real code-level pattern,
corroborated by real repeated `candidate_asset_id` values in actual
output — but it was NOT observed causing an actual double-booking,
because no scenario in this entire investigation (this session's, or the
original 682-operation trace) ever produced two-or-more LOCAL_PATCH
verdicts from the same breakdown event that would need to write to the
same slot; every real assessment came back NO_ACTION. Whether this would
actually corrupt a real schedule if LOCAL_PATCH ever did fire at scale is
a real, open question this investigation could not execute a test for,
since it never got a LOCAL_PATCH to occur at all — flagged for whoever
next gets a real LOCAL_PATCH scenario to check.

## 7. GA improvement work — closing the 17.5x gap (2026-08-25, later)

Directly targets Section 2's headline finding (evolution alone: 0.035%;
local search: 0.615% in the same run) — flagged as one of this project's
most important results. Two steps, both with real numbers, before any
third-tier idea gets built.

### 7a. Step 1 — real offspring-vs-parent fitness distribution

`scratch/ga_offspring_fitness_diagnostic.py`: replicated
`_advance_generation`'s exact logic (no production code changed) across a
real 50-generation window-1 run, capturing each selected individual's
fitness immediately before `varAnd` (crossover+mutation) and comparing
against its post-operation fitness for every individual DEAP's own
fitness-invalidation marked as genuinely touched.

**1,871 real offspring measured. 19.08% improved, 0.00% neutral, 80.92%
worse.** Of the improving moves, mean gain 47.3 fitness points (median
31.1, max 311.6). Of the worsening moves, mean damage **183.5** — roughly
**4x** the size of a typical gain. This is the direct mechanism behind
Section 2's finding: crossover/mutation is a noisy operator whose harmful
moves are both far more common AND far larger than its rare good ones;
elitism's job is fighting a strongly negative-EV operator pool, not
polishing an already-good one. Local search, by contrast, is
strict-improvement-only by construction (`ga/local_search.py`) — every
accepted move is a real gain, none of the noise.

### 7b. Step 2 — memetic integration, the main experiment

**Implemented** in `ga/scheduler.py::optimize()`: new `memetic_interval`/
`memetic_evals` parameters (default `None` — reproduces prior behaviour
exactly, byte-for-byte, confirmed by test). When set, the current best
individual is polished with a small evaluation budget every N
generations; if genuinely improved (strict-improvement guarantee
inherited from `ga/local_search.py`), it replaces the population's WORST
individual — never an elite, never shrinking diversity — so it becomes
available as a real PARENT for crossover/mutation from the next
generation onward, letting gains compound instead of firing once at the
end. 6 new tests (`tests/test_ga_orchestration.py`) cover: default
no-op equivalence, legality of memetic-produced chromosomes,
elitism-never-regresses still holding, reproducibility, and — the core
guarantee — memetic can only match or beat the same seed's non-memetic
result. Full suite 455/455 passing.

**Real comparison** (`scratch/ga_memetic_comparison.py`), window 1, same
seed (42), full production settings, `memetic_interval=5`,
`memetic_evals=30` (10 small polish calls across the run, 3 genuinely
improved and got injected — at generations 5, 20, 50):

| | evolution-only | final (post end-polish) | on-time | makespan | mean-tard | runtime |
|---|---|---|---|---|---|---|
| Baseline (end-only polish) | 807.74 | **802.67** | 11.0% | 5.36d | 60.9h | 270s |
| Memetic (periodic + end polish) | **798.54** | **798.54** | 11.0% | 5.36d | 61.2h | 237s |

**Memetic's evolution-only result (798.54) already beats the baseline's
FULLY-POLISHED result (802.67)** — a real 0.515% improvement on top of
the entire current production pipeline, not just on top of raw evolution.
The end-of-run polish pass found literally nothing left to improve on the
memetic arm (+0.000%, 45 evaluations, 0 accepted) — everything it would
have found, periodic in-loop polishing had already captured and let
evolution build further on. Ran slightly FASTER than baseline (237s vs
270s) despite the extra polish calls, because the now-better final
population needed far less end-of-run search. On-time and makespan were
identical between arms (window 1's dense-early-season overload dominates
those regardless of GA fitness quality, consistent with `GA_AUDIT.md`
Section 3); mean tardiness ticked up marginally (60.9h→61.2h) — reported
honestly as within single-seed noise, not hidden.

**This single-seed, single-window result did NOT hold up under
validation — see Section 7c immediately below before treating this
number as a finding.** Flagged here exactly as it was measured, in the
order it was measured, rather than edited after the fact: this is what a
plausible-looking first result looks like before the cross-seed/
cross-window check that this project's discipline requires runs.

### 7c. Validation (2026-08-25, later) — the +0.515% did NOT replicate;
    reported honestly, not defended

Per instruction, before trusting Section 7b's magnitude: a cross-seed
check (window 1, seed 123 vs. the original seed 42) and a window-
generalization check (window 2, seed 42, tested standalone under the
same idle-floor convention window 1 was), same "one variable at a time"
discipline as the split-encoder's two-seed cross-check
(`scratch/ga_memetic_validation.py`).

| test | baseline final | memetic final | delta |
|---|---|---|---|
| A: window 1, seed 123 | 766.42 | 794.49 | **-3.662%** (memetic WORSE) |
| B: window 2, seed 42 | 912.24 | 912.24 | **+0.000%** (no effect) |

**Neither check confirmed Section 7b's result. This is a real reversal,
not a footnote — the earlier "confirmed real win" framing was premature
and is retracted here, not defended.**

**Test A, understood mechanistically, not just observed**: 5 of the 10
memetic calls genuinely improved and got injected (gens 5, 10, 15, 20,
25: 802.57 → 794.49, each one a real, verified gain at the moment it was
made). Then NOTHING further improved for the remaining 25 generations
(gens 30-50, 5 more attempts, zero injections) — evolution stalled
exactly where the memetic injections left it, and the 400-eval end-of-run
polish also found nothing more. Meanwhile the NON-memetic baseline's own
evolution, with no injected individual dominating selection, reached
766.42 — a genuinely better result. The most likely explanation, offered
as a hypothesis consistent with the observation and standard memetic-
algorithm literature, NOT independently proven here: repeatedly replacing
the population's worst individual with a near-clone of the (repeatedly
re-polished) best reduces population diversity generation over
generation, and by relying on `tools.selTournament(tournsize=2)`, that
increasingly-dominant near-clone family gets selected disproportionately
often — accelerating convergence onto a single basin, which on this
seed's trajectory happened to be worse than the more diverse population
baseline evolution retained access to. Elitism's own regression guarantee
still held throughout (the logbook `min` never got WORSE, confirmed by
`test_memetic_never_regresses_generation_to_generation`) — the population
"as a whole" simply converged to a worse basin than the non-memetic run's
population did, a distinct failure mode elitism was never designed to
prevent.

**Test B, understood mechanistically**: only 2 of 10 memetic calls found
anything (912.32 → 912.31 → 912.24, a combined 0.08-point move on a
~912-point scale), and stopped finding anything after generation 10.
Critically, the BASELINE arm's own independent end-of-run polish also
converged to exactly 912.24 — meaning this window's fitness landscape
simply has very little local-search-reachable improvement available at
all, end-of-run or in-loop. The 0.000% delta is not evidence memetic
integration is "neutral by design" — it is evidence that on this window,
neither integration strategy finds much local search can do, so there
was nothing for the injection mechanism to help OR hurt with.

**What Step 1's diagnostic still stands as** (Section 7a: 80.92% of
crossover/mutation offspring are harmful, local search moves are
strict-improvement-only) — that mechanism is unaffected by this
validation and remains a real, measured fact about the operators
themselves. What does NOT stand is the specific conclusion that
periodically injecting a polished best individual, replacing the
population's worst, is a reliable way to exploit that fact — this
validation shows it can substantially help (window 1, seed 42), do
nothing (window 2, seed 42), or substantially hurt (window 1, seed 123),
apparently depending on population-diversity dynamics this integration
strategy does not currently protect against.

**Consequence for the memetic_interval feature**: stays implemented and
tested (Section 7b — the code is correct, the tests pass, elitism's own
guarantee genuinely holds), but is NOT recommended for adoption as any
kind of default given this 1-for-3 real record. If pursued further, the
mechanism-consistent next step would be a diversity-preserving injection
policy (e.g., only inject if the polished individual is not too similar
to what is already in the population, mirroring
`_select_diverse_solutions`'s own Hamming-distance filter) rather than
unconditionally overwriting the population's worst member — not
implemented here, flagged as the concrete next hypothesis this
validation itself points to.

### 7d. Third-tier idea (domain-aware crossover) — correctly held, not started

Per instruction: gated on Steps 1-2 landing cleanly. They did not (7c) —
memetic integration's real record is mixed, not confirmed. Building a
larger, more expensive third-tier change on top of an intervention that
just reversed on its own validation would repeat the exact mistake this
validation pass was designed to catch. Holding, as instructed, until
memetic integration itself is either fixed (the diversity-preserving
injection hypothesis above) and RE-validated, or abandoned.

### 7e. Follow-up — does gentler injection avoid the diversity collapse?
    (2026-08-25, later) — inconclusive, and why, reported honestly

Per instruction: before writing memetic integration off as a dead end,
test whether injecting less aggressively — `memetic_replace='random'`
(overwrite a random non-elite slot instead of always the worst) and/or a
longer `memetic_interval` (15 generations instead of 5, fewer injections
total) — recovers a positive result on the specific case that reversed in
7c: window 1, seed 123. Implemented `memetic_replace` as a new parameter
on `ga/scheduler.py::optimize()` (default `'worst'`, unchanged behavior;
4 new tests, full suite 459/459 passing), then ran a single fresh script
(`scratch/ga_memetic_gentler_injection.py`) with 4 arms sharing one
process and one `datetime.now()` origin — baseline, the original
`'worst'`/interval-5 policy, `'random'`/interval-5, and `'worst'`/
interval-15 — all on window 1, seed 123, full production settings.

| arm | evolution-only min | final (post end-polish) | vs. baseline |
|---|---|---|---|
| 1: baseline (no memetic) | 802.17 | 795.08 | — |
| 2: `'worst'`, interval=5 (original policy) | 788.66 | 788.66 | **+0.807%** |
| 3: `'random'`, interval=5 (gentler target) | 790.96 | 790.96 | **+0.518%** |
| 4: `'worst'`, interval=15 (gentler frequency) | 794.59 | 791.51 | **+0.449%** |

**The -3.66% failure did not reproduce.** In this fresh run, the exact
policy that lost to baseline by -3.66% in Section 7c (`'worst'`,
interval=5) instead beat baseline by +0.807% — and was the BEST of all
three memetic variants tested, not the worst. Both gentler alternatives
(`'random'` targeting, longer interval) also beat baseline, but by LESS
than the original aggressive policy did. **This directly contradicts the
hypothesis this follow-up set out to test**: if gentler injection were
protecting against a real diversity collapse, it should have outperformed
the original `'worst'`/interval-5 policy here. It did not — in this
instance, more aggressive injection won.

**Why this is inconclusive rather than a second reversal, and the real
methodological gap it exposes**: "window 1, seed 123" is not a frozen
dataset — it is a *rank-based selection* (the 300 earliest-release orders
out of the live order book) plus an RNG seed, re-evaluated against
`datetime.now()` at whatever moment the script runs. Between Section 7c's
validation run and this follow-up, real time passed and the live database
moved forward (order completions, releases, possibly machine-status
changes) — so "window 1, seed 123" run today is a genuinely different
optimization problem instance than "window 1, seed 123" run when 7c was
measured, despite the identical label. This script's 4 arms ARE validly
comparable to each other (same process, same origin timestamp, same
order set) — the internal ranking above is real. But this script does
NOT constitute a replication attempt of 7c's specific failure, and the
two results (-3.66% then, +0.807% now, both nominally "window 1, seed
123") cannot be averaged or treated as two draws from the same
distribution. This is a real gap in how "cross-seed validation" has been
done throughout this project: it assumes a stable dataset behind a fixed
window/seed label, which does not hold for a live, wall-clock-driven
database. Any future GA experiment that needs true reproducibility across
script runs on different days would need to snapshot the order book
(e.g., a frozen CSV/table copy) rather than re-querying live state —
flagged as a gap, not fixed here, since building that harness is a bigger
investment than this "small, cheap follow-up" was scoped for.

**Honest bottom line**: the gentler-injection hypothesis was tested and
found no support — in the one instance where a direct comparison was
possible, the original, more aggressive `'worst'`/interval-5 policy
outperformed both proposed alternatives. But because the specific failure
this follow-up aimed to fix did not reproduce today, this is not a
clean "hypothesis rejected" result either — there was no reproduced
diversity collapse in this run for a gentler policy to have prevented.
Real record across all measurements to date, with the live-database
caveat above attached to every one of them: window 1/seed 42 → +0.515%,
window 1/seed 123 (7c) → -3.66%, window 2/seed 42 → 0.000%, window
1/seed 123 (7e, later timestamp) → 'worst' +0.807% / 'random' +0.518% /
'worst'-interval-15 +0.449%. Four positive, one negative, one null, all
on a mechanism now known to be sensitive to a moving dataset in ways not
previously accounted for. **Memetic integration is not being called a
dead end, but it is also not being recommended for adoption** — the
honest state is that this project does not yet have a validation
methodology capable of giving it a clean verdict either way. Code and
tests for both `memetic_replace` policies stay in place, opt-in, off by
default. Domain-aware crossover (7d) stays held — this follow-up gives
no new reason to build on top of memetic integration.

### 7f. Frozen-snapshot re-run — the corrected, final verdict
    (2026-08-25, later)

Per instruction: build a reusable frozen order-book snapshot mechanism
(the fix for the reproducibility gap 7e found), then re-run the original
three-test memetic validation — window 1/seed 42, window 1/seed 123,
window 2/seed 42 — on equal footing for the first time, and only then
decide memetic integration's real status.

**The mechanism, now a standard, reusable tool**: `database/ga_snapshot.py`
(9 real tests against a live throwaway clone, all passing; full suite
468/468) clones every table from the live DB into a persistent, named
database (`garment_fyp_db_ga_snapshot_{name}`, same server-side `CREATE
TABLE ... LIKE` + `INSERT ... SELECT` pattern already used for disposable
test clones) and records the origin timestamp — captured once, at build
time — in a one-row metadata table inside the snapshot. Any script calls
`ga_snapshot.activate_snapshot(name)` before any other project import
(redirects `DB_NAME`, same env-override mechanism the disposable clones
already used) and reads `ga_snapshot.get_snapshot_origin(name)` instead of
calling `datetime.now()`, so every future run against the same snapshot
name shares the exact same data AND the exact same "now". Built the
standing snapshot `20260825` (26 tables, origin `2026-08-25 22:34:38`) —
this is now the standard input for GA validation work; a new snapshot
should be built (new name, old one left alone) only when a genuinely
fresher order book is needed for some other reason.

**Re-run** (`scratch/ga_memetic_frozen_snapshot_validation.py`, 8 arms, one
process, one frozen snapshot, one shared origin):

| test | baseline final | memetic (`'worst'`, interval 5) final | delta |
|---|---|---|---|
| window 1, seed 42 (the original "win" case) | 800.27 | 801.19 | **-0.115%** |
| window 1, seed 123 (the original "reversal" case) | 792.57 | 784.06 | **+1.073%** |
| window 2, seed 42 (the original "null" case) | 914.75 | 914.78 | **-0.003%** |

**The confound is confirmed, directly**: with the live-data drift removed,
the pattern from Sections 7b/7c essentially inverted. The case that
originally looked like a clean win (window 1, seed 42, +0.515%) is now a
statistical wash, slightly negative (-0.115%). The case that originally
looked like a damaging reversal (window 1, seed 123, -3.66%) is now the
single best result measured anywhere in this investigation (+1.073%).
Only the null case (window 2, seed 42) held steady in both the confounded
and frozen runs (0.000% then, -0.003% now) — consistent with Section 7c's
own explanation for that case: the window's fitness landscape has very
little local-search-reachable improvement available regardless of when or
how you inject it, so there was nothing for data drift to distort either.
**The earlier "+0.515% win" and "-3.66% reversal" were never a real
seed-42-wins/seed-123-loses pattern — both numbers were artifacts of
comparing two different, silently-drifted datasets under the same label.**

**Gentler-injection variants, re-measured under the same frozen
conditions** (window 1, seed 123, same 792.57 baseline): `'worst'`/
interval-5 (original, aggressive) +1.073%, `'random'`/interval-5 +0.193%,
`'worst'`/interval-15 (less frequent) +0.247%. This reproduces, now
validly, Section 7e's directional finding: the original aggressive policy
remains the strongest of the three, and neither gentler alternative closes
the gap to it. Under sound methodology, gentler injection still shows no
advantage.

**The real, final verdict — recommend / hold / discard**: **HOLD**, as an
opt-in tool, not adopted as the project default. Reasoning: across three
genuinely independent, methodologically clean test cases, memetic
integration never produced a real loss (worst measured effect: -0.115%,
indistinguishable from noise) and once produced a genuine, sizable gain
(+1.073%) — a materially safer record than either extreme this project
believed at different points (neither the confident "+0.515% win" nor the
alarming "-3.66% regression" survives). But three test cases is still too
small a sample to justify making this the default for every GA run, and
there is still no known way to predict, ahead of a run, whether a given
window/seed will land in the "genuine win" bucket or the "no effect"
bucket. Recommendation: keep `memetic_interval` implemented, tested, and
available as an explicit opt-in (default `None`/off, unchanged) for cases
where a user is willing to spend the extra ~250s and take an outcome that
is worst-case neutral and best-case a real improvement — but do not flip
it on by default until either a larger controlled sample or a predictive
signal for which cases benefit is available. Domain-aware crossover's hold
is revisited on its own terms directly below (7g), rather than carried
forward on 7d's original reasoning, which this section has just
invalidated.

### 7g. Revisiting the domain-aware crossover hold — the old reason is
    gone; here is the real one

7d's stated reason for holding was explicit: "Building a larger, more
expensive third-tier change on top of an intervention that just reversed
on its own validation would repeat the exact mistake this validation pass
was designed to catch." That reason is now known to be **false** — memetic
integration did not reverse; 7f showed the reversal was a confound
artifact, and under valid methodology the real record is zero losses
across three independent tests and one genuine gain. Continuing to cite
7d's original wording after this section would be citing something this
document itself has disproven. It is retracted, not carried forward.

**That does not automatically mean the hold should lift, though — "the old
reason is wrong" and "there is no good reason" are different claims, and
only the first one has been established.** The honest question is whether
a *different* argument for continued caution actually holds up. Two
considerations, weighed directly rather than assumed:

*Against continuing to hold*: memetic integration is now confirmed safe
(no real loss in three clean tests) and genuinely valuable in at least one
real case (+1.073%). Domain-aware crossover targets the same diagnosed
mechanism (Step 1, 7a: 80.92% of crossover/mutation offspring are harmful,
harmful moves ~4x more damaging than beneficial ones gained) — that
diagnostic is untouched by the confound and stands on its own. There is a
real, evidence-backed case that the standard operators are leaving
real gains unclaimed.

*For continuing to hold, on different grounds than 7d gave*: two gaps,
not one. First, Step 1's diagnostic — the actual causal justification for
building a domain-aware crossover — has itself only ever been measured
on a single instance (window 1, one seed, `scratch/ga_offspring_fitness_
diagnostic.py`, Section 7a). This project just spent real effort
discovering that a single-instance GA measurement, no matter how
mechanistically well-explained at the time, can misrepresent what holds
generally — that is exactly what happened to the original "+0.515% win."
The 80.92%-harmful figure has not yet been checked against the same
cross-instance discipline just applied to memetic integration, and it
would be inconsistent to demand that discipline for a wrapper parameter
but skip it for the larger investment. Second, domain-aware crossover is
a structurally different kind of change from memetic integration: memetic
integration is an opt-in, off-by-default wrapper with a hard safety net
(elitism's monotonicity guarantee, `memetic_interval=None` fully reverts
it) that never touches the core operators every individual passes
through. A crossover operator change replaces something every individual
in every generation is built from — a subtle bug or systematic bias there
has a larger blast radius and is not something a run can be un-committed
from after schedules are written out. A structurally riskier, harder-to-
revert change reasonably earns a higher evidentiary bar than a bounded,
reversible one, independent of how the wrapper's own validation turned
out.

**Conclusion**: the hold continues, but on a narrower and more honest
basis than 7d stated — not "memetic integration is unconfirmed" (it
isn't, anymore), but "the specific diagnostic that would justify this
particular bigger investment has only been measured once, and this
project now has the tool (`database/ga_snapshot.py`) to check that
cheaply before committing to the larger change." The concrete next step,
if this is pursued at all, is a cheap re-validation of Step 1 itself —
re-run `scratch/ga_offspring_fitness_diagnostic.py`-style instrumentation
across the same three frozen-snapshot test cases used in 7f, and see
whether the 80.92%/4x-asymmetry figures hold up or turn out to be as
landscape-dependent as memetic integration's benefit was. That is a
diagnostic, not a build — consistent with this project's standing
discipline of measuring before implementing — and is not being started
here, per instruction.

### 7h. Step 1 re-validated across the same 3 frozen-snapshot cases —
    the mechanism holds everywhere; the magnitude is landscape-dependent

Re-ran the exact Step 1 instrumentation (`scratch/ga_offspring_diagnostic_
frozen_snapshot.py`, no prod code changed, same `_advance_generation`
replication as the original `ga_offspring_fitness_diagnostic.py`) across
window 1/seed 42, window 1/seed 123, and window 2/seed 42 — the same
three frozen-snapshot cases from 7f, same snapshot, same origin.

| case | touched | improved | worse | mean gain | mean damage | damage/gain |
|---|---|---|---|---|---|---|
| window 1, seed 42 | 1,813 | 18.59% | **81.41%** | 43.47 | 183.44 | 4.22x |
| window 1, seed 123 | 1,830 | 16.83% | **83.17%** | 41.97 | 177.97 | 4.24x |
| window 2, seed 42 | 1,780 | 10.17% | **89.83%** | 111.16 | 525.42 | 4.73x |

**Neither of the two clean outcomes the instruction anticipated is exactly
what happened — the honest result is a third thing, reported plainly
rather than forced into either bucket.** It did not hold flatly consistent
(window 2 is meaningfully worse than window 1 on both seeds). But it also
did not vary the way the memetic result did — memetic integration's delta
changed *sign* across cases (-0.115%, +1.073%, -0.003%: sometimes helped,
sometimes didn't, no case ever clearly harmful). Here, crossover/mutation
is net-harmful in **every single one of the three cases, never once
dipping below 81%** — the mechanism itself (most genetic operations make
things worse, not better) is the most consistent finding in this entire
investigation, replicating within 1.76 points across two different seeds
on the same window (window 1: 18.59% vs 16.83% improved — the kind of gap
seed noise alone produces) and only diverging meaningfully across a
genuinely different window (window 2: 10.17% improved, an 8-9 point drop
from window 1's ~17-19%, roughly 5x the seed-to-seed noise band, and a
real reduction in absolute count too — 181 improving moves out of 1,780
touched, versus 337 out of 1,813 on window 1/seed 42).

**This is not a contradiction of the case for domain-aware crossover —
it is a refinement of it, and it lines up with independent evidence
already in this document.** Window 2 was also the case where memetic
integration showed essentially no effect (7f: -0.003%, baseline and
memetic converging to the same value) — Step 1's re-validation now
explains why: window 2's genetic operators are already closer to
whatever this landscape's exploitable ceiling is (fewer of their moves
are improving, and the ones that do improve are proportionally smaller
relative to the damage the harmful ones do — 4.73x asymmetry vs
~4.2x on window 1). Two independent measurements (Step 1's offspring-harm
rate, Step 2's local-search-injection benefit) now agree on the same
landscape property for the same window, measured two different ways.

**Verdict on the domain-aware crossover hold**: this is real grounds to
move forward, on a standard actually stronger than memetic integration's
own record — the core premise held in 3 out of 3 clean, independent,
frozen-snapshot test instances (never once did crossover/mutation turn
out to be net-beneficial), where memetic integration's own benefit only
held in 1 of 3. The hold from 7g is lifted on the evidence; what it should
NOT do is promise a uniform-size win — expect a bigger effect on
window-1-like dense/early instances and a smaller one on window-2-like
slack instances, the same pattern Step 2 already showed. Consistent with
this project's standing discipline (and the reasoning in 7g itself, which
argued a bigger, harder-to-revert change earns a higher evidentiary bar
than a small reversible one — that bar has now been cleared), the next
step is to scope the actual operator design before writing any code, not
to jump straight to implementation.

### 7i. Crossover-vs-mutation decomposition (2026-08-26) — damage
    concentrates in crossover, confirmed before any design was written

Per instruction: before writing a design proposal, confirm WHICH operator
the diagnosed harm actually concentrates in — 7a/7h's "touched by varAnd"
measurement never distinguished crossover from mutation, since DEAP's
`varAnd` applies both to the same offspring pool in one pass. A
code-level mechanism already implicated crossover specifically
(`ga/operators.py::custom_crossover` draws two independent, unrelated
cut-point sets — `pt1,pt2` for LA/MC at line 211, a separate `a,b` inside
`order_crossover()` at line 52 — reconciled only by a validity-only
repair), but that was a hypothesis from reading the code, not yet
measured. `scratch/ga_crossover_vs_mutation_diagnostic.py` replicates
`varAnd`'s exact two-phase loop (same pairing, same draw order) with
fitness captured at three points per individual — pre-operation,
post-crossover-before-mutation, and post-mutation — so crossover's effect
and mutation's effect can be measured separately, including for
individuals touched by both. Same 3 frozen-snapshot cases, same snapshot.

| case | crossover-only worse% | mutation-only worse% | mutation's marginal effect (on top of crossover) |
|---|---|---|---|
| window 1, seed 42 | **83.60%** | 58.74% | 58.22% IMPROVED |
| window 1, seed 123 | **85.37%** | 55.92% | 55.92% IMPROVED |
| window 2, seed 42 | **90.51%** | 80.00% | 45.20% IMPROVED |

**Confirmed, cleanly, without exception: crossover is the dominant source
of harm in all 3 cases, consistently 25-30 points worse than mutation
alone in window 1, and still meaningfully worse than mutation alone in
window 2 (90.51% vs 80.00%).** This directly supports the code-level
mechanism — the hypothesis is not being carried forward on the strength
of the narrative alone; the targeted measurement backs it. Mutation's
combined-effect numbers track close to crossover's (crossed+mutated
individuals are ~83-92% worse, nearly identical to crossover-only) simply
because crossover dominates that bucket's outcome — not evidence mutation
is equally harmful there.

**A genuine surprise, reported as found, not fitted to the narrative**:
mutation's *marginal* contribution — what it does starting from a
just-crossed individual, not from the original parent — is net
IMPROVING in 2 of 3 cases (58.22%, 55.92%) and nearly balanced in the
third (45.20%). Mutation is not simply "less harmful than crossover" —
on this evidence it behaves closer to a small repair/polish step applied
after crossover's damage, more often helping than hurting. This was not
predicted going in and is not something this proposal will lean on
further without separate confirmation — flagged here because it is real
and mechanistically interesting (LA/MC mutation already uses
`choose_line_work_aware`, a domain-aware, load-informed choice, unlike
crossover's positional swap — plausibly why a mutation applied after
crossover tends to partially undo crossover's damage rather than compound
it), but it is not the question this diagnostic was run to answer.

**Consequence**: `DOMAIN_AWARE_CROSSOVER_PROPOSAL.md` is written on this
confirmed basis — crossover, not mutation, is where the diagnosed harm
concentrates, in all 3 independently-tested cases.

### 7j. TCOX implemented and validated end to end — final verdict HOLD,
    same category as memetic integration (2026-08-26)

Full sequence completed on `DOMAIN_AWARE_CROSSOVER_PROPOSAL.md`'s own
terms: Triple-Coupled Order Crossover (TCOX) and the cheaper
`shared_cutpoint_crossover` comparison arm implemented in
`ga/operators.py` (Section 9), legality-verified first
(`tests/test_domain_aware_crossover.py`, 11 tests, full suite 479/479 —
and a real, honestly-documented finding along the way: `repair_precedence`
can re-decouple a small residual set of pairings TCOX gets right, a
pre-existing mechanism shared with `custom_crossover`, not a TCOX bug),
then a direct mechanism check (Section 10: TCOX's crossover-only harm-
rate reduction is real on window 2, 90.51%→80.74%, but negligible on
window 1 under both seeds), then the full GA comparison (Section 11).

**The full GA result does not confirm a net win**: TCOX final fitness vs.
baseline — window 1/seed 42: **-0.324% (a genuine regression)**, window
1/seed 123: -0.011% (flat), window 2/seed 42: **+0.371% (a real win,
consistent with the mechanism check)**. The regression on window 1/seed
42 is the most important honest finding here: Section 10 measured
essentially no difference in crossover-only harm rate on that case
(0.12 points, noise), yet the full GA run still came out measurably
worse — proof that an unchanged AGGREGATE harm rate does not guarantee an
unchanged outcome, since which specific individuals get modified (and
how) still changes, with downstream effects on population dynamics an
aggregate statistic can't see. `shared_cutpoint_crossover` was not
competitive with baseline anywhere.

**Final verdict: HOLD — not recommended as a default, same category as
memetic integration's own final verdict (Section 7f).** A real,
mechanism-backed win on one of three clean cases, a real regression on
another, a wash on the third: this project's second GA-operator
intervention in a row to land on a mixed, landscape-dependent record
rather than a clean confirmation. Both operators stay in the codebase,
tested, opt-in, never wired into `GAScheduler`'s default — this entire
investigation had zero effect on the system's actual behaviour throughout.
Not recommended for further investment without a clearer, mechanism-
confirmed reason to expect broad benefit — same discipline the whole
domain-aware-crossover thread was built on from Section 7g onward.

### 7k. GA-refinement investigation CLOSED — three interventions
    evaluated, not adopted (2026-08-26)

This closes the GA-refinement thread opened in Section 7a. Stated
plainly, not left open pending another round: **three independent,
carefully diagnosed, carefully validated interventions were evaluated
against this problem's actual GA and refinement stage. All three were
evaluated, not adopted.**

| intervention | mechanism targeted | verdict | where |
|---|---|---|---|
| Memetic integration | inject locally-polished individuals into the evolutionary population | HOLD | Section 7f |
| TCOX | domain-aware crossover, fixing the confirmed OS/LA/MC decoupling mechanism | HOLD | Section 7j, `DOMAIN_AWARE_CROSSOVER_PROPOSAL.md` |
| Sequential ALNS | replace the one-shot local-search polish with adaptive destroy/repair | HOLD | `ALNS_REFINEMENT_PROPOSAL.md` Sections 9-10 |

**None of the three is a settled "this never works" verdict on its
underlying idea** — each hold is scoped to the specific design actually
tested, and each document says so explicitly. But none cleared its own
pre-committed adoption bar either, and none is pending a further round on
the same design: this is a close, not a pause.

**The ALNS result is the strongest of the three holds, worth stating
precisely why rather than filing it under the same label by default.**
Memetic integration and TCOX both showed genuinely inconsistent,
landscape-flipping results ACROSS validation attempts — the same
intervention scoring a real win on one run and a real loss on another,
never fully settled on whether the search mechanism itself was sound.
ALNS is different: when its first validation failed, the specific
confound that could have explained the failure (a broken, near-inert
acceptance mechanism) was identified, fixed, and re-tested once — and
the recalibrated search, now demonstrably exploring hundreds of real
accepted moves per run instead of a handful, reproduced the IDENTICAL
outcome (`ALNS_REFINEMENT_PROPOSAL.md` Section 10). That is a hold that
ruled out its own most likely confound before landing on the negative,
not one left wondering whether a different run would have gone the other
way — the most decisive and best-supported of the three.

**One correction folded into this closure, not glossed over**: a
follow-up attempt to trace window 2's `high_delay_risk` improving moves
in ALNS operation-by-operation could not reproduce them
(`ALNS_REFINEMENT_PROPOSAL.md` Section 11) — the "100%-real-sample,
genuinely strong signal" framing from Section 10 did not survive a direct
reproduction check and is retracted there, not repeated here. The
underlying question (does `high_delay_risk` removal have real,
exploitable value on window-2-like landscapes) stays open, held to a
lower evidentiary standard than originally stated.

**What carries forward**: all three implementations stay in the
codebase — `ga/scheduler.py`'s `memetic_interval`, `ga/operators.py`'s
`triple_coupled_crossover`, `ga/alns.py`'s `alns_refine` — tested,
opt-in, never wired into any default path. This entire multi-week
investigation had zero effect on the system's actual production
behaviour throughout. The next candidate, if pursued, is scoped
separately in `ALNS_REPAIR_SCORING_PROPOSAL.md`: not another search
mechanism, but the fixed, deterministic, always-least-loaded-line repair
rule every one of these three interventions inherited unexamined.

### Updated honest-gaps status

Gaps 1 and 2 from Section 4 are now answered with real evidence, not
closed by assumption: **donor preemption was deliberately, genuinely
tested against a real 98.6%-scarcity scenario and still did not fire** —
this is a confirmed negative result, not an unconfirmed gap anymore. What
remains genuinely open: whether ANY real, naturally-occurring (not
artificially constructed) scenario in this database would ever trigger
it, and the double-booking risk in 5c, which needs a real LOCAL_PATCH
event to actually test. Section 7e found a new one — **GA experiments run
against the live database are not reproducible across different wall-
clock times under a fixed window/seed label**, since the underlying order
book itself drifts — and Section 7f closed it: `database/ga_snapshot.py`
now provides a standard frozen-snapshot mechanism, and the re-run under it
confirmed the drift really was distorting results (Section 7f's table).
Any future GA cross-run comparison should use `ga_snapshot.activate_snapshot()`
rather than querying the live DB directly.

---

## 8. Methodology finding — a silent live-database-drift confound
    inverted two independently "confirmed" results

This is reported as its own finding, not a footnote inside the memetic
work that surfaced it — the failure mode is general, not specific to GA
improvement or to memetic integration, and the fix is a reusable
methodological tool, not a one-off patch.

**What happened.** Two results in this project, each treated as settled
at the time it was measured, turned out to be artifacts of silently
comparing two *different* underlying optimization problems across
different wall-clock times, not real differences produced by the
intervention under test:

| result | as first measured (Section 7b/7c) | re-measured under a frozen snapshot (Section 7f) |
|---|---|---|
| window 1, seed 42 | "+0.515% win" — memetic integration confirmed working | **-0.115%** — a statistical wash |
| window 1, seed 123 | "-3.66% reversal" — memetic integration confirmed harmful | **+1.073%** — the strongest real result in this entire investigation |

Both signs inverted. This was not a small effect on a magnitude, and not
noise around a stable conclusion — the entire narrative ("seed 42 wins,
seed 123 loses") was constructed from two numbers that turned out to
describe different datasets, not different outcomes of the same
experiment.

**Root cause.** "Window 1, seed 123" was never a fixed dataset — it names
a *rank-based selection* (the 300 earliest-release orders in the live
order book) plus an RNG seed, evaluated fresh against `datetime.now()`
every time a script runs. Every element that selection depends on is live
and continuously moving: order completions and releases change which
orders fall in "window 1," `Machinery_Assets.current_status` changes
usable capacity, and the GNN predictions themselves are recomputed from a
graph built out of these same live tables. None of this is specific to
memetic integration or even to the GA — any script that queries this
project's live database and labels its result by a window/seed pair
carries the same risk, because the label describes a *selection rule*,
not a *snapshot*.

**Why it stayed hidden this long.** Every individual run was internally
consistent and passed its own sanity checks — reproducible for a fixed
seed within one process, legal chromosomes, elitism's monotonicity
guarantee holding generation-to-generation. The confound only exists
*between* runs separated by wall-clock time, which is exactly the
situation cross-seed/cross-window validation is designed to create
(deliberately re-running "the same" test at a different moment) — so the
validation methodology itself was the thing manufacturing the false
signal, not any bug in the code being validated.

**The fix, as a reusable contribution.** `database/ga_snapshot.py` (built
2026-08-25, Section 7f; 9 real tests, full suite 468/468) clones the live
database into a persistent, named snapshot and freezes the "now" origin
alongside it, so any script that calls `activate_snapshot(name)` /
`get_snapshot_origin(name)` gets identical data and an identical clock
regardless of when it actually runs. This is now a standing project tool,
not a disposable script — `PROJECT_STATUS.txt`'s STANDING PRACTICES note
records when to use it going forward: any comparison spanning more than
one script execution, not just GA/memetic work.

---

## 9. Methodology finding — aggregate parity does not guarantee outcome
    parity

This is reported as its own finding, same treatment as Section 8, not a
footnote inside the TCOX narrative that surfaced it — the failure mode is
general, not specific to crossover operators or to this GA.

**What happened** (full detail in Section 7i/7j): TCOX was measured,
directly and correctly, against the confirmed mechanism it targets — the
crossover-only offspring-harm rate. On window 1/seed 42, that measurement
came back essentially unchanged from baseline (83.60% vs. 83.48%, a
0.12-point difference, well within noise for a ~1,300-observation
sample). By every reasonable reading of that diagnostic, TCOX should have
had no measurable effect on this case. **The full GA run on the exact
same case came out measurably WORSE (-0.324% final fitness) — not flat,
a real regression.**

**The general lesson**: an aggregate before/after statistic that compares
an intervention's effect on a POPULATION-LEVEL measure (here: what
fraction of crossover offspring are worse than their parent) can show "no
change" while the intervention still measurably changes the downstream
outcome — because the aggregate rate does not capture WHICH specific
individuals are affected, or how. Two operators can produce statistically
identical harm rates while making entirely different individual-level
edits, and those different edits can interact differently with everything
downstream of them — selection pressure, population diversity, which
individual becomes an ancestor of the final generation — in ways the
aggregate statistic was never designed to see. This is not a new failure
mode invented by TCOX: it is the same class of mechanism this project
already flagged, as an unproven hypothesis, in Section 7c's
diversity-collapse explanation for memetic integration's own reversal —
here it is confirmed to matter in practice, not just plausible in theory.

**Why this matters beyond crossover operators**: any time this project
(or future work on it) uses a component-level or aggregate diagnostic as
a proxy for full-pipeline outcome — "this operator's own measured
behaviour didn't change" — that should be treated as informative, not
sufficient. A clean mechanism-level measurement is necessary evidence,
not conclusive evidence, for a full-system claim. The only way to know
whether an intervention changed the actual outcome is to measure the
actual outcome, at the level the outcome is defined — which is exactly
why Section 11's full GA comparison was run as its own, separate step,
not skipped once Section 10's mechanism check looked clean on two of
three cases. The general practice this suggests going forward: a
component-level diagnostic can rule a hypothesis IN (it is at least
plausible this intervention does something) but should never be treated
as having ruled the full-system question OUT or IN on its own.

---

## 10. Methodology note — RETRACTED: the GNN-inference-noise hypothesis
    was tested directly and did not hold up (2026-08-26)

Originally stated here as a general risk: that PyTorch GNN inference is
not guaranteed bit-reproducible across separate process runs, and that
any feature derived from it (`gnn_predictions[op]['delay']`) feeding a
hard cutoff would inherit that fragility. **This was a reasonable
hypothesis at the time it was written, offered explicitly as unconfirmed
("stated as a hypothesis... not chased down further") — it has since
been tested directly and does not hold up, and is retracted here rather
than left standing.**

`ALNS_REPAIR_SCORING_PROPOSAL.md` Section 7 ran the identical GNN forward
pass as three fully independent process invocations, specifically to test
this claim. All three produced BIT-IDENTICAL `delay` predictions to six
decimal places, across 450 real comparisons, zero differences anywhere.
In this environment, on this model, GNN inference is reproducible across
process runs, not fragile.

**Consequence**: the ORIGINAL discrepancy that motivated this note —
`scratch/alns_trace_window2_moves.py` failing to reproduce
`scratch/alns_delta_calibration.py`'s original 10 improving moves
(Section 7k) — remains genuinely UNEXPLAINED. It is not attributable to
GNN-inference non-determinism, since that specific mechanism has now been
ruled out by direct test. The true cause is an open item, not resolved by
this document and not chased down further (a separate investigation,
beyond what any step in this thread was scoped for) — recorded honestly
as unresolved rather than left implicitly attributed to a hypothesis that
turned out to be wrong. See `ALNS_REFINEMENT_PROPOSAL.md` Section 11 and
`ALNS_REPAIR_SCORING_PROPOSAL.md` Sections 7-8 for the full test and
correction.

---

## 11. Methodology note — a plausible explanation is a hypothesis, not
    a finding, until it is tested

Standalone, same treatment as Sections 8 and 9 — Section 10's own history
is the concrete instance this generalizes from, not the point of the
note itself. A real, surprising result (window 2's move-trace failing to
reproduce) got a plausible, mechanistically reasonable explanation (GNN
inference noise flipping a hard cutoff), correctly labeled as an
unconfirmed hypothesis at the time it was written, and correctly NOT
chased down further given the "cheap, bounded" scope of that step. It
was then generalized into a standing methodology note — and stood,
unquestioned, until a direct test became available as a SIDE EFFECT of
unrelated work (Step 1's stability check, run for a different reason
entirely) and failed outright.

**The correction happened because a test became available and was taken,
not because the explanation was re-examined on paper.** That is the real
lesson, not "the hypothesis was wrong" — reasonable hypotheses are
sometimes wrong, and this project's discipline has never claimed
otherwise. The risk this note is actually about: a hypothesis that is
clearly labeled as unconfirmed can still drift toward being TREATED as
settled simply because it was written down clearly, generalized into a
standing practice note, and never revisited — not because anyone stopped
believing it was a hypothesis, but because nothing forced the question
back open. This project's own "measure before concluding" discipline
applies as much to EXPLANATIONS for surprising results as it does to the
results themselves — a labeled hypothesis is not exempt from that
standard just because it already has a citation.

**Going forward**: when a cheap, direct test of a standing hypothesis
becomes available — even incidentally, even as a side effect of work
scoped for something else entirely — take it, and correct the record
explicitly if it fails, the way Section 10 does here, rather than by
quiet omission.

---

## 12. Methodology note — apparent sample size from a deterministic,
    frac-invariant destroy operator can silently collapse to a handful
    of unique cases (2026-08-27)

Standalone, same treatment as Sections 8, 9, and 11 — the concrete
instance (`ALNS_REPAIR_SCORING_PROPOSAL.md` Sections 11-12) is not the
point; the failure mode generalizes to any test harness built on
`_destroy_high_delay_risk` or another deterministic-ranking destroy
operator, not just to this one repair-scoring formula.

**What happened.** A Step 1 mechanism check reported "450 real
comparable decisions" (225 per window, 15 repeats x 3 removal fractions
x 5 sampled operations per batch) for both the original and a corrected
repair-scoring formula. Investigating an unrelated question (why the
corrected formula's differentiation rate split sharply between two
windows) required extracting the actual operation identities behind
those 450 lines — at which point it became visible that each window's
"225 decisions" were only **5 unique operations, each replayed 45 times,
byte-for-byte identical every time.** The true sample size was 10, not
450.

**Root cause, generalizable.** `_destroy_high_delay_risk` ranks
operations purely by GNN-predicted `delay_prob`, with no rng involved,
and returns `ranked[:count]` where `count` scales with `removal_frac` —
so its **top-K is invariant to `removal_frac`** (a smaller frac's top-K
is always a prefix of a larger frac's). A harness that (a) reuses the
same seeded starting chromosome across every sampling iteration without
mutating it, and (b) always takes the first K elements of this operator's
output, will draw the IDENTICAL K operations on every single repeat,
regardless of how many repeats, removal fractions, or "samples per
fraction" the loop parameters claim. The loop's iteration count measures
how many times the same computation ran, not how many distinct real
cases were tested — and nothing about the code raises an error or a
warning when this happens, because every individual comparison is
computed correctly; only the aggregate sample size is misrepresented.

**Why it stayed hidden.** Exactly the same reason Section 8's confound
did: every individual run was internally consistent, reproducible, and
passed its own sanity checks (the stability check across 3 process
invocations even reported this as a POSITIVE property — "bit-identical
every time" — without recognizing that bit-identical repeats of the same
5 operations is not evidence of stability across real cases, it is
evidence there was only ever one case per operation). The summary
statistic ("0/450", "135/450") was real arithmetic on real numbers; the
denominator was simply not what its own label implied.

**The fix, applied and verified**
(`scratch/alns_repair_scoring_step1_diverse_sampling.py`): two changes,
independently sufficient, both applied for maximum real diversity —
(1) sample from THREE genuinely different starting chromosomes/windows
(different 300-order slices of the real order book), not one or two, and
(2) within each real destroy batch `_destroy_high_delay_risk` returns,
draw a random K-subset via a seeded `rng.sample`, rather than always
taking a fixed prefix — every sampled operation is still a real member
of that batch's genuine high-delay-risk set (not synthetic noise), but
WHICH members get compared now actually varies across repeats. Result:
350 truly unique operations across 1,440 raw comparisons (verified by
extracting and counting distinct `op_key`s, not assumed) — and the
stability check (3 independent process invocations) still came back
bit-identical, now meaningfully, because it is 350 real distinct cases
reproducing identically, not 10.

**Going forward, for this project and any future one**: a test harness
sampling from a deterministic-ranking operator must report the count of
UNIQUE cases behind any aggregate statistic, not just the raw iteration
count — the two can differ by 45x with the code behaving exactly as
written and every number technically correct. `scratch/
alns_repair_scoring_step1_diverse_sampling.py` now does this by
construction (prints `UNIQUE op_keys` alongside `raw_comparisons` in
every summary line) as a template for future Step-1-style mechanism
checks in this project.

---

## 13. GA-refinement investigation — FULL AND FINAL closure: six
    mechanisms evaluated, zero adopted, one coherent finding (2026-08-27)

This supersedes Section 7k as the closing statement for the entire
thread Section 7a opened, not a fourth addendum to it. Section 7k closed
three interventions (memetic, TCOX, ALNS); two more were evaluated after
it (ALNS repair-scoring, this session; CP-SAT, evaluated in parallel as
a different-paradigm alternative) plus the pre-existing island model,
whose own contribution was measured as part of Section 2's original
finding. Six mechanisms total, spanning three genuinely different levels
of the pipeline. Consolidated here as one investigation with one
verdict, per instruction, rather than left as six separate documents a
reader has to assemble themselves.

**The shared origin, stated once**: Section 2/7a's headline finding —
evolution alone improves the population 0.035% over 50 generations;
local-search polish immediately after improves it 0.615% in ~150
evaluations, roughly **17.5x more**. Every mechanism below is an attempt
to either extract more from the search/evolutionary machinery, or to
make the refinement step that already captures most of the real gain
even better. None succeeded, in a specific, diagnosed way each time —
not six unrelated failures, but the same underlying picture confirmed
from six different angles.

| mechanism | level targeted | verdict | key number | where |
|---|---|---|---|---|
| Island model | evolutionary population structure (pre-existing, `ga/scheduler.py::optimize_islands`) | measured, not a proposal — contribution is marginal either way | single population +0.4%, islands +0.9% over initial population (both dwarfed by local search's +0.615% in a single pass) | Section 2, `ga/local_search.py` docstring |
| Memetic integration | inject locally-polished individuals into the evolutionary loop | **HOLD** | 1 real win (+1.073%), 1 wash (-0.115%), 0 real losses across 3 frozen-snapshot cases | Section 7f |
| TCOX (domain-aware crossover) | fix the confirmed OS/LA/MC decoupling in crossover | **HOLD** | 1 real win (+0.371%), 1 real regression (-0.324%), 1 wash, across the same 3 cases | Section 7j, `DOMAIN_AWARE_CROSSOVER_PROPOSAL.md` |
| Sequential ALNS | replace one-shot polish with adaptive destroy/repair search | **HOLD** (strongest of the three above — ruled out its own confound) | recalibrated search explored hundreds of real accepted moves/run, still converged on the IDENTICAL final answer as the broken version | `ALNS_REFINEMENT_PROPOSAL.md` Sections 9-10 |
| ALNS repair-scoring | fix the deterministic least-loaded-line rule EVERY one of the above inherited unexamined | **HOLD** — genuinely differentiates, but regresses where it matters | corrected formula picks a different line in 28.7% of real cases (was 0% for the original, provably-dead formula) — but -0.94% to -1.36% vs. plain ALNS in the one window it changes anything, 0% elsewhere | `ALNS_REPAIR_SCORING_PROPOSAL.md`, this session |
| CP-SAT | replace the GA/metaheuristic paradigm entirely with exact constraint solving | **CLOSED** (narrower claim — see its own doc) | could not find ANY feasible solution for a 2,541-op window within a runtime-matched budget, with or without a warm start from the GA's own real solution | `CP_SAT_PROPOSAL.md` Sections 8-10 |

**The pattern across the first five, read together rather than
separately**: island model, memetic, and TCOX all target the
EVOLUTIONARY loop itself (population structure, injection, crossover
content) — and Section 2/7a's own number already explains why none of
them could matter much: the loop they're all trying to improve
contributes 0.035% on its own, an order of magnitude below what a single
local-search pass captures. Sequential ALNS and repair-scoring instead
target the REFINEMENT step directly, where the real gain actually lives
— and both still held, for two DIFFERENT, specific, diagnosed reasons:
ALNS's destroy/repair SEARCH around the existing repair rule adds real,
measured exploration (hundreds of accepted moves) that still can't
escape the same final answer; repair-scoring's fix to the rule ITSELF
proves it CAN be made to behave differently, and when it does, that
difference is a real cost, not a hidden gain. Both results point at the
same underlying fact from different directions: `ga/local_search.py`'s
plain least-loaded-line repair, feeding off a heuristic-seeded
construction, is already close to whatever is achievable by rearranging
WHERE work runs once WHAT gets scheduled and WHEN is already good —
exactly the framing `optimize_islands`'s own docstring stated at the
start of this thread (Section 2), now confirmed from five independent,
substantial pieces of work rather than asserted once.

**CP-SAT sits apart, deliberately** — not a sixth attempt to out-tune
the GA's own machinery, but a genuinely different paradigm tested
specifically because the first two GA-operator attempts (memetic, TCOX)
showed the same landscape-dependent, unpredictable pattern
(`CP_SAT_PROPOSAL.md` Section 1's own stated reasoning for trying it
now rather than iterating further on GA operators). Its closure is
narrower and differently-shaped than the other five: it does not say
"exact solving can't beat this GA," it says the SPECIFIC tractability
simplification tested (candidate lines capped to the top 3 by raw pool
size, needed to make the model solvable at all) could not even reach
feasibility — a real, decisive result about that simplification, with
the door explicitly left open on a differently-scoped attempt
(`CP_SAT_PROPOSAL.md` Section 10).

**Final verdict, stated once for the whole thread**: six mechanisms,
three levels of the pipeline (evolutionary population structure,
refinement-stage search, refinement-stage repair rule content, and a
wholly different solving paradigm), each evaluated with a real,
pre-committed decision bar and real data — zero adopted, and this
investigation's own production behaviour never changed once across any
of it (every implementation stays opt-in: `memetic_interval`,
`triple_coupled_crossover`, `alns_refine`, `repair_score_weights`, all
default to unchanged behaviour; CP-SAT never left its two prototype
scripts). This is being closed here as a complete investigation, not a
paused one — six well-diagnosed negative/mixed results, each ruling out
a specific, concrete mechanism rather than leaving a vague "didn't work"
verdict, constitute a real answer to Section 2's original question
("can anything beyond heuristic construction + local search meaningfully
improve this GA's output"): not with any of the six approaches tried,
for reasons now understood in each case, not left as an open question
defaulting to "try more of the same." A seventh attempt in the same
family is not ruled out on principle, but is a new, separately-motivated
piece of work if it happens, not a continuation of this one.

---

## 15. Methodology finding — a missing ORDER BY in a multi-table JOIN was
    silently feeding undefined row order into GA chromosome construction
    (2026-08-27)

Standalone, same treatment as Sections 8, 9, 11, and 12 — a real,
general-purpose finding surfaced as a side effect of building the
authoritative GA-vs-baselines comparison (`run_authoritative_comparison.py`),
not a footnote to that report. Reported here on its own because the failure
mode generalizes to any query in this codebase that feeds a chromosome-
position-sensitive or precedence-sensitive pipeline, not just this one
script.

**What happened.** Two full runs of `run_authoritative_comparison.py` —
identical seed (42), identical frozen database snapshot, identical origin
timestamp, identical GNN checkpoint — produced genuinely different GA
schedules. Window 2's GA margin over the best dispatching heuristic was
+8.18% in the first run and +1.29% in the second. For a pipeline that
explicitly reseeds both `random` and `numpy.random` at the start of every
GA search, this should have been impossible.

**Bisection, the same discipline this project has applied to every prior
reproducibility question** (Sections 7e-7f's live-database-drift confound,
Section 10-11's retracted GNN-inference-noise hypothesis): each candidate
stage was tested twice, as separate process invocations, and diffed
directly rather than assumed —

1. Data loading + GNN inference + all 5 baseline heuristics
   (`GAScheduler.run_baselines()`, no GA search at all): bit-identical to
   6 decimal places.
2. `optimize()` alone, toy scale (pop=10, 1 generation): bit-identical,
   including the exact final chromosome.
3. `optimize()` alone, real production scale (pop=50, 50 generations,
   single window): still bit-identical.
4. `optimize()` immediately followed by `polish()` (the actual sequence
   the real pipeline runs): still bit-identical — same start/final
   fitness, same accepted-move counts, same chromosome, to the decimal.

Every one of these agreed with itself AND with the SECOND full run's
numbers — never the first. That pointed at something specific to the
first run's circumstance, not something randomly flaky in the GA itself:
the first run built its snapshot fresh
(`database/ga_snapshot.py::build_snapshot()`, a full DROP+CREATE+re-clone
of all 26 tables), while every later test — including the second full
run, which reused that already-built snapshot — read from an
already-materialized, unchanging table.

**Direct confirmation, not left as a plausible story.** Rebuilt the
snapshot fresh a THIRD time and immediately re-tested window 1: got a
third, still-different result (on-time rate matched run 1's 10.0%, but
margin, best-baseline, and end-day matched neither prior run). This is
not "run 1 was the odd one out" — it is "every fresh snapshot build
produces a new, different outcome," while repeated reads against one
settled snapshot stay internally consistent with each other but not with
a fresh build. That is the exact signature of an undefined row order,
not of nondeterministic computation.

**Root cause.** `database/queries.py::fetch_active_orders_and_routings`'s
three-table JOIN (`Production_Orders` × `Purchase_Orders` ×
`Routing_Operations`, plus a `LEFT JOIN` aggregation subquery) had **no
`ORDER BY`**. Row order from a multi-table JOIN without one is genuinely
undefined by SQL semantics — not merely unspecified-but-empirically-
stable, which is what every earlier test in this bisection had
(incorrectly) assumed. A freshly-populated table's very first read
apparently gets a different execution-plan/physical-layout-dependent row
order each time the table is rebuilt from scratch; once "settled,"
repeated reads against the same unchanging table return a consistent
(but still arbitrary) order. Since `df_routings`'s row order directly
determines the GA chromosome's initial operation sequence and
precedence-repair behavior, an undefined JOIN order was an undefined GA
input, silently, every single time this query's underlying tables got
rebuilt.

**The fix, at the source, not a workaround.** Added
`ORDER BY po.production_order_id, ro.operation_sequence_number` to the
query. This is not an arbitrary tie-break chosen to make the symptom go
away — it is the ordering every downstream consumer in this codebase
*already assumed* was true: `ga/fitness.py`'s own precedence-violation
check and `ga/operators.py::repair_precedence` are both keyed on
`operation_sequence_number` matching operation arrival order, a
correspondence this same codebase's history already verified holds for
every order in this database (`ga/fitness.py`'s own comment: "verified to
match `operation_sequence_number` for all 1,410 orders"). The fix makes
explicit and guaranteed what the rest of the pipeline was implicitly
relying on regardless.

**Verified, not assumed.** Two independent fresh snapshot builds after
the fix produced byte-identical, correctly grouped-and-sequenced row
order — every one of a table's 8,745 rows checked programmatically:
each order's rows contiguous, `operation_sequence_number` non-decreasing
within each order, zero violations, both times. Full test suite 506/506
passing (the fix is purely additive to the query, no application-code
change). `run_authoritative_comparison.py` was then re-run a third time,
fresh, against the fixed query — see `outputs/
authoritative_comparison_report.{txt,json}` and its own memory entry for
the now-genuinely-reproducible result this produced, which supersedes
both of the runs that surfaced this finding.

**The general lesson, worth stating plainly**: any multi-table query in
this codebase (or any codebase) without an explicit `ORDER BY` that feeds
a chromosome-position-sensitive, precedence-sensitive, or otherwise
order-dependent downstream pipeline is a live reproducibility risk, not
a style nit — "it has been stable in every test so far" is not evidence
that it will stay stable across a fresh table rebuild, a schema change,
an index change, or a different database server. This is now recorded as
a standing practice (`PROJECT_STATUS.txt` STANDING PRACTICES), alongside
the frozen-snapshot rule Section 8 established: use `ORDER BY` on any
query whose row order the pipeline depends on, explicitly, rather than
relying on incidental storage or query-plan behavior to hold.

---

## 6. Two follow-up closures (2026-08-25, later same day)

### 6a. NO_ACTION's intended semantics — confirmed from the code's own
    design comments, not assumed

**"No database write" IS the deliberate, documented design.**
`agent/event_handler.py::handle_machine_breakdown`'s own docstring states
the three-way policy explicitly: *"No affected orders / negligible impact
-> log only, no-op."* This is not an inference — it is the stated design
intent, confirmed by reading the comment before answering.

**But the premise that "a separate repair process handles it, entirely
outside this decision path" does NOT hold — searched and found no such
process anywhere.** `agent/monitor.py::FactoryMonitor.listen()` only
detects `Active → Breakdown` transitions (`if new_status == 'Breakdown'
and old_status != 'Breakdown'`); the reverse transition
(`Breakdown → Active`, i.e. a repair completing) is silently absorbed
into `self.machine_states[machine_id] = new_status` with no branch, no
event, no re-evaluation of anything. No file under `agent/`, `database/`,
or elsewhere in this codebase ever sets `current_status` back to `Active`
for a machine `handle_machine_breakdown` broke. **So: the "don't write
immediately" half of the design is confirmed intentional; the "something
else resolves it later" half that would make that safe is not implemented
anywhere in this codebase** — a real, more significant gap than
originally scoped, not the reassuring "elsewhere, by design" answer the
question's first framing left open as a possibility.

### 6b. committed_by_asset staleness — fixed in `agent/impact_assessor.py`,
    verified against real data, not just code inspection

**Fix**: two new functions, `_book_tentative_slot` /
`_release_tentative_slot`, called immediately after each affected
operation's candidate slot is determined (idle search, standby-reserve
draw, and donor preemption all now go through it) — so a later operation
in the SAME breakdown assessment sees earlier operations' claims instead
of independently re-discovering the same "idle" gap. The standby-reserve
draw itself had the identical latent bug (candidates were chosen by a
usage COUNTER only, never checked for a real time conflict) and got the
same fix. 5 new unit tests (`tests/test_impact_assessor_booking.py`,
pure in-memory, no DB) plus the full 444+5-test suite passing.

**Verified against real data in the disposable clone, not just by
inspection**, exactly as instructed: re-ran machine 2576 (the same real
machine that showed the identical-`candidate_asset_id`-five-times symptom
in Section 5c) under severe constructed scarcity — 401 of 404 real
Bar-Tack Pocketing machines broken, sparing 3 (including the target).

**Confirmed working, with real numbers**: the 2 surviving alternates
(assets 2469 and 2524) were recommended for 27 and 3 affected operations
respectively — but this time with **27 and 3 genuinely DISTINCT start
times each**, correctly queued one after another (asset 2469's queue
spans 2026-08-25 through 2027-07-05) instead of the pre-fix pattern of
identical, colliding recommendations. **Zero collisions found** across
either asset's queue.

**A real, informative negative result on the specific "2+ LOCAL_PATCH
writes" ask**: despite queuing 27 operations onto one machine spanning
almost a year, every one still scored `impact_hours=0.0` (NO_ACTION) —
because each operation's own ORIGINAL committed schedule was already
spread across a comparably wide date range, so serializing them onto one
substitute machine in roughly the same relative order adds close to zero
measured delay relative to each operation's own original slot. The
scarcity that finally broke through landed **6 real operations in
FULL_RESCHEDULE** (`candidate=None`, "No alternate eligible machine ...
available" — the first time this investigation has seen this tier fire
for real, at 401/404 machines broken) rather than the moderate-impact
LOCAL_PATCH band the double-write test needed. Confirmed why: real
impact in this dataset appears to jump almost directly from
negligible (NO_ACTION) to impossible (FULL_RESCHEDULE) with very little
occupying the moderate middle band that LOCAL_PATCH requires, independent
of the committed_by_asset bug — a structural property of how sparse this
database's real demand is relative to its real capacity, not a
methodology failure. **The fix is confirmed correct against real,
executed data at the assessment layer** (no more identical/colliding
recommendations); **the specific "two real LOCAL_PATCH writes colliding"
scenario could not be constructed even under three separate, genuinely
severe, real attempts** (this session's original 682-op sample, the TSJ
98.6%-scarcity test, and this Bar-Tack-Pocketing 401/404-scarcity test) —
reported as a real, informative negative, not left as an assumption that
the fix works.
