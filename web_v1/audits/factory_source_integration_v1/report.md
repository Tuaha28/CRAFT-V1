# CRAFT source integration: offline safety gate (2026-10-13)

Status: **PARTIAL — live six-factory extraction blocked before connection**.

The current `.env` configures `DB_USER=root` and does not configure a separate
`LEGACY_DB_USER`. Root is not acceptable evidence of SELECT-only permissions.
No connection to `garment_fyp_db` was attempted in this audit. The updated
legacy reader now requires an explicit non-root legacy account and executes
`SHOW GRANTS` as its first application query (including mandatory roles). It rejects
write grants, role grants, grant option, and unrecognized grant syntax before
any extraction. Each factory read then uses REPEATABLE READ, a consistent
snapshot, and a read-only transaction; it also requires InnoDB source tables
before extraction because MySQL consistent snapshots are InnoDB-specific.
This mechanism is tested with a fake
connection; it has **not** been verified against live MySQL grants.

## Evidence used, not a new live database audit

- Sealed Factory 1 source capture SHA-256:
  `010541c698ca6c46aee096d322e1c197598b4d6fbd8ec2bfa6bfbca0b8974438`.
- Its normalized input hash is
  `88e86f939bddd5476c56280f83ced487bbfed11057cdc35ebe56deec7d51e3c0`;
  normalized snapshot hash is
  `5f58bb16132c5ecdb9a9fe24ebdee5b09cad53f156bbe6eabf48832411461e37`.
  These hash differently serialized objects and must not be equated.
  Both computed values match the previously saved normalized snapshot fields.
- The prior schema evidence lists 171 columns in 29 tables. The historical
  six-factory count evidence was recorded on 2026-10-07; it is not a current
  read or a scheduling-readiness assessment.
- Protected capture, historical schema, and historical count files had
  identical SHA-256 values before and after the offline audit. See
  `offline_readiness_report.json` for the exact hashes.

## Historical schema tables inspected from saved evidence

`_dataset_activation`, `batch_members`, `bill_of_materials`, `companies`,
`dataset_calibration`, `delivery_logistics`, `departments`, `employee_batches`,
`employees`, `factories`, `inventory_stock`, `job_shop_schedules`,
`machine_capabilities`, `machine_status_logs`, `machinery_assets`,
`operator_skill_matrix`, `optimization_metrics_log`,
`order_factory_ownership`, `product_variants`, `production_lines`,
`production_orders`, `products`, `purchase_orders`, `quality_logs`,
`raw_materials`, `routing_operations`, `suppliers`, `wip_tracking`,
`work_schedules`.

The existing source reader extracts orders/target quantity/delivery dates,
routing sequence and SMV, machines/capabilities/lines, workers/skills/efficiency,
shift schedules and leave, lots with execution segments, and WIP. The captured
Factory 1 source has 336 orders, 2,879 route-operation identities, 33 lines,
958 machines, and 3,103 workers. It does **not** extract attendance events,
machine-status history, material readiness, QC/rework, or any explicit
operation-level required crew size. Historical `routing_operations` has no
crew/worker-count column. Batch membership and one employee per historical
lot do not prove the required crew size of a future operation.

## Readiness, with no one-worker default

| Factory | Historical orders | Historical operations | Current source-admission result |
| --- | ---: | ---: | --- |
| 1 | 336 | 2,879 | Offline sealed capture: 171 completed; 82 partial blocked by unresolved continuation; 2,626 unstarted require crew-size evidence. Zero proven schedulable from this source contract. |
| 2 | 248 | 2,124 | UNKNOWN: no equivalent sealed full-source capture verified here. |
| 3 | 156 | 1,329 | UNKNOWN: no equivalent sealed full-source capture verified here. |
| 4 | 12 | 94 | UNKNOWN: no equivalent sealed full-source capture verified here. |
| 5 | 268 | 2,288 | UNKNOWN: no equivalent sealed full-source capture verified here. |
| 6 | 9 | 85 | UNKNOWN: no equivalent sealed full-source capture verified here. |

The historical total is 8,799 operation identities, not 8,799 admissible
scheduling operations. Factory 1's simulation overlay and previous TEST
schedules do not supply observed crew requirements or cure the 82 real partial
operations. The existing one-worker Factory 1 benchmark convention must not be
promoted to an authoritative factory rule.

## Implemented and verified

`legacy_repository.py` now fails closed on credentials and grants before data
access. `source_readiness.py` reuses the existing deterministic normalizer and
independent boundary validator, verifies a required pinned raw-capture hash,
and classifies each canonical operation without inferring crew size.
`run_offline_audit.py` generated the sanitized summary in this directory.

Focused tests: 39 passed, 29 subtests passed, zero failed. They cover grant
rejection, first-query ordering, no root/implicit account, capture hash
rejection, non-InnoDB refusal, Factory 1 identity/readiness accounting, and the existing boundary
contract. No scheduling or migration was run.

## Safety and next gate

Because no live legacy connection was made, **this audit process issued zero
queries and zero writes to the legacy database**. This does not establish
that other actors left the database unchanged. A live before/after comparison
and actual grant evidence are unavailable. The Django application database
was not opened or changed. Frozen captures and benchmarks were not edited.

Next: provision a dedicated SELECT-only MySQL account; independently inspect
its effective grants (including roles); run a read-only, consistent schema and
six-factory capture with a separately pinned identity; establish an
authoritative crew-size rule or field and validate it with factory personnel;
then assess resource and execution readiness before any factory-wide solve.
The 200-operation-cycle / <=25-operation-window controller must not admit
operations that fail these source requirements. Live activation remains off.
