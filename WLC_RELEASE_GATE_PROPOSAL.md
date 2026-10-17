# Workload Control Release Gate — Scoping Proposal (no code changes)

Companion to `GA_AUDIT.md` Sections 5-8. This is a design proposal only,
per instruction — nothing here has been implemented. Same discipline as
`DELAY_LABEL_FIX_PROPOSAL.md`: describe the mechanism precisely, ground it
in real code and real numbers already measured this session, flag open
questions honestly, and stop short of writing it.

---

## 1. Why this, why now

`GA_AUDIT.md` Section 8's release-date-enforcement fix was necessary and
correct (achieved makespan is now genuinely comparable to a real capacity
floor, 327.6d vs 327.3d), but it only fixed WHEN an order is *permitted* to
start (its real `release_date`, no longer clamped to day zero for
everything). It did not add any control over WHEN an order *should* start
given how loaded the factory already is. The result, also measured this
session: **577 of 1,029 orders (56.1%) are simultaneously in flight on day
0** — a genuine backlog (real orders whose real release dates have already
passed), immediately competing for the same finite machine pools, because
nothing currently holds any of them back.

One more number from this session is the key argument for WHY a
workload-norm approach (not a capacity fix) is the right tool: Section 5's
same-day capacity-relaxation floor, per type, is **1.8–2.8 days** —
i.e. if the entire current order book's remaining work at a single machine
type were released all at once, that type's usable machines could clear
ALL of it in under three days. The factory is not short of capacity
(labour utilisation under the corrected simulation is 0.5% — Section 8).
**The problem this proposal addresses is entirely a timing/pacing problem,
not a capacity problem.** That is exactly the situation Workload Control
was developed for: high-mix make-to-order job shops with adequate
aggregate capacity but uncontrolled, demand-driven release causing shop
floor congestion and queue-time blowup.

---

## 2. The three WLC layers, mapped onto what already exists here

| WLC layer | Status | This codebase |
|---|---|---|
| Order entry / due-date setting | Have an equivalent | Completion-time GNN head used for customer quoting (`models/gnn.py`, `agent/decision_engine.py`) |
| **Job release** | **Missing — this proposal** | Orders currently become schedulable the instant `release_date` passes (`order_release_minutes`, Section 8) — a date gate, not a load gate |
| Priority dispatching | Already have this | `FIFO` / `EDF` / `CriticalRatio` (`ga/heuristics.py`) — real WLC-literature dispatching rules, already implemented and already the GA's own baseline comparisons (Section 8's table) |

This proposal is scoped narrowly to the missing middle layer.

---

## 3. Proposed mechanism: pre-shop pool + workload-norm release gate

**Pre-shop pool**: every order whose `release_date` has passed (per
Section 8's now-correct ready-time floor) is *eligible* but sits in a
pool rather than being automatically admitted into the next window's
lookahead. **Admission** into active scheduling (i.e. becoming part of a
window's lookahead, and thereby able to compete for machines) additionally
requires that admitting it would not push any (line, type) pool's
projected outstanding workload past that pool's norm.

**Projected workload, direct-load version (proposed starting point)**: for
each of the 5 canonical operation types (`utils/machine_taxonomy.py`,
`utils/machine_reserve.py::usable_cap_by_type` — bar-tack pocketing, body
assembly, flatlock hemming, seam edge overlock, tubular sleeve join),
track the sum of `serial_processing_mins` (the same total-work figure
`ga/scheduler.py` already computes per operation, `sam * quantity /
avg_efficiency`) across every operation of that type belonging to an
ADMITTED-but-not-yet-COMPLETED order, converted to machine-days. Before
admitting a candidate order, compute what each of its touched pools'
outstanding load would become if it were added; admit only if every
touched pool stays under its norm. This is mechanically the same
quantity `scratch/release_date_enforcement_check.py`'s peak-backlog
diagnostic already computed once, offline, as a demand-vs-capacity check —
this proposal turns it into a live, continuously-applied admission gate.

**Indirect load (the fuller WLC formulation, NOT proposed for a first
version)**: literature (Bechte; Land & Gaalman) also discounts load from
operations upstream of a pool that haven't reached it yet, weighted by
how many routing steps away they are. This is more theoretically complete
but adds real complexity and a new source of estimation error. Given this
project's history of preferring the simplest version that can be measured
first (`MC_PROTECTION_FRACTION` ships inactive by default; the risk
buffer's broad-vs-gated ablation existed specifically because the simple
version was tried and measured before adding sophistication), **direct
load only is the recommended starting scope**, with indirect load
flagged here as a documented, deferred refinement — not assumed necessary
until direct-load-only is measured and found insufficient.

---

## 4. Workload norm sizing

**Starting point, not a final number**: express each pool's norm as a
small number of days of that pool's own daily capacity — e.g. an initial
guess in the 1–3 day range per pool, directly motivated by Section 5's
same-day floor (1.8–2.8d): since the WHOLE current book's remaining work
at any one type is only 1.8–2.8 machine-days deep, a norm of that rough
order of magnitude is enough to meaningfully stagger the day-0 backlog
without starving the factory (a norm many times larger than the total
book's own depth would rarely bind at all — the same "inactive by
default" failure mode `MC_PROTECTION_FRACTION=1.0` already demonstrates
for a different knob). This needs empirical tuning via ablation (Section
6 below), not adoption as a hardcoded constant.

**Should bar-tack pocketing (the identified bottleneck) get different
treatment?** Two existing pieces of infrastructure are directly relevant
and already reused across this session's findings:

- `ga/scheduler.py::_compute_protected_machine_type` /
  `most_congested_machine_type` already measures, per window, from real
  carried-forward `machine_free` state, which single type is currently
  most booked-ahead — the exact live signal a DBR-style "drum" needs.
  Currently this measurement only softens MACHINE SELECTION within a
  window (0H.8); this proposal's natural extension is to also let it pace
  ORDER ADMISSION — i.e. the bottleneck pool's own load could be the
  master release-pacing signal for the whole book (admit an order only if
  it doesn't push bar-tack pocketing past its norm), with the other four
  pools carrying looser, secondary norms given they showed no independent
  binding pressure in Section 5's per-type floor breakdown.
- **Worth stating precisely, not conflating the two frameworks**: DBR's
  buffer exists to protect the bottleneck from STARVING (make sure work
  arrives just before it's needed); WLC's norm exists to control QUEUE
  LENGTH ahead of a resource (make sure too much doesn't pile up). This
  factory's measured problem is the latter (577 in flight, not the
  bottleneck sitting idle for lack of arriving work) — so WLC's
  queue-limiting framing is the primary, directly-motivated mechanism
  here; treating bar-tack pocketing as the drum is a genuine, literature-
  grounded refinement on top of it, not a separate fix for a separate
  problem.

---

## 5. Interaction with the rolling-horizon window structure

**Sits in front of, does not replace, the existing window/commit/lookahead
machinery.** Recommendation, with reasoning:

`ga/rolling_horizon.py::run_rolling_horizon` currently slices
`all_orders` (sorted by `release_date`) purely by position:
`all_orders[pos:pos+lookahead_size]`, unconditionally. The proposed
change is narrow: replace "next `lookahead_size` orders by release-date
order" with "walk forward through the release-date-sorted list, admitting
orders one at a time while every touched pool's projected load stays
under norm, stopping (leaving the remainder in the pool for a later
window) once a pool would be pushed over." Everything downstream —
`GAScheduler`, `evaluate_schedule`, local search, the four baseline
heuristics, the just-added release-date ready-time floor — is untouched.

**Why not replace the window structure**: this project has already tried
changing window GEOMETRY directly twice (widening lookahead beyond
commit; shrinking commit relative to a fixed lookahead — PROJECT_STATUS.txt
Section 2.2 / 0B) and measured BOTH worse on every metric. The common
diagnosis there was that the fitness function had no notion of
committed-vs-provisional orders. A workload gate is a different lever
entirely — it changes WHICH orders are even offered to a window, not how
wide the window's view is — so it isn't already-known-bad by that
precedent, but it also hasn't been tried, and should be measured with the
same skepticism those two were.

**Open question this raises, not resolved here**: if the gate leaves
fewer than `commit_size` orders eligible for a given round (e.g. a tight
bar-tack-pocketing norm only clears 50 orders, not 300), does that window
commit a smaller-than-usual batch (variable-size windows — the more
literature-faithful WLC pattern, continuous/frequent small releases
rather than fixed large batches), or does the gate loosen to guarantee
some minimum admission floor so the factory is never left idle waiting on
a pool that's near its norm but not genuinely out of capacity? Both are
defensible; this needs a decision informed by measurement, not assumed
before implementation.

**Relationship to `GA_AUDIT.md` Section 7 (demand-aware window sizing,
already scoped, not built)**: this proposal supersedes that scoped idea
rather than sitting alongside it. Section 7 proposed manually shrinking
`commit_size` during the dense early season as a static, hand-tuned
geometry change. A workload-norm gate is the literature-grounded, demand-
*reactive* generalization of exactly that intuition — it produces smaller
effective admission batches during genuinely loaded periods and larger
ones during slack periods automatically, from the real-time pool state,
rather than from a pre-computed density curve. Recommend closing Section
7 as superseded if this proposal is adopted, rather than pursuing both.

---

## 6. Validation methodology, before any of this is trusted

Same standard as every other structural change this session (gated risk
buffer, split-encoder promotion, release-date enforcement itself): measure
before implementing further, ablate before adopting, report negative
results honestly. Concretely, if this is approved to move to
implementation:

1. Build the direct-load-only gate as scoped above (Section 3), disabled
   by default (mirrors `RISK_BUFFER_AT_CERTAINTY`'s and
   `MC_PROTECTION_FRACTION`'s existing "ships inactive, enabled
   deliberately" convention in `ga/schedule_utils.py`).
2. A multi-arm ablation, same methodology as
   `scratch/gated_buffer_ablation.py`: (A) disabled — current
   release-date-only behaviour from Section 8; (B) a norm sized per
   Section 4's starting guess; (C) a tighter norm; possibly (D) a looser
   one — one process, one origin, one seed, on-time/makespan/throughput/
   labour/margin compared across arms exactly as that ablation reported.
3. Specifically check the risk flagged in Section 7 below (deadline
   interaction) before calling any result an improvement.

---

## 7. Risks and open questions, flagged before implementation

1. **Deadline interaction is the primary risk, not a footnote.** Section
   8 already showed that pushing an order's real start LATER (release-date
   enforcement) measurably reduced on-time rate in windows 3-4 by removing
   unearned slack. A workload gate pushes starts later still (holding
   orders in the pool past their raw release date). This could tighten
   deadlines further and worsen on-time rate — including, plausibly, in
   window 1 (already the worst at 11.3%) — unless norms are sized with
   this tradeoff explicitly in view. Must be measured, not assumed to
   help, exactly like the two already-rejected window-geometry changes in
   PROJECT_STATUS.txt Section 2.2/0B that looked like they should help and
   didn't.
2. **Untested interaction with the now-adopted confidence-gated risk
   buffer** (`GA_AUDIT.md` Section 6): the buffer pads operation DURATION;
   this gate would pad an order's START. Both are real, both compound,
   neither has been measured against the other.
3. **Norm sizing is a genuine empirical unknown**, not a value this
   document should be read as recommending with confidence — too tight
   starves the factory (worsens the already-measured 0.5% labour
   utilisation further for no benefit), too loose never binds (the same
   "inactive by default" outcome `MC_PROTECTION_FRACTION=1.0` already
   demonstrates elsewhere in this codebase for a structurally similar
   knob).
4. **Admission tie-breaking is its own open design decision**: when more
   orders are pool-eligible than the norm allows through in one pass, what
   selects which ones go first — release-date order (FIFO), tightest
   deadline (EDF/CriticalRatio, reusing the exact dispatching rules
   already implemented), or GNN-predicted priority? Not decided here.
5. **New accounting needed, not free**: "outstanding admitted-but-
   incomplete workload per pool" is a POOL-level aggregate, distinct from
   the existing PER-MACHINE `machine_free`/`machine_busy` state already
   carried window-to-window. Some new state, threaded through
   `run_rolling_horizon` the same way `releases`/`completions` were just
   added (Section 8), would be required — flagged as real, scoped work,
   not assumed to fall out of existing structures for free even though
   the underlying per-(line,type) pool identities already exist.
6. **This will not shrink the 327.6-day makespan** — total real work and
   real release dates are fixed; a release gate redistributes WHEN
   machines are asked to do that work, it does not create capacity. Its
   plausible benefit is a better-behaved on-time/tardiness DISTRIBUTION
   (specifically smoothing window 1's demand crunch, per the seasonal-
   concentration finding), not a smaller aggregate makespan. Framing it
   as a makespan fix to stakeholders would be a false promise this
   document is explicitly not making.

---

## 8. Stress-tested against real edge cases (2026-08-24)

Per instruction: checked against actual code, not more literature, before
any of Section 3-7's design is trusted. Two of these five checks surfaced
real, previously-undocumented facts about the EXISTING system (not just
about this proposal) — reported plainly.

### 8.1 An order genuinely infeasible under any release timing

**Today's system has no live feasibility check at all** — checked, not
assumed: deadlines are exclusively a SOFT fitness penalty
(`ga/fitness.py`'s lateness term, `utils/config.py`'s comment on why even
the idle-labour term is soft), and the only "0 orders structurally
infeasible" check this project has ever done was a one-off OFFLINE
verification (PROJECT_STATUS.txt Section 4, reversal 1), not something
that runs continuously. An infeasible order today is silently absorbed
into a late-but-uncomplaining schedule.

A workload gate makes this gap actively dangerous rather than just
incomplete, because it introduces a NEW failure mode date-only release
cannot have: **starvation deadlock**. If a single order's own workload at
some pool already exceeds that pool's norm — even alone, with nothing
else competing — a naive "admit only if it fits under norm" rule would
never admit it, ever. Two required design elements, not optional
refinements:

1. **A pre-admission feasibility check**, run once per order at its first
   pool-entry, reusing exactly the critical-path calculation already built
   this session (`scratch/capacity_floor_current.py`'s Floor C: release
   date + this order's own minimum routing-chain duration, compared
   against its real deadline). If already infeasible before the gate is
   even applied, **report it** — surface as an explicit flagged event, the
   same way `agent/impact_assessor.py` already escalates rather than
   silently degrades — never let it sit in the pool accumulating a fitness
   penalty nobody sees. This closes a real gap in the CURRENT system, not
   only a risk of the proposed one.
2. **A norm-exceeds-norm escape valve**: an order whose own single-order
   load already exceeds a pool's norm must be admitted anyway (flagged as
   "admitted over norm"), and more generally an **aging/starvation
   override** — an order that has waited in the pool past some threshold
   (e.g. its remaining deadline slack drops below its own critical path)
   force-admits regardless of norm. This is standard real-world WLC
   practice, not an invention for this codebase: production WLC systems
   always carry an urgency override for exactly this reason.

### 8.2 A demand shock larger than the 577-order burst

The pool as scoped has no size cap — it is just a filtered order list, so
an arbitrarily large shock doesn't break the mechanism structurally. Two
real effects worth flagging, not smoothed over:

- **Feasibility risk scales with shock size.** A larger backlog means a
  longer expected queue-time in the pool for tail orders, which makes 8.1's
  feasibility check something that needs re-checking as orders wait (not
  just once at entry) — an order that was feasible when it entered the
  pool can become infeasible while queued behind a large-enough backlog.
- **The gate's smoothing benefit is not shock-size-invariant, and may
  shrink exactly when most needed.** For a large enough backlog, some
  norm-satisfying subset of eligible orders almost always exists to fill
  a window — meaning the gate might barely change WHICH orders get
  admitted for a shock so large it swamps every pool regardless of
  selection, even though it still constrains WHEN the full backlog clears.
  The mechanism's real value is strongest for moderate, realistic bursts
  like the measured 577 — this should be measured across at least one
  larger synthetic shock in the ablation (Section 6), not assumed to scale
  linearly.

### 8.3 Compatibility with the manager-priority override

**Checked directly, and this is a real, concrete gap the design must
close, not a footnote.** `agent/decision_engine.py`'s existing
`manager_forced_orders` / `set_manager_priority()` mechanism (already
real, already used) currently affects ONLY in-window sequencing — a
manager-flagged order gets a fuzzy urgency boost to its reward term
(`ga/scheduler.py`'s `fuzzy_multiplier`) once it is already inside a
window. It has no effect on admission at all. Under the proposed pool,
a rush order stuck behind the workload gate would have **no fast path**
with the design as scoped in Sections 3-5 — a real regression versus
today's behaviour if shipped as-is.

**Required amendment to Section 3, not optional**: `manager_forced_orders`
must be checked BEFORE the workload-norm filter — a manager-forced order
is admitted into the current window unconditionally, bypassing the norm
check entirely, exactly mirroring how it already bypasses fuzzy triage
today. This is standard WLC practice (real release systems always carry a
manual expedite path) and should be treated as part of the gate's initial
scope, not a v2 refinement. One consequence worth naming: a bypassed
order's workload still gets added to the pool's outstanding-load
tracking once admitted (it consumes real capacity) — several simultaneous
manager overrides can still push a pool over norm, which is correct
behaviour for an explicit human override, not a bug to guard against.

### 8.4 An extended multi-machine-type outage

**Checked directly and found a real, pre-existing architectural fact that
this proposal inherits rather than causes**: `utils/machine_reserve.py`'s
`usable_cap_by_type` — the exact figure Section 3's workload norm would be
denominated against — is **deliberately static**, computed from each
type's TOTAL asset count, and explicitly does NOT read live
`current_status` (the module's own docstring: "Deliberately NOT reserving
by current_status... the reserve would then differ between the GA run and
a later breakdown assessment"). `ga/chromosome.py`'s machine pools
likewise never filter on `current_status`. Live breakdowns are handled
entirely reactively, per-asset, by `agent/impact_assessor.py` (find an
alternate machine, draw from the 7% reserve, or escalate) — the 7%
company-wide reserve is explicitly sized to absorb ordinary, moderate
breakdown load, not an extended multi-type outage.

**Honest answer: no, the workload norm as scoped in Section 3 would NOT
recompute against reduced real capacity during an extended outage**,
because it inherits `usable_cap_by_type`'s existing static assumption.
This is not a new gap this proposal introduces — the GA's OWN current
capacity model has exactly the same blind spot today, for everything, and
even today's "escalate to full reschedule" fallback would still plan
against the same static cap. It matters more once a workload gate exists,
though: the entire point of the gate is to admit orders only as fast as
the shop floor can genuinely absorb them, and a gate computed against a
capacity figure that doesn't reflect a real, ongoing multi-type outage
would keep admitting orders at a rate the ACTUAL degraded floor cannot
handle — over-admitting during exactly the situation the mechanism exists
to prevent.

**Design requirement, flagged as a shared prerequisite, not scope creep**:
if this proposal proceeds, the norm's denominator should be sourced from a
live-capacity-aware count (total minus reserve minus currently
Breakdown/Under_Maintenance, refreshed each time the gate runs) rather
than the static company-wide cap. This is arguably a fix to
`utils/machine_reserve.py` (or a new live-capacity query) that the
existing GA capacity model would also benefit from — found here, not
previously documented anywhere in this project's history — and should be
scoped as its own small piece of work reviewed alongside this proposal,
not bolted on silently inside the gate's own code.

### 8.5 A near-empty order book

The safe, correct degenerate case, confirmed by construction rather than
needing new logic: with very few orders, projected pool workload stays far
under norm (Section 5's finding that even the FULL current 1,029-order
book's same-day floor per type is only 1.8-2.8 days makes this concrete —
a near-empty book is a small fraction of that), so the gate never binds
and every eligible order is admitted immediately — this converges exactly
to Section 8's (of `GA_AUDIT.md`) current release-date-only behaviour, with
no special-casing required. Worth flagging only because the fixed,
constant-days norm proposed in Section 4 has no divide-by-pool-size or
percentile-of-recent-history term that could misbehave at low volume; if a
future refinement moves to a dynamically-fitted norm instead of a fixed
constant, a near-empty-book cold-start case would need separate handling
that this proposal, as scoped, does not need.

---

## 9. Shared prerequisite — IMPLEMENTED, VERIFIED (2026-08-24)

All three items (feasibility+starvation guard, manager bypass, live
capacity source) approved. Sequenced first, as its own small,
independently-testable fix, per instruction — the release gate itself
(feasibility/starvation guard and manager bypass built in from the start)
is NOT yet implemented; this section covers only the prerequisite.

**Implemented**: `utils/machine_reserve.py::build_reserve`'s
`usable_cap_by_type` is now live-capacity-aware — the 93%-of-total policy
cap minus however many of those specific non-reserved machines currently
have status `Breakdown`/`Under_Maintenance` (new shared
`NON_USABLE_STATUSES` constant, also used to de-duplicate the equivalent
inline tuple already in `reserve_pool_by_type`). Reserve MEMBERSHIP
(`reserved_asset_ids`) is deliberately untouched — stays the same
deterministic, status-blind set the module's docstring already commits to;
only the aggregate ceiling number changes. Explicitly NOT fixed here (out
of scope, flagged in Section 8.4): `ga/chromosome.py`'s per-line machine
pools still list every non-reserved `asset_id` regardless of status, so a
broken machine's ID can still appear in a schedulable pool — this fix
raises the honesty of the AGGREGATE bound only, not per-machine placement.

**Verified in isolation, before moving on**: 10 new tests
(`tests/test_machine_reserve_live_capacity.py` — all-active parity with
the old behaviour, broken-non-reserved-machine reduces capacity,
broken-reserved-machine costs nothing, reserve membership stays
status-blind across differing breakdown patterns, floors at 0 not
negative, `Under_Maintenance` counts the same as `Breakdown`, `Idle` does
NOT count as unusable, missing `current_status` column reproduces old
behaviour exactly, duplicate `Machine_Capabilities` join rows per asset
don't double-count, multiple types tracked independently). Full suite
418/418 passing.

**Verified against the real live database, not only synthetic fixtures**
(`scratch/live_capacity_fix_check.py`) — and this surfaced a genuine,
non-hypothetical finding, not just a fixture check: **303 of 3,000
machines (10.1%) are currently `Breakdown` or `Under_Maintenance`**
(168 + 135), already exceeding the 7% reserve fraction the system was
sized around. Every one of the 5 pools is currently running 10-11% below
its old static cap:

| type | policy cap (old) | currently down | live cap (new) |
|---|---|---|---|
| bar-tack pocketing | 375 | 40 | 335 |
| body assembly | 816 | 87 | 729 |
| flatlock hemming | 418 | 44 | 374 |
| seam edge overlock | 845 | 83 | 762 |
| tubular sleeve join | 333 | 33 | 300 |

This is not a contrived extended-outage scenario for testing purposes —
it is today's actual seeded database state.

> **A standalone data-grounding finding, not a gate finding — kept
> separate on purpose.** The 10.1% down-rate is not this proposal's result
> to claim; it's a property of the underlying data the fix merely exposed
> by finally reading `current_status` for something real. Full writeup in
> `DATA_AUDIT.md`'s `Machinery_Assets` entry (updated 2026-08-25): the
> seeder's own stated intent (`machine_status_weights = [90, 5, 3, 2]`, a
> 7% Under_Maintenance+Breakdown target at initial assignment) plus
> dynamic mid-simulation breakdowns compound to a measured 10.1% on the
> live database — already above the 7% `MACHINE_RESERVE_FRACTION` the
> standby reserve was sized around. No real company file contains
> machine-status data at all (DATA_AUDIT.md's original finding, unchanged),
> so grounding this properly needs new information from the factory, not a
> re-read of existing files. Not urgent, flagged so it doesn't get lost.

---

## 10. The release gate itself — IMPLEMENTED, VERIFIED (2026-08-25)

**Implemented**: `ga/release_gate.py::ReleaseGate` — the pre-shop pool and
workload-norm admission gate described in Sections 3-8, with all three
approved guards built in from the start, not added after:
- **Feasibility check + honest report** (Section 8.1): every pooled
  order's own release + critical-path is checked against its deadline at
  construction; a structurally-unreachable order is reported explicitly
  (`self.infeasible_orders`, plus a distinct log line) and STILL pooled
  and scheduled best-effort — reporting is not the same as dropping a
  real customer order, which stays a business decision, not this gate's.
- **Starvation override** (Section 8.1): an order waiting past
  `starvation_days` (default 14.0, flagged as needing tuning like every
  other constant here) force-admits regardless of norm — no order can
  wait in the pool forever.
- **Manager bypass** (Section 8.3): `manager_forced_ids` skips the norm
  check entirely, including the release-not-yet-arrived check (safe --
  `ga/fitness.py`'s ready-time floor independently still prevents it from
  actually starting early).

Wired into `ga/rolling_horizon.py::run_rolling_horizon` as an opt-in
`release_gate=None` parameter (default reproduces the original positional-
slicing behaviour EXACTLY, unchanged) plus a new `manager_prioritized_orders`
parameter — which also fixed a related, previously-unnoticed gap: the
rolling-horizon path (what every multi-window measurement in this project
has used, including every number in Sections 5-9) never threaded manager
priority through to `GAScheduler` at all before this change.

**A real design gap was caught and fixed before it shipped**: the first
draft let the gate admit an order whose real release date hadn't arrived
yet, just because its load happened to fit the norm — silently defeating
the entire point of a pre-shop pool (planning against orders that don't
exist yet). Fixed by gating eligibility on `release_mins <= current_time_mins`
first, with `current_time_mins` supplied by the caller
(`ga/rolling_horizon.py` passes `max(machine_free.values())`, i.e. real
progressed simulated time) rather than derived internally from the pool.

**A second real risk was found and closed**: a naive integration could
deadlock — if a round admits nothing (everything either not yet released
or norm-blocked), nothing advances `current_time_mins`, so nothing can
ever become eligible or starve. Fixed with an explicit fast-forward: jump
to the next order's real release date if that's what's blocking, or
straight past the starvation threshold if everything pending is already
released but norm-blocked — guarantees termination by construction (each
empty round strictly advances time; starvation guarantees eventual
force-admission), confirmed by dedicated loop-termination tests, not just
argued.

**Verified in isolation**: 23 tests (`tests/test_release_gate.py`) —
norm-respecting admission, look-ahead-past-a-blocked-order, outstanding
load freeing up on completion, not-yet-released orders never admitted
early, manager bypass (including bypassing the release check), starvation
force-admit (and never firing before real release), feasibility
report+non-drop, **explicit confirmation the norm reads LIVE
`usable_cap_by_type` and reacts immediately to a full-type outage**, and
three loop-termination integration tests mirroring
`ga/rolling_horizon.py`'s exact fast-forward logic (normal norm, an
impossibly tight norm relying purely on starvation, and orders released
far apart in time) — each asserting the loop provably terminates and
every order is eventually admitted. Full suite: 441/441 passing (default
`run_rolling_horizon` path confirmed byte-for-byte unaffected).

**Verified against the real live order book**, not only synthetic
fixtures (`scratch/release_gate_real_data_check.py`):

- **Norm sizing confirmed live, exactly as required**: every pool's norm
  computes to precisely 2.00 days/machine from `factory.usable_cap_by_type`
  — 335/729/374/762/300 machines (today's live, breakdown-adjusted counts
  from Section 9), not the static 375/816/418/845/333 baseline.
- **1,029 orders pooled** (the resampled synthetic book). **380 reported infeasible —
  disaggregated before trusting the number, not reported flat**: 378 of
  380 simply have a deadline already elapsed as of today (real simulated
  time has passed enough that many early-released orders' delivery dates
  have already gone by — a genuine, different, pre-existing condition,
  not a gate finding). Only **2 orders are genuinely infeasible in the
  sense this guard exists to catch** (a real future deadline structurally
  unreachable regardless of scheduling) — orders 150 and 209, both
  marginal (0.1d and 0.5d shortfalls). Each report dict carries
  `deadline_days`, so a consumer can already separate "already overdue"
  from "structurally impossible" without a code change; worth a one-line
  filter note for whoever builds a dashboard on this, not urgent.
- **Admission pacing works as designed**: rounds 1-2 admit 578 orders
  from the day-0 backlog (near-exact match to Section 8's independently-
  measured 577-in-flight finding — 1-order difference is boundary
  rounding, not a discrepancy), then the pool correctly throttles to
  small batches (5, 9, 8, 9...) as it fast-forwards through subsequent
  real release dates — spreading admission over time instead of dumping
  everything at once, the entire point of the mechanism, now confirmed
  on the real order book rather than only argued from theory.

---

## 11. Gated-buffer x release-gate interaction — MEASURED (2026-08-25),
    NOT a clean win either way

**An efficiency prerequisite was fixed first, in production code, before
this run**: the naive per-`admit_next_batch`-call round structure would
have needed hundreds of GA rounds once the day-0 backlog cleared (each
paying a fixed `ChromosomeFactory`-rebuild cost) — see
`ga/rolling_horizon.py`'s accumulation logic, added specifically to make
this ablation tractable. Verifying it in isolation
(`tests/test_release_gate.py`) caught a real correctness bug before it
touched real data: accumulation's own forward-scanning could manufacture
FALSE starvation and cascade-admit an entire norm-blocked batch at once,
defeating the norm. Fixed by tracking two separate clocks (`real_now_mins`
for starvation, a separate `scan_time_mins` for release-eligibility
lookahead) rather than one — full rationale and the specific test that
caught it are in the module's own docstring. 26 release-gate tests + 444
full suite passing before this ablation ran.

**Three arms, one process, one origin, one seed**
(`scratch/gated_buffer_release_gate_interaction.py`), matching the
methodology of every prior ablation: `A_current_default` (buffer ON —
today's shipped default — gate OFF), `B_both` (buffer ON, gate ON at
`norm_days=2.0`), `C_gate_only` (buffer OFF, gate ON). The accumulation
fix held up on real data exactly as intended: all three arms completed in
**4 GA rounds each** (matching the ungated baseline's own window count,
not the hundreds a naive per-call design would have needed), total
runtime ~46 minutes.

| arm | on-time | makespan | throughput | mean-tard | labour | margin |
|---|---|---|---|---|---|---|
| A_current_default | 56.5% | 326.5d | 3.2/d | 85h | 0.5% | +3.5% |
| B_both | 57.9% | 326.5d | 3.2/d | 97h | 0.5% | +1.2% |
| C_gate_only | 57.9% | 326.5d | 3.2/d | 97h | 0.5% | +0.6% |

**Finding 1 — the gate is a genuine tradeoff, not a free improvement.**
On-time rises modestly (A→B: 56.5%→57.9%, +1.4pp), but mean tardiness gets
WORSE (85h→97h, +14%) and the GA's own margin over the best baseline
shrinks substantially (+3.5%→+1.2%). A plausible mechanism, NOT confirmed
at the per-order level: pacing admission lets a few borderline orders
squeak in on time, while orders already destined to be late get pushed
later still by the delayed admission, dragging the mean up even as the
COUNT of late orders falls. Reported as a hypothesis, not a finding.

**Finding 2 — the confidence-gated risk buffer (`GA_AUDIT.md` Section 6)
becomes completely redundant once the gate is active.** B and C are
IDENTICAL on every real schedule-quality metric — on-time, makespan,
throughput, mean-tardiness, labour, all to the reported precision. Only
the GA's own internal fitness margin differs (+1.2% vs +0.6%), meaning
the buffer still nudges what the GA scores itself against but changes
nothing about the actual resulting schedule once the gate's coarser
admission-pacing is in control. This is the "one mechanism makes the
other redundant" outcome flagged as an explicitly valid result before
this ablation ran, not a disappointment to explain away.

**Makespan is unchanged across all three arms (326.5d)** — expected and
consistent with Section 8's own framing: the gate paces WHEN orders
compete for machines, not the total real work or the last order's real
release date, which is what actually sets the makespan ceiling.

**382 infeasible orders reported in both gated arms** (vs. 380 in
Section 10's earlier real-data check — the ~1-day difference in `ORIGIN`
between the two runs shifts which orders' deadlines have crossed into the
past, exactly as expected, not a discrepancy). Same disaggregation
caveat as Section 10 applies: the overwhelming majority are already-past-
deadline orders, not gate-specific findings.

**Honest bottom line — this is a genuine business tradeoff, not a
recommendation this document is making unilaterally**: adopting the gate
(at this starting norm) trades a small on-time gain for worse tardiness-
among-the-late and a smaller GA margin, AND makes the already-adopted
buffer pointless to keep running. Whoever decides needs to weigh whether
+1.4pp on-time is worth +14% mean tardiness and a ~2.3pp margin loss —
this document reports the number, not the weighting. Single-seed result,
same caveat carried since the original buffer ablation
(`GA_AUDIT.md` Section 6): not yet cross-seed-confirmed, so treat the
exact magnitudes as provisional, though the DIRECTION (real tradeoff, real
redundancy) is unlikely to be pure noise given how cleanly B and C matched.
