# Integration Audit Ledger

Status: **IN PROGRESS -- no Integration Freeze has been declared.**

This ledger records the current production implementation, the five historical
repair batches, new evidence, and the validity of earlier experiments. The
repository and reproducible artifacts are authoritative; historical prose is
used as a map, not as proof.

## Audit baseline

- Live database: `garment_fyp_db`
- Current confirmed company scale: 3,000 machines and 9,000 employees
- Current data: 1,029 orders, 4,139 routing rows, 176,187 lot schedule rows
- Pre-audit suite: 814 passed, 0 failed
- Evidence: `outputs/integration_audit/baseline_tests.log`

## Authoritative production flow (current)

| Concept | Source of truth | Production consumers | Current contract |
| --- | --- | --- | --- |
| Customer deadline | `Purchase_Orders.delivery_deadline` | routing query, GA tardiness, disruption slack | Customer promise; internal `scheduled_end_date` is not substituted |
| Release date | `Production_Orders.scheduled_start_date` | routing query, GA ready-time floor, rolling windows | Converted to minutes from one scheduling origin and clamped at zero |
| Remaining work | lot statuses in `Job_Shop_Schedules` | routing query `remaining_frac`, scheduler work content | Done lots excluded from demand; unfinished fraction scales target quantity |
| Routing order | `Routing_Operations.operation_sequence_number` | graph precedence, chromosome repair, fitness validator, heuristics, polish | Operation ID never defines precedence |
| Work content | `standard_allowed_minutes * remaining quantity * dataset calibration / efficiency` | seeder and scheduler through separate population-level inputs | Minutes; scheduler divides by MC exactly once |
| Factory ownership | `Order_Factory_Ownership` | chromosome factory and factory-scoped graph contention | Required and complete except explicit legacy mode |
| Machine capability | `Machine_Capabilities` plus asset line/factory | chromosome pools, decoder, validator | Correct type, line and owned factory required |
| Machine usability | `Machinery_Assets.current_status` plus deterministic reserve | chromosome pools, fitness denominator | Breakdown and maintenance assets are excluded |
| Worker skill | `Operator_Skill_Matrix.operation_type` | exporter crew pools and efficiency ranking | Skill and factory are hard eligibility constraints |
| Worker calendar | batch rotation/rest day or floater Work_Schedules | seeder, phenotype decoder and export | Same fixed crew resumes; common productive segments advance work |
| Fixed work | occupying schedule rows classified by status/start relative to origin | rolling machine/operator initial state | Fixed is retained; replaceable future work is released and rescheduled |
| GNN feature contract | checkpoint-associated preprocessing/feature-schema artifact | training, evaluation, decision engine, both pipeline scripts | Versioned ordered schema; input width is never inferred from a literal |
| Delay target | operation-level `max(actual_end)-max(planned_end)` | delay loss/evaluation | Validity-masked operation completion lateness |
| Completion target | valid planned operation span | completion loss/evaluation | Plan reproduction; unavailable targets are masked |
| Priority target | frozen train-split slack percentile curve | priority loss/evaluation/inference | Train-derived cut-points persisted with artifacts |
| Schedule scoring | ga.fitness.evaluate_schedule | GA and deterministic baselines | Worker-aware phenotype determines elapsed completion and fitness |
| Export | AIDecisionEngine._export_proposed_schedule | rolling and direct rescheduling | Serializes the scored phenotype; hash parity is enforced |

## Integration invariants

1. Every active order-operation key is unique and maps to one routing
   sequence, one owned factory, and at least one usable compatible machine.
2. Identifier normalization is explicit at subsystem boundaries; database
   metadata is never compared case-sensitively.
3. Quantities, SAM/SMV, efficiencies, durations, origins and deadlines retain
   documented units through every conversion.
4. Simulator, evaluator, exporter and reservation carry-over use one execution
   calendar and agree on elapsed completion and occupied intervals.
5. Fixed work is represented once: retained reservations are not also offered
   as replaceable demand.
6. GA and baselines see the same orders, ownership, resource pools,
   commitments, decoder, evaluator and export feasibility rules.
7. Each GNN output is classified as operationally active, candidate-invariant,
   export-only, or unused through a controlled perturbation.
8. Every exported candidate passes an independent validator before it can be
   treated as executable.
9. Run, dataset, snapshot, checkpoint, preprocessing and split identities are
   mutually verified.

## Identifier and join audit

Evidence: `outputs/integration_audit/identifier_joins.json`. The probe uses
the production routing, machine, worker, assignable-operator, assignment and
ownership readers, then independently checks the resulting keys against direct
database joins.

| Transformation | Input | Matched/output | Unmatched | Duplicated |
| --- | ---: | ---: | ---: | ---: |
| Active orders -> product routing | 911 orders | 7,809 operation rows | 0 expected operations | 0 order-operation and 0 order-sequence rows |
| Active operations -> stored lot schedule | 7,809 operation keys | 7,809 covered | 0 | 0 duplicate lot indices |
| Active orders -> ownership | 911 orders | 911 owned | 0 | ownership table is one row per all 1,029 orders |
| Owned operation -> local machine capability | 7,809 operation rows | 7,809 feasible | 0 | 0 duplicate asset-capability pairs |
| Machine query -> graph/scheduler assets | 3,000 assets | 3,000 rows/IDs | 0 | 0 |
| Schedule -> asset line/factory/ownership | 176,187 lot rows | 176,187 consistent | 0 mismatches | primary row identity retained |
| Schedule -> machine capability | 176,187 lot rows | 176,187 compatible | 0 | 0 |
| Worker query -> graph workers | 9,000 employees | 9,000 worker rows/IDs | 0 | 0 |
| Assignable worker-skill query | 12,751 stored skill rows | 11,748 currently assignable rows | filtered by active/present/current schedule | 0 employee-skill duplicates |
| Schedule -> worker qualification | 176,187 lot rows | 176,187 qualified | 0 null/unqualified | 0 |
| All-lot assignment edge input | 176,187 lots | 156,600 distinct order-operation-asset-worker tuples | 0 null asset/employee | 0 tuple duplicates |

The 8,799 schedule order-operation pairs cover every order/status, while the
7,809 routing population deliberately contains only Pending, Processing and
Delayed orders. This population difference is not loss.

## Unit contract audit

Evidence: `outputs/integration_audit/unit_contract.json`. The probe measured
the columns actually read by the simulator and scheduler, rather than a
similarly named report field.

| Quantity | Production expression | Unit/current finding |
| --- | --- | --- |
| Simulator productive work | `0.825454 * lot_quantity * standard_allowed_minutes / (employee_skill_percent / 100)` | productive minutes per lot |
| Scheduler productive work | `0.825454 * standard_allowed_minutes * round(target_quantity * remaining_frac) / (mean_active_line_efficiency_percent / 100)` | productive minutes per order-operation before division by machine count |
| Dataset calibration | `Dataset_Calibration.duration_calibration_factor` | dimensionless; current value 0.825454 and identity-verified |
| Employee skill | `Operator_Skill_Matrix.efficiency_rating` | percentage, divided by 100 exactly once |
| Scheduler efficiency | mean active `Production_Lines.target_efficiency_percentage` | percentage, divided by 100 exactly once; current mean 77.450606% |
| Setup/changeover | scheduler constants 15 and 25 | elapsed minutes; absent from the simulator/label model |
| Stored simulated span | productive calendar segments and elapsed start/end | seconds in SQL timestamps, converted to minutes for labels/scheduling |
| Deadline/slack | customer `delivery_deadline` minus one scheduling origin | minutes in GA, days in fuzzy triage |

Twenty stored lot rows were hand-reconstructed. Productive segments conserve
the formula within 0.01581 minutes (the expected one-second timestamp
quantisation), and every elapsed span is at least its productive span.

The shared units are correct, but two semantic differences remain open:

- the scheduler uses one global mean line efficiency, so an LA gene does not
  change processing speed;
- the scheduler adds setup/changeover while the simulator and completion
  targets do not model either quantity.

These are model-definition choices, not percentage/minute conversion defects,
and are not changed during this audit without a design decision.

## Seeder planned/actual dependency map

Evidence: `outputs/integration_audit/seeder_timeline_dependencies.json` and
`outputs/integration_audit/seed_plan_actual_coupling.json`. Both replays use
the exact iteration order and eligible pools consumed by the seeder.

| State domain | Contents | Dependency result |
| --- | --- | --- |
| Shared immutable identity | order/routing/lot quantity, owned factory, machine capability/line, eligible worker skill/batch/shift, calibration and iteration order | valid inputs to both timelines |
| Planned | `planned_machine_free`, `planned_employee_free`, `planned_prev_end`, planned segments/start/end | each clock advances from its own planned end |
| Actual | actual-end machine heap, `machine_free_time`, `employee_free_time`, actual `prev_end`, breakdown/repair/runtime/variance state | each clock advances from realized actual end |
| Actual -> planned coupling | asset popped from actual-end heap; employee minimized by actual employee clock | selected identity, line, employee efficiency and employee calendar enter the plan before `plan_ready` is calculated |
| Plan -> actual dependency | `actual_ready` is floored by planned start; actual reuses selected identities | prevents actual dispatch before its plan; this direction is explicit rather than future-information leakage |

The machine replay found 146,836/176,187 (83.34%) stored choices differed
from the plan-clock earliest asset. The independent employee replay found
146,665/176,187 (83.24%) differed from the plan-clock earliest employee;
both matched their actual-clock earliest resource on every row.

Predecessor readiness is correctly separated (`planned_prev_end=end_t`,
`prev_end=actual_end_t`). Initial non-usable machines and scenario-wide leave
workers are excluded from dispatch pools. Neither setup nor changeover exists
in the seeder.

## Historical-fix verification

| Area | Historical issue and existing fix | Production path | Regression evidence | Current verification | Status |
| --- | --- | --- | --- | --- | --- |
| Order query | INNER JOIN could delete orders without a purchase-order match; changed to LEFT JOIN with warning | `database/queries.py::fetch_active_orders_and_routings` | database/query tests | Current SQL is LEFT JOIN; live count has no unmatched order | VERIFIED AND ACTIVE |
| Validation split | Evaluation re-derived a split; old `val_order_ids.json` was replaced by a checkpoint-bound split manifest | train/evaluate | graph split and evaluation tests | Manifest verification is current; old file is not authoritative | SUPERSEDED BY LATER FIX |
| Workforce size | 15,066 interpretation replaced with company-confirmed 9,000 | seeder and validation gate | seed validation tests | Live Employees count is 9,000 | VERIFIED AND ACTIVE |
| Seeder failure | Seed exception was swallowed; original error now propagates | production seeder/staging | `test_seed_failure_propagation.py` | Current path raises | VERIFIED AND ACTIVE |
| Missing machine pool | Random/virtual fallback replaced by `ScheduleInfeasibleError`/infinite fitness | chromosome, fitness, scheduler | `test_ga_infeasibility.py` | No fabricated pool remains in audited path | VERIFIED AND ACTIVE |
| GNN checkpoint | Missing/corrupt checkpoint could leave random weights; now aborts and uses eval mode | decision engine/inference | `test_checkpoint_guard.py` | Checkpoint/preprocessing pair fails loudly | VERIFIED AND ACTIVE |
| Routing sequence | Numeric operation IDs were treated as precedence | operators, chromosome, fitness, heuristics, polish | adversarial 30/10/20 routing tests | Current fitness consumes explicit sequence map | VERIFIED AND ACTIVE |
| Feature width | 12 features fed a model constructed for 10 | schema, train, evaluate, inference | real feature-build/forward integration test | Construction sites use persisted width | VERIFIED AND ACTIVE |
| Weighted-delay inference | Weighted BCE raw sigmoid was consumed as a probability in scheduling while evaluation applied the prior correction | preprocessing v2 persists train pos_weight; all maintained scheduling paths call one analytic correction helper | test_delay_probability_inference_contract.py, feature-schema and checkpoint tests | Current checkpoint, manifest and preprocessing hashes match; scheduling and evaluation now use the same corrected probability | NEW ISSUE FIXED AND ACTIVE |
| Label validity | Missing outcomes became negatives and missing completion got a foreign fallback | labels, training, evaluation | label and metric tests | Per-head masks are present | VERIFIED AND ACTIVE |
| Priority leakage | Population-wide percentile target was fit before split | labels/train/evaluate | priority-label tests | Frozen train cut-points are persisted | VERIFIED AND ACTIVE |
| Factory ownership | Algorithms independently derived factories | ownership table/shared reader | ownership and disjunctive-edge tests | Live ownership covers all orders; graph and GA consume it | VERIFIED AND ACTIVE |
| Factory-scoped workers | Company-wide fallback borrowed workers across factories | exporter | scheduling audit tests | Sourcing stops at owned factory | VERIFIED AND ACTIVE |
| Reservation identity | int/string employee keys bypassed committed reservations | query/export/rolling horizon | real-caller reservation tests | Keys normalize to string and busy load uses same namespace | VERIFIED AND ACTIVE |
| Fixed vs replaceable work | Future movable work was reserved while also rescheduled | reservation classifier and rolling horizon | causal staffing replay and reservation tests | Default retains fixed work only | VERIFIED AND ACTIVE |
| Machine commitments | Rolling windows started with idle machines; the first fix carried only busy-until in rolling paths | direct decision engine, query and both rolling runners | `test_reservation_classification.py` plus focused 61-test reservation/rolling run | Direct rescheduling now loads machine-only rows and carries both busy-until and last-order identity; rolling paths use the same loader | PARTIAL HISTORICAL FIX COMPLETED IN CURRENT AUDIT |
| Execution segmentation | Continuous coverage created a false ~60% shortage; same-worker pause/resume now enters fitness/export | worker allocator, fitness, export, rolling state | phenotype, carry and validator tests | Segments drive elapsed completion, machine holding, successors and reservations | VERIFIED AND ACTIVE; RESEED PENDING |
| Schedule calendar | Shared primitive existed but fitness/export diverged | work_calendar, worker allocator, fitness and export | shift, crew and parity tests | Seeder/evaluator/export share productive segments | VERIFIED AND ACTIVE; RESEED PENDING |
| Machine usability | Aggregate reserve capacity accounted for down machines, while identity pools still included them | machine reserve/chromosome | updated live-capacity test | Audit fix now filters non-usable identities and aligns denominator | PARTIAL HISTORICAL FIX COMPLETED IN CURRENT AUDIT |
| Planned/actual simulation | Earlier clock separation left resource identity coupled to realized state | two-phase seeder | invariant tests and canonical proof | Plan hash is unchanged across actual seeds; actual hashes differ | FIXED AND PRE-RESEED PROVEN |
| Run/snapshot identity | Artifacts and cloned calibration could drift | promotion, snapshot, run artifacts | real MySQL promotion/recovery tests | Production implementation exists; full current-path re-verification pending | VERIFIED BUT NEEDS CURRENT END-TO-END RECHECK |
| Identifier/join integrity | Earlier joins duplicated workers, dropped orders, or guessed type mappings | production query contracts and canonical taxonomy | database, taxonomy, ownership and routing tests | Full live probe: zero missing/duplicate active operations, zero resource/factory/skill mismatches | VERIFIED AND ACTIVE |

## Confirmed current-audit defects

### IA-001: planned resource choices depended on realized actual clocks

The historical replay proved incomplete separation: 83.34% of machine and
83.24% of employee identities differed from planned-clock selection. The
approved production correction is two phase. build_frozen_planned_schedule
uses planned-only machine, employee and predecessor state. Only afterward
does simulate_actual_from_frozen_plan apply actual clocks and outcome draws
to frozen assignments.

A six-row production-function proof held planning seed 42 and all planning
inputs fixed while changing actual seed 1 to 999. Every planned field matched
and both canonical plan hashes were
d43adafb50068821c4bb7783ffbcae223316b20ece9f22c1b57d26bf2c1cd12e;
actual hashes differed. Evidence:
outputs/integration_audit/seed_plan_actual_invariance_proof.json.

Classification: **incomplete historical fix corrected and pre-reseed
proven**. Full MySQL staging remains required. The live data, checkpoint and
scheduler results become superseded after promotion/retraining.

### IA-002: same-worker segmented execution is authoritative

OS/LA/MC now decode deterministically to machines and a fixed qualified crew.
Setup is machine-only; work advances only on the complete crew's common
availability. Worker reservations contain productive segments, machines stay
occupied through pauses, and final elapsed completion releases successors
and drives tardiness and fitness. Export serializes that phenotype and rolling
windows carry its segments under normalized string employee IDs.

The 600-minute proof ends Tuesday 08:00 in evaluator and export; its successor
starts then and the order completes Tuesday 09:00 with two hours tardiness.
Hashes match and the independent validator reports zero errors. A committed
paused row loads only its future Tuesday 06:00-08:00 segment; the next job's
productive work waits until 08:00.

Evidence:
- outputs/integration_audit/authoritative_phenotype_parity.json
- outputs/integration_audit/worker_segment_carry_forward.json
- tests/test_authoritative_worker_phenotype.py
- tests/test_worker_pause_resume_contract.py

Classification: **partial historical fix completed in production and
pre-reseed proven**. Historical optimizer/delivery schedules used the former
worker-blind score and are superseded for the new formulation.

### IA-003: down machines remained selectable by identity

Aggregate capacity removed Breakdown/Under_Maintenance assets, but per-line
machine pools did not. Current live data contains 188 such non-reserved down
assets.

Corrected in production by filtering identity pools with
`NON_USABLE_STATUSES` and deriving the fitness denominator from the same
usable ID set. Focused tests: 11 passed.

### IA-004: final pipeline could not print current evaluation contract

`evaluate_gnn()` returns structured metrics as well as scalars, while
`run_final_pipeline.py` formatted every value with `:.4f`. The current
pipeline would crash after evaluation.

Corrected with type-safe rendering and a regression test. Focused test:
1 passed.

### IA-005: fixed machine state was incomplete outside rolling windows

The historical commitment fix existed in rolling-horizon code, but direct
decision-engine rescheduling did not load it. The query also derived machine
state through employee-populated rows and retained only busy-until, losing both
machine-only commitments and the last committed order needed for first-job
changeover.

Corrected by one machine-specific reservation reader that returns
`(busy_until, last_order)` and is consumed by direct GA scheduling and both
rolling-horizon arms. Focused reservation/rolling tests: 61 passed.

Classification: **partial historical fix completed in the current audit**.
Older direct schedules ignored commitments. Older rolling comparisons retained
busy-until but could omit the first 25-minute order-change setup; exact KPI
figures are qualified until the corrected authoritative run.

### IA-006: simulated employee-absence effect is unreachable

The seeder samples scenario-wide leave and excludes those employees from every
eligible dispatch pool. It then sets `on_leave=False` for every assigned lot,
although `sample_execution_variance_multiplier` still documents and implements
a leave-based mean shift.

Live measurement of the column consumed by dispatch-context construction:
`employee_on_leave_at_dispatch=1` on 0/176,187 rows (0 nulls). Queue and prior
breakdown context remain populated (132,697 and 104,483 positive rows,
respectively).

Classification: **historical causal feature made unreachable by a later
scenario-wide exclusion policy**. This does not create a runtime crash, but the
current dataset/checkpoint cannot support claims that the delay head learned
absence effects. Changing it changes the synthetic DGP and requires a reseed,
retrain and explicit design decision.

### IA-007: weighted-delay outputs were misinterpreted at scheduling inference

The delay head is trained with BCEWithLogitsLoss(pos_weight=w). Its raw
sigmoid is the weighted posterior q, not the deployment-prior probability
p. Evaluation already used logit(p) = logit(q) - log(w), but every
maintained scheduling entry point used the raw sigmoid.

The current train-derived w=2.3854505971769817 changes the full-graph output
from raw mean 0.4992304 to corrected mean 0.2995640. The raw range
0.3177647-0.7503850 becomes 0.1633581-0.5575634. At the export threshold 0.75,
one operation crossed under the raw value and none crosses after correction.

Corrected in production by persisting delay_pos_weight in preprocessing
artifact version 2 and applying one validated analytic helper in the decision
engine, final pipeline, authoritative comparison and maintained audit runners.
The existing checkpoint sidecar was migrated only after its checkpoint hash
matched both manifest and prior sidecar identity. Focused verification: 19
tests passed.

Evidence: outputs/integration_audit/gnn_output_wiring.json.

Classification: **new train-inference mismatch, fixed and active**. Historical
GNN discrimination metrics remain valid because evaluation was already
corrected. Exact GA rankings remain valid under the current objective because
the delay term is candidate-invariant and risk buffering is zero; exported
high-risk flags/reroutes require qualification.

## Current GNN output wiring

The real graph/checkpoint forward pass verified 3,000 machine nodes, 9,000
worker nodes and 7,809 active operation nodes with 12 operation features. The
checkpoint SHA, preprocessing ownership and split-manifest graph fingerprint
all match. The split contains 6,236 training and 1,573 validation operations.

Controlled perturbations establish the operational roles:

- **Delay:** trained and now probability-corrected, but its objective penalty
  is an additive candidate-invariant mean. With risk buffering configured to
  zero it cannot change GA ranking; it can affect the post-optimization export
  threshold in configurations where corrected probability exceeds 0.75.
- **Completion:** trained and evaluated, but absent from production
  gnn_preds; changing it from 1 to 1e9 leaves every scorer detail unchanged.
  It is operationally unused.
- **Priority:** trained, passed into scheduling and candidate-dependent.
  Swapping two operation priorities reverses the preferred order in a hand
  scorer probe.

These are measured integration states, not claims about whether the heads
should exist. Connecting or removing a head is an architecture decision and is
deferred until foundational semantics freeze.

## Chromosome, decoder and objective verification

A read-only probe constructed an EDF chromosome for 12 current active orders
(112 operations), using the live production queries, ownership, eligibility,
current calibration and 54 machine commitments visible at the probe timestamp.

- OS, LA and MC all reached the physical decoder.
- An OS perturbation changed fitness by 0.443445 and 27 decoded operation
  records after routing and LA/MC repair.
- A one-operation LA perturbation moved that operation from line 34 to line 36,
  selected only line-36 assets, and changed fitness by 0.013272.
- A one-operation MC perturbation changed the selected asset count from 11 to
  1 and changed fitness by 0.015917.
- Every selected machine belonged to the line encoded by the post-repair LA
  gene; chromosomes remained complete permutations with length 3N.

This verifies that genetic diversity is not being erased by a decoder that
ignores all three genes. It also confirms that an OS change can have broad
phenotypic effect because LA/MC are positional and are revalidated after OS
repair. That behavior is part of the current representation and remains an
input to the later operator study.

Evidence: outputs/integration_audit/chromosome_phenotype.json.

The saved authoritative objective was independently reconstructed for all 20
arm-window rows. The six emitted weighted components sum to the minimized
fitness with maximum absolute residual 3.64e-12. The delay term has exactly
zero range across the five arms within every window. Candidate control is:

| Term | Candidate control |
| --- | --- |
| Makespan | OS/LA/MC through decoded machine timing |
| Delay | None; one fixed prediction is averaged once per operation |
| Priority | OS position only |
| Load balance | LA/MC directly; OS indirectly through resource timing |
| Lateness/service | OS/LA/MC through order completion |
| Idle labour | MC and decoded makespan/resource use |

Evidence: outputs/integration_audit/objective_reconstruction.json. Exact
saved values remain qualified because that report predates IA-003 and IA-007;
the arithmetic and candidate-dependence classifications remain valid.

The authoritative phenotype parity proof supersedes the earlier
machine-only probe. Evaluator and exporter now share a semantic phenotype hash
over identities, resources, worker segments and elapsed timing. Export no
longer performs the dormant high-risk post-optimization line override; LA is
authoritative.

Evidence: outputs/integration_audit/authoritative_phenotype_parity.json.

### IA-008: post-optimization high-risk line override removed

The old branch could export a different line and machine from the scored
chromosome. The one-phenotype design removes it rather than duplicating it in
export. Its threshold remains only as historical audit metadata.

Classification: **bypassed historical fix completed and hash-verified**.
Current checkpoint predictions did not trigger the old branch.

### IA-009: overlapping lookahead is scored differently from committed state

For supported lookahead_size > commit_size, the GA scores the full lookahead
with provisional operations consuming machines, then
extract_committed_subschedule removes those operations and the rolling
horizon re-decodes only committed orders. A hand instance with provisional B
before committed A on one machine scored A at minute 45 (10 minutes B, 25
changeover, 10 minutes A), but the state actually carried after extraction put
A at minute 10.

Evidence: outputs/integration_audit/rolling_lookahead_commit_parity.json.

The authoritative default and release-gated modes use L=C, so existing
authoritative comparisons are unaffected. The wider-lookahead mode explicitly
uses the full lookahead as a surrogate policy score, then commits only the
current chunk. This is documented behavior, but the committed-state
reconstruction does not preserve provisional delays from the scored plan.
Classification: **qualified optional mode, disabled by default**. Before it is
used for authoritative experiments, choose and freeze a commitment policy:
preserve frozen placements from the full plan, or score the exact extracted
committed schedule.

## Declared model-contract divergences requiring decisions

1. Scheduler setup/changeover is absent from simulator completion labels.
2. Delay is candidate-invariant; completion is unused; priority is consumed.
3. run_final_pipeline directly reseeds live data and must remain development
   only or adopt staging/promotion.
4. api/main.py is empty; Streamlit is an internal dashboard, not a deployable
   API lifecycle.

## Result validity (provisional)

| Result | Current classification | Reason |
| --- | --- | --- |
| Current checkpoint GNN evaluation | SUPERSEDED AFTER NEXT RESEED | Artifact chain is valid for old DGP; new data requires retraining |
| Staffing four-condition replay | VALID for attribution of the tested eligibility policies | Correctly disproves physical-shortage interpretation; does not prove exported segmented reservations are conflict-free |
| Five-seed GA generation/polish audit | VALID HISTORICAL / SUPERSEDED FOR NEW FORMULATION | Sound old evidence; rerun after reseed/retrain |
| Diversity audit | VALID HISTORICAL / SUPERSEDED FOR NEW FORMULATION | Sound historical instrumentation; phenotype/data change |
| Offspring-quality audit | VALID HISTORICAL / SUPERSEDED FOR NEW FORMULATION | Exact old reproduction remains useful; rerun before redesign |
| Old ALNS experiments | PARTIALLY VALID / likely SUPERSEDED | Exact commitment, calendar, pool and objective versions still need artifact-level classification |
| Historical 15,066-worker comparisons | SUPERSEDED | Wrong workforce interpretation |
| Old 60% physical-worker-shortage conclusion | INVALIDATED | Causal segmentation replay reduced measured shortage to zero |
| 15x recoverable-headroom claim | INVALIDATED | Workload bound is a loose relaxation; stronger critical-path bound is required |

## Freeze progress

Safe to freeze now:

- routing order derives from routing sequence;
- company size is 3,000 machines / 9,000 employees;
- customer deadline is `delivery_deadline`;
- one persisted order-factory ownership source;
- no cross-factory machine/worker borrowing without an explicit transfer model;
- no virtual machine capacity;
- validity-masked GNN heads and train-derived priority cut-points;
- checkpoint/preprocessing/split identity pairing;
- fixed-vs-replaceable reservation classification and string-normalized worker IDs;
- non-usable machine identities are excluded from scheduling pools.
- current identifier normalization and joins for orders, operations, machines,
  workers, ownership, capability and qualification.
- machine commitment identity, busy-until and last-order transfer through all
  current production scheduler entry points.
- SAM/quantity/efficiency/calibration percentage-to-minute conversions.
- two-phase plan-then-realize generator semantics (activation pending);
- same-worker segmented phenotype, reservations, completion and export parity;
- one worker-aware comparison context for GA and all baselines;
- independent segmented-schedule validation.

Not frozen:

- whether absence is an exclusion-only scenario property or a learnable
  dispatch disruption in the synthetic DGP;
- scheduler setup/changeover relationship to generated training data;
- full staged activation/retrain/rerun and application/API lifecycle.

## Independent validator and application/disruption integration

A production-independent validator now exists in validation/schedule_validator.py. It consumes plain exported records and does not call the GA decoder, machine selector, operator allocator, or shared calendar walker. Fifteen deterministic microinstances cover operation coverage, duplicates, precedence, release dates, ownership, machine capability/factory/line/count/overlap/commitments, worker skill/factory/count/overlap/commitments, explicit productive segments, calendar callbacks, setup, productive minutes, completion and tardiness.

The validator requires explicit worker segment evidence. Authoritative
export now supplies it. The parity instance independently reconstructs both
operations, Tuesday 09:00 completion and two hours tardiness with zero errors.
The carry-forward instance also validates against a committed resumed worker
segment without overlap. It remains independent of the decoder and calendar
walker.

Evidence: outputs/integration_audit/authoritative_phenotype_parity.json and
outputs/integration_audit/worker_segment_carry_forward.json.

### IA-010: disruption alternate-machine selection bypassed ownership

The GA and graph correctly consumed persisted order ownership, but the machine-breakdown assessor searched every active compatible machine company-wide. On the exact consuming relation Machinery_Assets.factory_id versus Order_Factory_Ownership.factory_id, all 147,194 occupying schedule rows had at least one external active candidate exposed. The old query exposed 69,186,437 external candidate pairs and 4,969,679 external standby pairs; every row also had a local candidate. The live stored schedule had zero current factory mismatches, so this was a reachable future local-patch defect rather than evidence of current corruption.

The production assessor now loads the authoritative ownership snapshot, searches only active compatible machines in the owned factory, carries the candidate line with the machine, and atomically patches asset, line and interval together. Missing/unreadable ownership escalates to full rescheduling. Focused verification: 28 tests passed.

Evidence: outputs/integration_audit/disruption_factory_scope.json.

Classification: **historical factory-ownership fix active in GA/graph but bypassed in disruption path; completed and focused-tested**. GA/baseline benchmark results are unaffected. Historical disruption/local-patch claims require qualification. This factory-scoping behavior is now freezeable.

### IA-011: breakdown persistence failure could be hidden

The event handler previously logged and returned after a failed machine state update, and did not require exactly one affected row. Its dashboard caller could then display success although the breakdown never reached the database or impact assessor.

The production handler now rolls back and raises when SQL fails or the update does not affect exactly one asset. Impact assessment runs only after a successful commit. The monitor initially bypassed that guard by launching a full reschedule against unchanged state; a dedicated BreakdownPersistenceError now blocks that stale-state retry while retaining the fallback for post-persistence failures. Five focused tests cover SQL failure, zero-row update, the successful one-row path, and both monitor branches.

Classification: **new application integration issue with a downstream bypass; fixed and focused-tested**. Scheduling benchmark results are unaffected.

### IA-012: production application lifecycle remains a prototype boundary

api/main.py is empty. The executable UI is explicitly a Streamlit internal test dashboard that invokes Python components directly. It has no persisted proposal acceptance/rejection lifecycle, transactional activation/rollback of a manager-approved schedule, or API/.NET contract in this repository. Output display reads files by modification time without enforcing their run identity. Manager priority overrides are process/session memory.

The material-shortage handler logs the event and reruns scheduling without persisting a shortage, delaying the order, changing release/readiness, or altering any scheduler input. It must be described as disconnected prototype behavior, not operational disruption handling.

Classification: **industry-readiness architecture gap; not changed during the foundational audit**. It does not invalidate scheduler research metrics, but blocks a production-deployment claim and Integration Freeze for the full application lifecycle.

## Baseline and deadline-metric contracts

The current rolling baseline constructs the same GAScheduler context as GA and consumes the same ownership, eligibility, machine pools, calendar predicate, objective and committed machine state. Each baseline owns an independent carried state. Optional export uses the same worker allocator and starts from the same window-start machine/reservation state. When export is disabled, baseline staffability is explicitly UNMEASURED. Focused parity/commitment verification: 40 tests passed.

The objective intentionally measures tardiness against effective_deadline = max(customer deadline offset, 0), excluding lateness already inherited at the scheduling origin. On the authoritative GA output, 238/911 deadlines were already past. The late count is 304 under both definitions on this run, but mean tardiness among late orders is 65.52 hours against the effective deadline and 453.99 hours against the actual customer commitment; all-order means are 21.86 and 151.49 hours. The optimizer was not changed. User-facing final/evaluation/recommendation wording now explicitly says schedule-added/effective-deadline tardiness. Sixteen focused tests passed.

Evidence: outputs/integration_audit/deadline_metric_contract.json.

### IA-013: effective-deadline metrics were displayed as unqualified tardiness

Classification: **internal semantic is documented and unchanged; ambiguous user-facing labels fixed and focused-tested**. Historical numeric results remain valid as effective-deadline results. They are not customer-tardiness magnitudes and must not be presented that way.

## Historical optimizer-result classification update

- Current GA-versus-baseline comparison: **VALID WITH QUALIFICATION** for the exact frozen formulation and shared scorer. Tardiness is schedule-added/effective-deadline tardiness.
- Corrected workload/critical-path bounds: **VALID WITH QUALIFICATION** as documented relaxations; neither proves attainable improvement.
- ALNS snapshot 20260825 experiments: **SUPERSEDED FOR THE CURRENT FORMULATION**. They were internally frozen historical experiments, but predate the 20260911 dataset and later ownership/calendar/commitment/pool/worker corrections. They should be rerun only after Integration Freeze.

## Authoritative arm parity under the new decoder

run_authoritative_comparison now loads one machine and worker commitment
snapshot and supplies independent copies to GA, FIFO, EDF, CriticalRatio and
RandomRepaired. Every arm passes the same operator index, ownership, decoder,
objective and export path. Stale text claiming baseline staffing was
unmeasured was removed.

Evidence: outputs/integration_audit/authoritative_arm_parity.json and
tests/test_authoritative_comparison_worker_parity.py.

## Exact evaluator performance

On the current 300-order/2,580-operation window, optimized exact worker-aware
evaluation is 1.99-2.04 seconds warm versus 0.121-0.127 seconds worker-blind
(16.23x). The initial exact implementation was 64.8x slower. Calendar crew
intersection now dominates; whole-workforce reservation copying was removed
without changing the phenotype hash.

At about 2.02 seconds/evaluation and 2,550 evaluations per window, scaling by
7,809 operations gives roughly 4.4 hours for one GA seed and 22 hours for five
seeds before overhead. This is acceptable for offline research, not
interactive production rescheduling. No surrogate was introduced.

Evidence: outputs/integration_audit/worker_aware_evaluator_benchmark.json.

## Audit checkpoint: 2026-09-14

The pre-reseed complete suite passes: **862 passed, 0 failed, 423 warnings in 373.67 seconds**. The prior 814 and 847 counts remain historical; the increase comes from authoritative worker-phenotype, arm-parity, rolling segment-carry and seeder-invariance regressions.

The first full attempt exposed one stale release-gate test. It expected an order with every schedulable machine down to remain in a zero-capacity pool until starvation could admit it. That contradicts the already-frozen structured infeasibility guard and could only defer an impossible schedule. The production code was unchanged; the test now requires ScheduleInfeasibleError with missing_machine_capacity. The focused rerun and complete rerun pass.

Evidence: outputs/integration_audit/pre_reseed_full_suite.xml. The affected focused gate also passed 174 tests in 141.81 seconds.

## Remaining gates before Integration Freeze

The two foundational semantic decisions are implemented and focused-tested.
Integration Freeze still requires: fresh staged seed, full validation, atomic
promotion, graph rebuild, GNN retraining/evaluation, identity verification,
worker-aware authoritative comparisons, independent validation of exported
schedules, relevant GA diagnostic reruns and a final complete suite.

The small DGP proof exercises the real planning/simulation functions but not
MySQL insertion. Full staging is therefore the next data gate.

Application deployment remains outside scheduler freeze: the dashboard is
internal, api/main.py is empty, and no manager proposal acceptance/rollback
API exists.

## Authoritative staged rebuild and DGP delay gate (2026-09-14)

The corrected two-phase generator completed two full MySQL staging runs with planning seed 42 and actual seeds 42 and 999. Across all 176,187 schedule rows the normalized planned projection was identical (`d423143a8926e0d2e671456f8cded8c1afa8bf9f0b3fed205ba9ea608135ad71`), including ownership, resource identities, routing order, planned timestamps and planned segments; actual outcomes differed. This upgrades IA-001 from focused proof to **VERIFIED AND ACTIVE on the full staging lifecycle**.

Staging `garment_fyp_db_seed_v2_20260914_142905` (dataset `b72b7231-c2bf-4e82-b952-5609fdf853ec`) passed all 67 seed checks and was atomically promoted under journal `94d02cf41c924991a588`. Clone identity intentionally created live dataset UUID `9fa2915b-5f28-4463-8e4f-d21b5b57dc8d`; the promotion journal and calibration ancestry retain the staging identity. Live and rollback schema `promotion_backup_94d02cf41c924991a588` independently pass the same validation gate.

### IA-014: corrected DGP operation-delay prevalence

The exact production label population is 7,491 positive / 720 negative among 8,211 scheduling-active operations: **91.2313%**. An independently generated actual-seed-999 dataset gives 89.5787%, so the result is not a seed-42 anomaly. Prevalence is high in every major operation type (90.59%-92.01%) and varies with factory/time congestion; tiny factories 4/6 are much lower (52.78%/57.58%), while September-November dense cohorts are 98.71%-99.45% and later sparse cohorts fall below 50%.

No double-counting or accidental amplification was found. All 176,187 lot rows close the identity `actual_end-planned_end = start shift + productive-duration delta + pause delta` with maximum residual `1.36e-11` minutes. Exact phase-B replay using the unrounded production duration formula matches all stored timestamps within the shared calendar's one-second quantization. Readiness uses `max(machine, employee, predecessor, planned floor)`, not addition; queue is measured rather than added a second time. Removing the planned-start floor changes only 10 labels (91.2313% to 91.1095%). Start displacement accounts for 99.9383% of positive lateness components, and 85.6628% of positive operation labels are primarily inherited through predecessor readiness. Dynamic repair is inferred on 5,357/176,187 rows; employee leave contributes zero rows; calendar-pause deltas are small relative to propagated start displacement.

Classification: **ACCEPTED CAUSAL SYNTHETIC STRESS REGIME; CALIBRATION QUALIFICATION REQUIRED**. Duration scale is tied to company source data, but execution SD, breakdown process, prior-breakdown mean shift and queue-SD widening remain reasoned synthetic parameters rather than fitted factory outcome rates. The 91.23% value is defensible as this generated experiment's prevalence and must not be presented as measured real-factory prevalence. With no proven DGP defect, graph rebuild/retraining may proceed.

ML consequence: train split 6,058 positive / 510 negative, giving `pos_weight=0.0841862`. PyTorch weighted BCE supports weights below one and the existing analytic prior correction remains valid. The raw 0.5 threshold is the class-balanced-loss operating point; it is not a calibrated 50% event-probability threshold after weighting. Majority accuracy and no-skill AP baselines are both 91.2313%, so AP must be interpreted against that high baseline and ROC-AUC remains prevalence-independent.

Evidence: `outputs/integration_audit/dgp_delay_sanity_audit.json`, `dgp_delay_sanity_decision.json`, `dgp_planned_start_floor_counterfactual.json`, `dgp_replay_first_divergence.json`, `graph_profile_actual_seed999_staging.json`, and `full_staging_plan_independence.json`.
