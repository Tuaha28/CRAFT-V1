# Execution Boundary Contract V1

This contract converts verified source execution evidence to a deterministic operation snapshot. It neither schedules work nor authorizes a residual execution plan. Its admission gate is deliberately stricter than state classification: a correctly classified snapshot may still be blocked from CRAFT.

## Source and clock

The supported source is `garment_fyp_db` with `Dataset_Calibration.payload.timeline_generation_contract = frozen-plan-then-actual-v1`. The schema and generator were inspected, not inferred from field names. The snapshot retains the dataset version and generation reference time, and takes an explicit requested reference time (defaulting to that generation reference).

The legacy timestamps are timezone-unspecified local dataset times. No current wall clock or timezone offset is substituted. This adapter rejects offset-bearing/mixed clocks until a separately defined conversion contract exists. Source records are read in one repeatable-read consistent snapshot in a read-only transaction.

Generator evidence: `data_generation/seed_database_fixed.py` computes complete simulated execution intervals and segments, including future execution, before assigning the snapshot status. Done means end <= generation reference; Scheduled means start > generation reference; otherwise the row is either Delayed or Running. Delayed is started execution with causal delay, not evidence of an unstarted task.

## Lot normalization

For supplied reference T and supported timestamp provenance:

| Evidence | Normalized state |
|---|---|
| Valid start S and end E, S <= E <= T | COMPLETED |
| Valid start S <= T, with E > T or no completion timestamp | IN_FLIGHT |
| Valid future start S > T, with no contradictory end | NOT_STARTED |
| Missing start, reversed timestamps, unsupported provenance | UNRESOLVED |

Intervals are half-open. A lot ending at T is completed; a lot starting at T is in flight. Running and Delayed receive the same treatment for identical timestamps. At the generation reference, a conflicting stored status is an admission error. At other supplied times, the generation-time status is retained for provenance but does not override timestamp evidence.

Malformed segments, missing/negative/fractional quantities, duplicate row/physical-lot identities, ownership errors, and unknown data block admission. Non-null generated future actuals are not observed completion, and scheduled timestamps alone never authorize work to be marked complete.

`actual_work_segments` is split at T into observed productive segments and projected segments. A future generated end is retained under `projected_end`, not as an authoritative committed completion boundary. Planned fields and the original source rows remain in the input capture for audit.

## Operation normalization and quantities

An operation is identified by `production_order_id_operation_id`. A schedule row uses `schedule_item_id`; physical lot identity is operation plus `lot_index`. Routing comes from the order's product routing sequence.

- All lots completed: **FULLY_COMPLETED**, historical/fixed, not optimizable. Completion boundary is the maximum observed completed-lot end.
- All lots not started: **UNSTARTED**, potentially optimizable. Release and predecessor gates still apply; optimizable does not mean currently executable or scope-admissible.
- Any started/completed work with any unfinished work: **PARTIALLY_EXECUTED**, never freely optimizable. This also includes completed-prefix plus unstarted-tail operations with no lot currently in flight.
- Missing/contradictory evidence: **UNRESOLVED_STATE**, never optimizable.

For every operation:

`total lot quantity = order target quantity`

`completed quantity = sum(quantity of COMPLETED lots)`

`remaining quantity = total - completed = in-flight quantity + not-started quantity`

These are whole-lot completion units. The adapter does not estimate how many units within an in-flight lot are already finished. `in_flight_completed_units` and `remaining_productive_seconds` remain null. No duration is manufactured from elapsed wall time, percentage, SMV, or WIP presence.

WIP is corroborating evidence, not an additional completed-quantity source. The generator emits WIP for Done lots; the stored update timestamp may be ingestion time after the simulation origin. The adapter neither adds WIP quantities to lot quantities nor blindly drops all late-timestamp WIP evidence.

## Partial operation boundary and the unresolved-tail rule

Every partial operation remains in `boundary_evidence`. Every unfinished lot remains in `residual_lot_ids`, with its identity, quantity, known resources and projections retained. Every in-flight lot retains machine, worker and line identity and observed start.

An unresolved in-flight hold has `hold_from = T`, `release_at = null`. It is an unresolved resource association, **not a guessed finite reservation** and not a claim of continuous productive worker activity during a pause. It cannot be converted to CP-SAT intervals. The whole scope is withheld before core preparation.

The verified legacy schema does not distinguish an approved immutable residual plan from ordinary scheduled rows. Generated future `actual_*` values and old planned times do not establish such a commitment. Therefore this version cannot certify a partial operation's committed residual tail. It records:

`boundary_status = BOUNDARY_UNRESOLVED`

`completion_boundary = null`

`committed_execution = []`

`optimizable = false`

This is intentional. It avoids converting a full future simulated trajectory into an asserted live plan. No empty `committed_execution` means the tail was discarded: all tail lots remain in the snapshot and block admission.

To close this gap, an authoritative carry-forward plan must identify **all** residual lots and quantities, valid fixed resources/crews and productive segments, machine occupancy across pauses, physical completion bounds, and provenance/validation for those commitments. It must cover unstarted tail lots too. No manager checkbox or supplied Boolean is sufficient proof of physical validity. Such a producer is not implemented or simulated in this task.

## Residual-tail review (resumed task)

Case A is the conservative required treatment, but NOT a claim that a committed
plan already exists. Case B (independent residual migration) is unsupported by
the inspected operation-level contract. No partial operation becomes movable.

Inspected continuity chain:

| Source | What it proves | What it does not prove |
|---|---|---|
| `crafter/authoritative_semantics.py` | Fixed-crew calendar walk; continuous machine hold | Measured residual work or lot-to-operation commitment |
| `ga/schedule_utils.py:split_operation_work` | Setup elapsed minutes and serial productive minutes divided by machine count | Splitting completed lots from unfinished work |
| `ga/worker_allocation.py:allocate_crew` | Same selected crew resumes across common calendar/reservation pauses | The already-started lot crew can be replaced by newly selected workers |
| `utils/work_calendar.py:productive_segments` | Exact calendar walk GIVEN known positive productive minutes | Remaining productive minutes inferred from elapsed time |
| `scratch/audit_worker_segment_carry_forward.py` | A supplied planned segmented reservation survives load/decode/export | That the captured future simulation is an approved residual plan; probe uses a synthetic supplied row |
| `ga/rolling_horizon.py` | Carry-forward of already committed scheduled work | Independent rescheduling of live partially executed lots |
| `database/queries.py` | Legacy `remaining_frac` counts lots not Done | Measured within-lot progress or quantity-weighted residual duration |
| `crafter/lns_probe/exact_line_k_probe.py:_duration_record` | Formula based on supplied remaining fraction and efficiency | Independent observation of residual effort |
| `crafter/industry_model_v1.py` | Explicitly documents partial migration unsupported until WIP contract exists | An implemented live WIP continuation adapter |

A justified boundary would require a trusted total/residual productive-work
budget, observed productive consumption, fixed resources, all residual lot
quantities, calendar/reservation context and an approved continuation plan.
For such a plan, worker occupancy is its productive segments, machine occupancy
includes pauses, and operation completion is the latest physical tail end.
This is a required evidence contract, not a newly implemented scheduling formula.
Subtracting elapsed wall time from an old span or using a future simulated end
is not permitted. No supplied captured partial operation satisfies that contract.

`residual_tail.py` records missing evidence and retained resource associations
for every partial operation without changing the normalized snapshot. It does
not certify supplied plans or construct new ones. The independent validator
also rejects fabricated residual progress and finite execution boundaries.

All 82 partial operations remain `BOUNDARY_UNRESOLVED`: 17 have in-flight lots,
65 have completed prefixes with unstarted tails. Zero have a proven committed
tail; zero have proven splittable residuals. For all, `execution_boundary_end`
is null and successor admission remains blocked. Known resources are preserved
as evidence, not represented as proven finite future occupancy.

## Precedence (admission rule)

The original order release is preserved. `earliest_ready_lower_bound` is a separate readiness bound and includes all known completed predecessor ends. An unresolved preceding partial operation leaves that bound unknown and explicitly blocks admission. Unstarted predecessors remain scheduling decisions, not falsely completed boundaries. Future releases remain future releases.

Historical lot pipelines may overlap whole routing operations. The adapter does not rewrite that observed history to invent whole-operation precedence. It withholds unresolved partial scopes; it does not permit a new successor to bypass an unresolved completion boundary.

## Independent validation and CRAFT handoff

`boundary_validator.validate_snapshot(snapshot, source)` independently derives expected lot states and quantities from the separately supplied source. It does not call the normalizer. It verifies identity, hashes, completion/residual membership, resource holds and ownership, segments, routing, and readiness bounds. Recomputing a tampered snapshot's hash does not bypass evidence checks.

All ten requested invariants are enforced. Partial/unresolved operations cause a `BOUNDARY_UNRESOLVED` error even when all classification/accounting checks pass. Unknown release times, resource references, and unsupported clocks also block admission.

`snapshot_adapter.craft_boundary_payload` calls that validator and raises `BoundaryUnresolved` before any scheduler import if it fails. It cannot filter out blockers to make a smaller scope pass. For an admissible completed/unstarted snapshot it exposes historical routing completions in the existing `WindowBoundaryState` field shape. It does not build a new scheduler or claim to produce the full CRAFT loaded-state tuple.

No real Factory-1 CRAFT preparation was attempted because this snapshot contains unresolved partial operations. A synthetic unit test of a completed/unstarted boundary is not reported as the requested real CRAFT smoke test.

## Identity

The snapshot contains factory, supplied reference, dataset identity, all orders/operations, resources/calendars, WIP evidence, precedence, observed history and unresolved boundary evidence. The source hash covers all captured inputs. Query row order is canonicalized; volatile fetch timestamps and runtime are excluded. The snapshot hash covers the entire normalized payload except its own hash field. SHA-256 detects changes; it is not a substitute for trusted source provenance.

## Files and checks

- `legacy_repository.py`: read-only verified schema access, no scheduler or legacy engine imports.
- `execution_state.py`: pure timestamp/lot/operation normalization.
- `snapshot_adapter.py`: deterministic snapshot and fail-closed handoff.
- `boundary_validator.py`: independent evidence validator.
- `contracts.py`: serialization, identity and clock primitives.
- `tests/test_execution_boundary.py`: synthetic edge/tampering tests and nine captured real cases.

From the repository root:

```powershell
venv\Scripts\python.exe web_v1/audits/execution_boundary_v1/run_audit.py
venv\Scripts\python.exe -m pytest web_v1/backend/integrations/craft/tests/test_execution_boundary.py -q -p no:cacheprovider
```

To reproduce normalization against the already captured immutable source without another database read:

```powershell
venv\Scripts\python.exe web_v1/audits/execution_boundary_v1/run_audit.py --from-capture
```
