# Verified reader setup and six-factory capture

Local date: 2026-10-13 (Asia/Karachi). UTC artifacts are dated 2026-10-12.

## Account setup: VERIFIED

The user explicitly authorized creation of a new `craft_reader` account and
its SELECT grant. The administrator connection found no existing account of
that name and no mandatory roles. It executed only account metadata checks,
`CREATE USER` for `craft_reader@localhost`, and `GRANT SELECT ON
garment_fyp_db.*` for that new account. No existing account was modified.
No factory data was queried with the administrator connection.

The generated password was saved directly to the ignored, untracked local
`.env`, without printing it. A separate connection authenticated as
`craft_reader@localhost`; `SHOW GRANTS` returned only USAGE on *.* and SELECT
on garment_fyp_db.*, and CURRENT_ROLE() returned NONE. Passwords and
authentication hashes are absent from the evidence files.

Provisioning evidence: `../reader_setup_20261012T223329658583Z.json`.
Reader schema/privilege evidence: `reader_and_schema.json`.

## Capture: complete; production scheduling admission: blocked

| Factory | Orders | Operations | Completed | Blocked partial | Crew evidence required |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 336 | 2879 | 171 | 82 | 2626 |
| 2 | 248 | 2124 | 170 | 49 | 1905 |
| 3 | 156 | 1329 | 80 | 45 | 1204 |
| 4 | 12 | 94 | 28 | 3 | 63 |
| 5 | 268 | 2288 | 256 | 89 | 1943 |
| 6 | 9 | 85 | 19 | 10 | 56 |
| Total | 1029 | 8799 | 724 | 278 | 7797 |

Resources: 9,000 workers and 3,000 machines. Extraction plus offline
normalization/admission auditing took 184.559 seconds. Per-factory extraction
and validation times are in `report.json`.

Each factory was captured through the existing LegacyExecutionRepository
using a consistent, read-only REPEATABLE READ transaction after verifying
grants, database identity, read-only mode and InnoDB storage. The six factories
were captured in separate transactions; this is not a single globally atomic
snapshot. Dataset revision and operation counts agree across the captures.

Dataset revision: `9fa2915b-5f28-4463-8e4f-d21b5b57dc8d`.
Reference time: `2026-09-10T00:00:00`.
Timeline contract: `frozen-plan-then-actual-v1`.
The captured generated dataset is TEST evidence, not certification of real
factory execution or actual attendance.

Available in the captures: order quantities/deadlines/release dates, routing
sequence and SAM, lot identities/quantities and execution segments, WIP,
factory/line ownership, machine capabilities/current status, worker skills
and efficiency, work schedules/leave, and batch membership/rotation.

Missing: no explicit required-worker/crew-size field was found in the live
171-column, 29-table schema. There is no separate attendance-event table.
Machine-status logs, material and QC tables exist but are not included by the
current source adapter. Future planned rows are not independently approved
resource commitments. The 278 unresolved partial operations remain blocked;
no simulated continuation was substituted in this extraction.

## Verification and data integrity

- 5 grant-safety tests passed, 5 subtests passed; 2 previously tested offline
  tests were deselected. No failed tests.
- All six factory captures were normalized and independently checked with
  the existing boundary validator. Their reported admission error is
  BOUNDARY_UNRESOLVED; this is an expected source-data blocker, not a PASS
  for scheduling admission.
- All 14 generated payload hashes in `manifest.json` were recomputed and
  matched. This README is outside that original payload manifest.
- Factory 1 raw bytes exactly match its historical capture SHA-256
  `010541c698ca6c46aee096d322e1c197598b4d6fbd8ec2bfa6bfbca0b8974438`.
- The current column names/types/nullability match the saved 171-column
  historical schema evidence. This is not a full index/DDL audit.
- Three protected historical capture/audit hashes were unchanged before and
  after extraction. No Factory 2 benchmark was edited or rerun; its complete
  artifact tree was not rehashed in this task.
- Factory-table writes: none by this workflow. The administrator changes
  were limited to the authorized new account and grant; factory queries used
  only the SELECT-only reader in read-only transactions. This is evidence
  about this workflow, not a claim that no external actor wrote concurrently.
- No Django database was opened. No scheduling or activation was performed.

## Next step

Obtain approved, versioned operation-level crew/machine-set requirements and
their applicability to products, lines and methods. Validate the rules against
the captured operations without assuming one worker per operation. Obtain
trusted continuation evidence for the 278 partial operations before production
admission. Keep all scheduling experiments TEST-only and live activation off.

To repeat only extraction using the configured reader:

```powershell
venv\Scripts\python.exe web_v1/audits/factory_source_integration_v1/capture_six_factories.py
```

Every execution creates a new output directory and preserves earlier captures.
There is no need to rerun account provisioning.
