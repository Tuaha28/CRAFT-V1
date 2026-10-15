# Dataset promotion and recovery

This is an OFFLINE maintenance command for MySQL 8.0.13+ or 8.4, with all
participating tables using InnoDB. The live database name stays unchanged.
No promotion has been executed against the project live database during implementation.

## Before running

Stop API servers, monitoring agents, ingestion, schedulers, training, and other
clients of staging/live. Stop administrative schema changes and concurrent gate
runs too. Keep them stopped until promotion or recovery is explicitly complete.
`--maintenance` acknowledges this requirement; it does NOT stop those processes
for you. Existing multi-statement clients/model caches cannot safely span a
dataset change merely because the SQL cutover is atomic. Restart them afterwards.
The named MySQL advisory lock serializes this command's promotions/recoveries.
Table locks protect copying and the final cutover from concurrent DML; they are
not a replacement for the maintenance requirement, particularly after a crash.

Run a recorded gate first:

```powershell
venv\Scripts\python.exe -m data_generation.validation_records STAGING_DATABASE
venv\Scripts\python.exe -m database.promote_dataset promote --staging STAGING_DATABASE --live garment_fyp_db --maintenance
```

The CLI has no population-count override: production validation requires 9,000
employees, 3,000 machines, and six factories. Tiny integration tests use an API
override restricted to names beginning `promotion_test_`.

## Preconditions and preparation

- Require the latest validation pointer and full record to agree on database,
  dataset version, validation ID and a passing verdict. Verify the result hash
  and current validator hash. An absent, failed, mismatched or stale record aborts.
- Reject views, triggers, routines, events, cross-schema FKs, unsupported engines,
  and generated/invisible columns. Do not silently omit database objects.
- Create a new prepared schema using SHOW CREATE TABLE, preserving FKs, unlike
  CREATE TABLE LIKE. Copy under table locks and verify every table's row count
  and order-independent content hash. Preserve source/staging unchanged.
- Assign the prepared schema its own calibration identity with
  calibration_for_clone(). Case-insensitive discovery is used throughout.
- Run the full shared recorded gate on that prepared dataset. Capture its content
  fingerprint before the gate and recheck under write locks before activation.
- Create a separate backup schema of live, including its complete FK structure.
  Under the final locks, copy/verify all live rows BEFORE modifying the live table
  set. Give backup calibration its own valid database identity and origin provenance.
  A legacy live dataset without calibration is backed up as legacy; it cannot be
  claimed to pass the modern gate. The old original tables are also retained.

## The atomic boundary

The prepared calibration is assigned the final LIVE identity while it is still
isolated. Then ONE `RENAME TABLE` statement moves ALL live tables into the retained
old schema and ALL prepared tables (including calibration, ownership and activation
marker) into live. No application configuration file changes are required.

MySQL atomic DDL makes this statement entirely committed or rolled back, including
when the server halts. This command refuses engines/server versions outside its
supported atomic-DDL contract. This is statement-level atomicity, not a transaction
around the entire multi-step preparation process.

References:
- https://dev.mysql.com/doc/refman/8.0/en/rename-table.html
- https://dev.mysql.com/doc/refman/8.0/en/atomic-ddl.html

The command subsequently runs the full live gate through validate_and_record().
A normal post-activation exception or failed gate triggers the inverse SINGLE
rename, restoring the retained original tables and original live calibration.
If connectivity is lost, it records recovery_required when possible and leaves
recovery to the command below; it never attempts a table-by-table restoration.

## Process death and recovery

Each run writes `outputs/promotions/PROMOTION_ID.json` using flushed, fsynced files
and atomic replacement. It records the server UUID, exact original table inventory, prior activation
identity (including explicit absence), and original content fingerprint before
preparing a replacement. Backup fingerprints, validations and cutover intent
are recorded BEFORE sending RENAME.
The new dataset carries `_dataset_activation.promotion_id` in the same atomic
rename. Therefore recovery can distinguish a committed cutover from an uncommitted
one even when the client never received an acknowledgement.

- Death during preparation: old live remains intact; partial prepared/backup
  schemas are isolated. No cleanup is automatic.
- Death during RENAME: MySQL leaves either the complete old or complete new table
  set. Recovery examines the activation marker, not the journal's last success flag.
- Death after RENAME and before validation: complete new data and matching live
  calibration are active, but validation is pending. KEEP APPLICATIONS STOPPED.
- A process crash can release table/advisory locks. These locks do not persistently
  disable application access; maintaining the maintenance window is essential.

Finish validation of the current activation:

```powershell
venv\Scripts\python.exe -m database.promote_dataset recover outputs/promotions/PROMOTION_ID.json --maintenance
```

Or explicitly restore the original dataset atomically:

```powershell
venv\Scripts\python.exe -m database.promote_dataset recover outputs/promotions/PROMOTION_ID.json --maintenance --rollback
```

Recovery checks the MySQL server UUID and activation marker. Before declaring
the old dataset active, it requires the exact old table inventory, prior activation
identity and content fingerprint. The same checks protect the retained original
tables during rollback, and the active new table inventory must match exactly.
Early failures, interrupted preparation and repeated recovery/rollback all resolve
to old_dataset_active only after these checks. Older/incomplete journals lacking
original-state evidence fail with a clear PromotionError: recovery does not invent
missing evidence or classify live from a preflight flag alone. An unrelated later activation is
refused, not overwritten. Recovery validation failure restores the old dataset;
a validation exception leaves the complete new dataset active for explicit recovery.
Do not restart clients on an exception. A terminal `validated`, `rolled_back`, or
verified `old_dataset_active` result resolves the maintenance operation.

## Retained evidence

Each promotion uses unique prepared, backup, retired and rejected schemas. Nothing
is dropped automatically. `backup` is a standalone usable clone, with corrected
calibration identity if the original had calibration. `retired` retains original
rows verbatim for atomic rollback; its calibration still names LIVE intentionally.
Do not point the runtime at retired: use backup for independent inspection.
After rollback, the rejected complete new dataset remains for investigation.

Full validations and latest pointers live under
`outputs/dataset_validations/DATABASE/`. Historical validation failures are never
overwritten. A failed new-live validation may remain the latest record after an
old legacy dataset is restored; the promotion journal and old fingerprint record
that restoration without manufacturing a passing modern gate for legacy data.

## Tests and limits

Disposable tests run the real extended gate, real clone/cutover/recovery SQL,
real calibration/ownership readers, and verify FKs still enforce local references.
This establishes backup usability at the database/gate/artifact-reader level; it
does not establish a full graph/GNN/GA runtime smoke test against that backup.
They inject post-activation failure, a lost acknowledgement, and actual subprocess
termination immediately before/after cutover. Additional tests cover a closed
MySQL connection, real failing live-gate verdicts in both promotion and recovery,
early preparation failures, repeated recovery, extra tables, incomplete journals
and unrelated later activation identities. They do not crash the MySQL server,
simulate disk/controller corruption, or benchmark production-sized promotion.
MySQL's documented atomic-DDL guarantee covers server-halt behavior; storage loss
and malicious concurrent administrator changes are outside that guarantee.
