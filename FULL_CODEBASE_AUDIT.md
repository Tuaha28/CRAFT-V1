# Full Codebase Audit (2026-09-08)

Same discipline as `GA_AUDIT.md`/`DATA_AUDIT.md`: read the code as it exists
today, describe what's actually there with real file/line references, and
make no changes here. Scope was every module in the live pipeline —
`database/`, `utils/`, `graph/`, `models/`, `ga/`, `agent/`, `reporting/`,
`api/`, `frontend/`, and the routing side of `data_generation/` — read in
full, module by module, checking both within-file correctness and
cross-file consistency (shared column names, shared constants, "the
exported plan must be the plan that was scored" invariants this project
already cares about elsewhere).

**Headline: this codebase is unusually well self-audited already.** Nearly
every file carries first-person comments documenting a bug found and fixed
in a prior session, with real measured numbers, not assumptions. The large
majority of what was read had nothing new to report. Two real, currently-
live bugs were found that are not documented anywhere else in the project;
they're Sections 1–2 below. Sections 3–5 are smaller or already-known items
worth restating because they're still true today.

---

## 1. Multi-window schedule export ignores carried-forward machine state

**Severity: High. Live in the production pipeline (`run_final_pipeline.py`), not just a script.**

`agent/decision_engine.py::_export_proposed_schedule` initializes its own
internal simulation state unconditionally from empty:

```python
# agent/decision_engine.py, lines 548-549
machine_free = {}
machine_last_order = {}
```

There is no parameter on this method for a caller to supply prior machine
state. Confirmed by search: the strings `initial_machine_free` and
`initial_machine_last_order` do not appear anywhere in `decision_engine.py`.

But `ga/rolling_horizon.py::run_rolling_horizon` — the function
`run_final_pipeline.py::do_schedule()` actually calls with `export=True` in
the real pipeline (see that file's `do_schedule()`, which imports and calls
`run_rolling_horizon(..., export=True, decision_engine=eng, ...)`) — scores
each window's *committed* chromosome like this, a few lines before its own
export call:

```python
# ga/rolling_horizon.py, ~line 278
_f, det = evaluate_schedule(
    committed_chromo, committed_num_ops, sched.op_details, sched.gnn_predictions,
    ...
    initial_machine_free=machine_free,
    initial_machine_last_order=machine_last_order, return_details=True,
    ...
)
...
if export and decision_engine is not None:
    label = None if w == 1 else f"w{w}"
    summary = decision_engine._export_proposed_schedule(
        committed_chromo, committed_num_ops, sched.op_details, preds, df_lines,
        sched.factory.eligibility_map, sched.order_deadline_abs,
        sub, df_machines, label=label,
        machine_pools=sched.factory.machine_ids_per_line_map,
        operator_index=operator_index, present_operator_count=capacity,
        usable_machine_count=sched.usable_machine_count,
        op_optype=sched.factory.optype_for_op,
        protected_machine_type=sched.protected_machine_type)
```

`machine_free`/`machine_last_order` here are the *real* busy-machine state
carried forward from every previously committed window — exactly the state
`evaluate_schedule` just scored the committed subschedule against. The
`_export_proposed_schedule` call right below has no way to receive that
same state, and there is no `initial_machine_free`/`initial_machine_last_order`
parameter in its signature to pass it through even if the caller wanted to.

**Consequence:** for window 1 this is harmless (the factory really is idle
at t=0). For window 2 onward, `_export_proposed_schedule` re-derives every
operation's start/end time from its own fresh, empty `machine_free = {}` —
i.e. it decodes the chromosome as if every machine were free at that
window's own t=0, which is not the schedule `evaluate_schedule` actually
scored a few lines earlier in the same loop. The exported
`proposed_schedule_w2.json`, `proposed_schedule_w3.json`, etc. (and the
`order_completions_w2.json` etc. rolled up from them) will show start/end
times, machine assignments, and changeover flags that do not match the
schedule that was actually optimized and evaluated for that window.

This is precisely the failure class this codebase already prides itself on
having eliminated — see `agent/decision_engine.py`'s own comment on
`select_machines_and_start` about the post-hoc high-risk-reroute bug ("if
the two computed start times differently, the exported plan would not be
the plan that was scored — a defect this codebase has already had once").
It appears to have re-entered via the newer overlapping-lookahead rolling
horizon export path, which was added after that invariant was established
and never wired to preserve it.

**Not affected:** `agent/decision_engine.py::trigger_rescheduling()`, the
single-shot (non-rolling) path used by the dashboard's "Full Reschedule"
button and by direct API calls. That path only ever plans one window
against the live, actually-idle-or-busy database state at call time, so
starting `_export_proposed_schedule`'s simulation from empty is correct
there — there is no "previous window" to carry forward.

**Fix shape (not applied, per this document's own convention):** add
`initial_machine_free=None, initial_machine_last_order=None` parameters to
`_export_proposed_schedule`, seed its local `machine_free`/`machine_last_order`
from them (falling back to `{}` exactly as now when `None`), and pass
`sched`'s carried-forward state through from both call sites in
`ga/rolling_horizon.py` (`run_rolling_horizon`'s per-window export call
shown above).

---

## 2. `apply_local_patch`'s SQL updates can corrupt multi-machine operations

**Severity: High. Self-flagged as never tested against a live database.**

`agent/impact_assessor.py`'s own module docstring already says: *"this
module has been logic-checked against the schema used elsewhere in this
codebase ... but has not been run against a live database — no DB is
available in this environment. Test it against a real breakdown scenario
before relying on it."* This section is exactly the kind of bug that check
would have caught.

`apply_local_patch()` writes the primary reassignment like this:

```python
# agent/impact_assessor.py, ~lines 575-579
session.execute(text("""
    UPDATE Job_Shop_Schedules
    SET asset_id = :new_asset, planned_start_time = :new_start, planned_end_time = :new_end
    WHERE production_order_id = :oid AND operation_id = :op_id
"""), {...})
```

The `WHERE` clause has no `asset_id` predicate. It matches **every**
`Job_Shop_Schedules` row for that `(production_order_id, operation_id)`
pair — not just the one row that was actually sitting on the broken
machine.

This matters specifically because of how central machine-level parallelism
is to this whole system's design: an operation legitimately runs on
several machines at once (the MC/machine-count gene throughout `ga/`), so
it legitimately has several `Job_Shop_Schedules` rows, one per assigned
asset. `_get_affected_operations()` (a few lines above in the same file)
correctly scopes to the broken asset (`WHERE asset_id = :aid`), so the
*assessment* is computed correctly, but the *write* in `apply_local_patch`
throws that scoping away. If only one of an operation's several machines
broke down, this statement reassigns **all** of that operation's rows —
including the ones on machines that never broke — onto the single
replacement `new_asset`, both destroying the operation's real parallelism
and potentially double-booking `new_asset` with work that was previously
spread across several machines.

The donor-preemption update immediately below has the identical gap:

```python
# agent/impact_assessor.py, ~lines 587-597
if donor is not None:
    session.execute(text("""
        UPDATE Job_Shop_Schedules
        SET planned_start_time = :new_start, planned_end_time = :new_end
        WHERE production_order_id = :oid AND operation_id = :op_id
    """), {...})
```

This one is harder to fix in place: `_attempt_donor_preemption()`'s
`donor_op` dict (built around line 312) never captures the donor's own
`asset_id` at all — only `production_order_id`, `operation_id`, old/new
start/end, and slack/urgency. The caller (`assess_machine_breakdown`) does
know the donor's asset (it's the `alt_asset_id` the blocking row was found
on), but that value isn't threaded through into `donor_op`, so fixing this
update needs a small data-flow change, not just an added `WHERE` clause.

**The primary-operation fix is a one-line addition**, since the needed
value is already on hand: `assessment['original_asset_id']` is the broken
machine's asset id, and adding `AND asset_id = :old_asset` (bound to it)
to the first `UPDATE` scopes it to exactly the row that needs to move.

---

## 3. Dashboard "Changeovers Applied" metric is silently stuck at zero

**Severity: Medium. User-visible, no data corruption.**

`agent/explainability.py::generate_executive_summary()` detects a
changeover by substring-matching the operation's XAI text:

```python
# agent/explainability.py, line 35
changeover_ops = [op for op in schedule_data if "changeover buffer" in op.get('xai_reason', '')]
```

The actual text generator in `agent/decision_engine.py` was rewritten (as
part of the multi-machine/multi-operator XAI upgrade) to say:

```python
# agent/decision_engine.py, line 719
xai_reason += f" At least one assigned machine was last running a different order — {CHANGEOVER_MINUTES:.0f}min re-rigging charged to it."
```

— "re-rigging charged to it", not "changeover buffer applied". (The old
phrase is literally the wording from the very first, pre-audit version of
this file.) The two files drifted apart when one was updated and the other
wasn't.

`frontend/dashboard.py` surfaces this count directly to a user:

```python
# frontend/dashboard.py, line 308
c3.metric("Changeovers Applied", summary["style_changeovers_applied"])
```

This metric will now read 0 on every run, regardless of how many
changeovers actually occurred. The other two substring checks in the same
function (`"exceeded"` for reroutes, `"past its"` for deadline warnings)
still match current wording and are unaffected.

---

## 4. Confirmed still-open, previously self-documented gaps

Not new findings — re-verified directly against today's code to confirm
they're still true, since a fresh audit should say so either way rather
than silently repeat stale claims.

**4a. The MC-gene's per-line ceiling still isn't machine-status-aware.**
`utils/machine_reserve.py::build_reserve`'s own docstring (2026-08-24 entry)
already states: *"this does NOT touch which specific machines are
schedulable (`ga/chromosome.py` still lists every non-reserved asset_id
regardless of status) — a separate, deeper gap, not fixed here."* Verified
directly: `ga/chromosome.py::ChromosomeFactory._build_eligibility_map`
(lines 68–74) filters `active_machines` only by `line_id` presence and
`is_active` lines, never by `current_status`. So while the company-wide
aggregate cap (`usable_cap_by_type`) correctly subtracts machines currently
`Breakdown`/`Under_Maintenance`, the *per-line* `machines_per_line_map` and
`machine_ids_per_line_map` — which bound the MC gene and list the actual
machine IDs the GA and fitness function schedule onto — still count and
include machines that are currently down. A single operation's machine
count could still be bounded, or literally scheduled, against a broken
machine.

**4b. `agent/impact_assessor.py` is still explicitly self-flagged as
untested against a live database.** See Section 2 above for a concrete
illustration of why that caveat is not just boilerplate caution.

---

## 5. Minor / cosmetic

**5a. Misleading variable name in `models/evaluate.py`.**
```python
# models/evaluate.py, line 135
mse_prio = mean_absolute_error(true_priority, pred_priority)
```
Despite the name, this computes MAE, not MSE. The returned dict key
(`priority_mae`) is correctly named, so there's no functional impact —
purely a naming trap for a future reader.

**5b. Duplicated magic number in `graph/node_features.py`.**
```python
# graph/node_features.py, line 407
machine_parallelism_norm = (parallelism_source / 60.0).fillna(0).clip(upper=1.0).values.reshape(-1, 1)
```
The `60.0` here is meant to be `GA_LOTS_PER_OPERATION` (`utils/config.py`,
currently `60`), but is a literal rather than an import. Harmless while the
two values happen to match; would silently desynchronize this feature's
normalization from the rest of the system if `GA_LOTS_PER_OPERATION` is
ever tuned.

**5c. `utils/helpers.py` is entirely dead code, and its one interesting
function would crash if it weren't.**
`calculate_working_minutes`, `clean_text_for_rag`, and `normalize_sam` have
zero callers anywhere else in the codebase (confirmed by search). Under the
current `SHIFT_START_HOUR=0, SHIFT_END_HOUR=24` config (`utils/config.py`),
`calculate_working_minutes`'s `current.replace(hour=SHIFT_END_HOUR, ...)`
would evaluate to `current.replace(hour=24)`, which raises `ValueError:
hour must be in 0..23` — a live crash bug, currently inert only because
nothing calls the function.

---

## What was read but not re-litigated

`ga/fitness.py`, `ga/scheduler.py`, `ga/operators.py`, `ga/heuristics.py`,
`ga/local_search.py`, `ga/alns.py`, `ga/release_gate.py`,
`graph/edge_builder.py`, `graph/graph_builder.py`, `graph/labels.py`,
`models/gnn.py`, `models/train.py`, `database/queries.py`, `database/db.py`,
`database/ga_snapshot.py`, `utils/config.py`, `utils/shift_rotation.py`,
`utils/machine_taxonomy.py`, `agent/monitor.py`, `agent/event_handler.py`,
`agent/fuzzy_logic.py`, `reporting/run_report.py`,
`reporting/evaluation_report.py`, `data_generation/routing_tables.py`, and
`api/main.py` (confirmed intentionally empty) were all read in full this
pass. Nothing new to report in any of them — each already carries its own
accurate audit trail from prior sessions, and cross-checking against
current behaviour didn't turn up anything they got wrong.

Not read this pass: `tests/*` (33 files — test-suite coverage was not in
scope for this audit) and the full body of
`data_generation/seed_database_fixed.py` (1,728 lines — a one-time seed
script already covered by `DATA_AUDIT.md`/`DATA_REBUILD_LOG.md`; only its
routing-table companion, `data_generation/routing_tables.py`, was read in
full here).
