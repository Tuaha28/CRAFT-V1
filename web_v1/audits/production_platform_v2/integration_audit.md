# CRAFT Production Platform V2 integration preflight

Status: **BLOCKED_AT_EXECUTION_STATE_CONTRACT**. Stage 1 found a concrete execution-state incompatibility. Stages 2–14 were not started. This report does not claim that the V2 platform is implemented or that a physical scheduling problem is infeasible.

## Verified connection and clock

- `garment_fyp_db`, MySQL 8.0.45, successfully queried inside a repeatable-read, consistent-snapshot, read-only transaction. `@@session.transaction_read_only = 1`.
- Credentials were consumed in memory from existing environment configuration; none were printed or copied into source.
- `Dataset_Calibration.payload.reference_time = 2026-09-10T00:00:00` is preserved.
- Dataset version: `9fa2915b-5f28-4463-8e4f-d21b5b57dc8d`.
- Timeline contract: `frozen-plan-then-actual-v1`. Data comes from the real project database, but includes a generated simulation trajectory. This is not evidence of today's shop-floor telemetry.
- `craft_web_db` does not exist on the connected server. No database was created and no migration was run.
- Database writes: **0**. No scheduler was executed.

The machine-readable report contains the current schema with verified columns and primary-key metadata. Logical operations are production-order/routing-operation instances, not raw routing-template rows.

| Factory | Historical orders | Operation instances | Operations with execution spanning reference time |
|---|---:|---:|---:|
| 1 | 336 | 2,879 | 17 |
| 2 | 248 | 2,124 | 7 |
| 3 | 156 | 1,329 | 3 |
| 4 | 12 | 94 | 1 |
| 5 | 268 | 2,288 | 20 |
| 6 | 9 | 85 | 3 |

Totals: 1,029 orders, 8,799 operation instances. There are 89 lot rows across 51 operations whose `actual_start_time <= reference_time < actual_end_time`: 41 rows marked `Running`, 48 marked `Delayed`.

## Blocking evidence: in-flight delayed work is classified as movable

`data_generation/seed_database_fixed.py`, in the actual-simulation status assignment, defines:

1. `actual_end <= reference_time` → `Done`.
2. `actual_start > reference_time` → `Scheduled`.
3. Otherwise → `Delayed` if causal delay exists, else `Running`.

Thus `Delayed` is not merely a future order-risk label in this dataset. Those rows have started and have not finished at the reference time.

`scratch/audit_factory1_schedulable_workload_v1.py` instead assigns `CURRENTLY_RUNNING` only if an operation has a `Running` row. An operation with `Delayed` rows and no `Running` row falls into `RELEASED_INCOMPLETE`. Resource boundary lists also filter exclusively on `Running`.

The following eight operations are both in flight by the source generation contract and present in the frozen `optimizable_operations.json`:

| Operation | Running-status lots | Delayed-status lots | Scheduled lots |
|---|---:|---:|---:|
| 40_902 | 0 | 4 | 2 |
| 40_903 | 0 | 5 | 14 |
| 40_905 | 0 | 4 | 38 |
| 212_2584 | 0 | 1 | 1 |
| 212_2586 | 0 | 2 | 6 |
| 411_2598 | 0 | 1 | 14 |
| 411_2599 | 0 | 1 | 15 |
| 979_952 | 0 | 1 | 21 |

Excluding order 790 does not resolve these eight conflicts. The existing frozen Factory-1 contract may remain research evidence, but cannot be silently reused as the live execution-state adapter for V2. This finding does not change or invalidate the separate frozen Factory-2 benchmark by assertion; that benchmark is a different input contract.

## Blocking evidence: lot state is not a proven whole-operation boundary

At the reference time, `212_2585` contains 60 lots: 56 Done, one Running, three Scheduled. Its running lot is on line 15, machine 1264, worker 2500; the lot's generated interval is `2026-09-08T23:27:05` to `2026-09-10T01:26:36`. The operation-wide maximum generated end is `2026-09-10T09:36:00`.

The Factory-1 audit computes `actual_end_evidence` as the maximum `actual_end_time` across all rows, including Scheduled lots. `scratch/run_factory1_craft_frozen_v1.py::_build_loaded` uses that aggregate as the whole-operation boundary end, while reserving only resources extracted from Running rows and marking the whole operation committed. This does not establish that all unfinished lots, their crews, and their machine commitments are represented correctly.

For `790_975`, the running lot ends at `2026-09-10T00:33:12`, while the whole operation contains one Running and ten Scheduled lots and has a generated maximum end of `2026-09-16T07:10:47`. There is no WIP row. The generated future segment endpoints exist, but they do not by themselves authorize treating the entire operation as completed, fixed until the running lot ends, or restarting it from zero.

The seeder writes full simulated `actual_start_time`, `actual_end_time`, and `actual_work_segments` for future lots too. These fields must be interpreted through its as-of contract; treating every non-null actual timestamp as observed production would leak future simulated outcomes.

49 of the 51 in-flight operations have WIP records whose recorded update timestamp is after the reference time. Seeder inspection shows WIP quantities are emitted for Done lots. The late timestamps therefore require provenance-aware interpretation; they do not alone prove quantities are wrong, and filtering them blindly would also be unsafe. WIP presence alone is not proof of an authoritative remaining productive duration.

## Current invocation and output mapping

The production scheduling entry point is:

`crafter.production.controller_v1.ProductionControllerV1.run_window(prepared, boundary=..., cache=..., boundary_facts=..., required_operation_keys=...)`.

Its inputs are built through `PlanningContextV1`, `build_planning_state`, `select_precedence_safe_window`, and `prepare_planning_schedule`. The loaded tuple contains `SchedulingProblem`, operator/calendar index, execution/incumbent rows, durations, operation details, route index, baseline phenotype, validation result, and source metadata. `OperationSpec` represents a whole routing operation; it has no field that directly preserves partial lot execution.

The canonical result contains:

- `candidate.acceptance.stages[*].status`: CP stage statuses; do not substitute the controller status.
- `candidate.acceptance.selected_schedule`: selected assignments, resource configuration, start/end seconds, productive worker segments.
- `candidate.acceptance.materialization.mismatch_count`.
- `candidate.acceptance.replay.status` and `validator.status`.
- `trace.controller_status`, recovery rounds, and balanced rescue evidence where supplied.
- Independent `validate_canonical_planning_candidate(...)`: `scope_status`, `replay_status`, `physical_status`, and `accepted`.

Any future normalizer must fail closed for missing evidence, require a feasible CP result and zero materialization mismatches, and require Replay PASS, Physical VALID and Scope SCOPE_VALID. A local candidate is not automatically a fully assembled active factory schedule. Promotion must validate the merged schedule against its unchanged boundary and base version.

`crafter.disruptions.benchmark._load_state()` is not a live database extractor. It loads a frozen historical CR phenotype and duration artifacts, reconstructs metadata, and asserts an expected baseline hash. Reusing it as a generic V2 database loader would introduce the wrong workload/source.

The Factory-1 runner is also not a generic snapshot adapter: it fixes factory, reference time, exclusion, artifact paths and expected counts, and re-queries resource data. It cannot be launched unchanged as the per-request immutable worker bridge. No such runner was invoked during this audit.

## Verified source map and identifiers

| Concern | Authoritative source |
|---|---|
| Factory membership | `order_factory_ownership.production_order_id/factory_id` |
| Orders and releases | `production_orders.production_order_id`, `scheduled_start_date`, `product_id`, `target_quantity` |
| Buyer and due | `purchase_orders.po_id/client_name/delivery_deadline` via `production_orders.po_id` |
| Routing | `routing_operations.operation_id/product_id/operation_sequence_number` |
| Operation identity | composite `production_order_id_operation_id` |
| Individual execution row | `job_shop_schedules.schedule_item_id`, plus operation and `lot_index` |
| Machine | `machinery_assets.asset_id`, capability relation in `machine_capabilities` |
| Worker | `employees.employee_id`, skills in `operator_skill_matrix` |
| Calendar | `work_schedules`, `employee_batches`, `batch_members` |
| Line/factory | `production_lines` joined through `departments` |
| WIP | `wip_tracking.production_order_id/operation_id/pieces_completed` |
| Materials | `raw_materials`, `inventory_stock`, `bill_of_materials`, `suppliers` |
| Quality/delivery | `quality_logs`, `delivery_logistics` |

Legacy schedule rows must remain read-only. A future web active schedule belongs in `craft_web_db`; no writeback into `job_shop_schedules` is required. Existing `database/db.py` creates a general-purpose SQLAlchemy engine and a persistence session helper. A new read-only repository should not import that helper into the Django web process.

## Existing web application audit

The current application remains V1 demo code. Django uses process-local fixture lists and an in-memory SQLite setting. There are no persistent application models, authentication/session middleware, approval transactions, run queue, or real scheduler adapter. No current UI badge is evidence of actual CRAFT execution.

The existing Gantt marker uses `Date.now()` and a ratio of visible container width independently from the library's scroll/zoom coordinates. The fixture clock, static shift label and current browser clock are distinct. The schedule header also hardcodes active/version/validation claims. These are confirmed V2 repair items; they have not been changed in this audit.

## Resume plan after the execution contract is resolved

1. Establish a tested snapshot adapter contract for in-flight `Running` **and** `Delayed` lots, partial completion, future simulated endpoints, complete remaining quantities, and all boundary resource segments. Do not silently change frozen artifacts.
2. Decide whether the adapter can represent residual work and immutable boundaries using existing CRAFT whole-operation semantics. If a core change is necessary, request separate authorization before editing it.
3. Use a reviewed bounded scenario whose current-state mapping passes that contract. Reject unsupported input states explicitly rather than dropping operations.
4. Implement the requested two-database application, subprocess worker, versioned candidates, independent evidence gates, manager-only transactional promotion, and Gantt comparison in the authorized order.

Proposed Django-owned models (not created): Customer, Supplier, Material, StockBalance/Reservation, ProcurementOrder/Line, OrderExtension, ProductionReport, MaintenanceTicket, QualityInspection, WIPRecord, Shipment, SchedulingEvent, CraftRun, ImmutableSnapshot, ScheduleScope, ScheduleVersion, ScheduleOperation, ScheduleValidation, SchedulePromotion, AuditEvent, Notification, Role/permission assignments, and controlled application settings.

The intended architecture remains React → authenticated Django application services → read-only legacy repository / Django-owned application database → persistent queue → worker process → isolated CRAFT subprocess → validation evidence → candidate → manager approval → atomic promotion. No substitute scheduler or scheduling semantics have been introduced.

## Reproduce this audit

From the repository root:

```powershell
venv\Scripts\python.exe web_v1/audits/production_platform_v2/audit.py
```

The script reads schema/execution evidence in one read-only transaction, writes reports only beside itself, and verifies inspected source files were unchanged. `artifact_hash_manifest.json` and `hash_verification.json` cover this audit's artifacts. No credentials are included. No V2 application or integration tests are claimed, because implementation stopped at the required contract gate.
