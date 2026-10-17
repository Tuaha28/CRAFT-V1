# Safe legacy access and operation-crew evidence — 2026-10-13

Status: **LIVE EXTRACTION BLOCKED; CREW REQUIREMENTS UNKNOWN**. This is a
read-only continuation of the pinned offline audit. No MySQL connection was
made. No Django database was opened.

## Access check

Only presence flags were inspected, without printing secrets:

| Check | Result |
| --- | --- |
| `LEGACY_DB_USER` configured | No |
| `LEGACY_DB_PASSWORD` configured | No |
| General `DB_USER` is root | Yes |
| Effective grants verified on a dedicated account | No — no account configured |

The dedicated legacy reader refuses implicit/root credentials and checks
`SHOW GRANTS` before any source query. Its in-transaction source read also
requires read-only mode and InnoDB tables. These guards are unit-tested, not
live-verified; they are not a substitute for a DBA review of effective grants.

An authorized database administrator, **not CRAFT**, must provision or identify
an account restricted to `SELECT` on `garment_fyp_db` (and no write, DDL,
EXECUTE, grant, or role-derived write privileges), restrict its client host,
manage its secret out of band, and independently inspect its effective grants.
For example, the administrator may choose an account scoped to
`garment_fyp_db`.* and verify the result with `SHOW GRANTS`; CRAFT must not
run `CREATE USER`, `GRANT`, or privilege changes. Only then should the
credentials be supplied as `LEGACY_DB_USER` and `LEGACY_DB_PASSWORD` through
the approved secret mechanism. The first CRAFT connection must itself verify
the effective grants and fail closed on any unrecognized grant or role.

An illustrative **DBA-only** provisioning checklist (not executed here) is:

```sql
CREATE USER 'craft_reader'@'<approved-client-host>'
  IDENTIFIED BY '<secret-supplied-out-of-band>';
GRANT SELECT ON `garment_fyp_db`.*
  TO 'craft_reader'@'<approved-client-host>';
SHOW GRANTS FOR 'craft_reader'@'<approved-client-host>';
```

The DBA must inspect existing accounts/roles first, avoid extra privileges,
and use the organization's approved secret-handling and host policy. CRAFT
must not execute this checklist or receive a password in an audit report.

## Crew-size evidence audit

| Candidate source | What it actually establishes | Operation crew size? |
| --- | --- | --- |
| Historical `routing_operations` schema | Product route sequence, operation name, SAM/SMV allocation, machine type | **No** required-worker-count column |
| `machine_capabilities` and `machinery_assets` | Machine type/line/factory eligibility | No worker cardinality |
| `operator_skill_matrix` and `employees` | Eligible seeded worker skills/factory membership and efficiency | Eligibility, not number required |
| `work_schedules` | Seeded shift/leave availability | Availability, not number required |
| `employee_batches` and `batch_members` | Line staffing groups/rotation | Group capacity, not per-operation crew |
| `WorkStation.xlsx.Worker Capacity` in documented seeder | Per-line, per-shift staffing capacity | Not per-operation demand |
| `job_shop_schedules.employee_id` | One historical assigned employee per seeded lot row | Observed row cardinality does not prove a minimum crew rule |
| Literature-derived routing tables | Simulated garment routing template | Not factory-approved staffing standard |

Seeder provenance matters: `seed_database_fixed.py` documents that employee
identities, role split, shift/leave assignments, skills, and efficiency ratings
are generated; routing is literature-derived, with category SMV split across
steps. The database is suitable for **controlled TEST research**, not proof of
real worker attendance or factory-approved crew requirements. The documented
9,000-person company total and line worker-capacity values do not convert
those generated identities into observed production evidence.

**Explicitly stored operation crew requirements:** none found in the 171-column
historical schema or the sealed Factory 1 operation rows.

**Defensibly derived crew requirements:** none found. No cited factory-approved
rule maps operation/product/machine/line conditions to worker cardinality.

**Unknown:** minimum and maximum simultaneous workers per operation; whether
multiple machines/workers form a fixed crew or are independently substitutable;
crew changes across product, line, quantity, or method revisions; effective
dates; and who authorized the standard.

To resolve this, the factory must supply a versioned, approved staffing
standard keyed by factory, product or style, routing operation and relevant
line/machine method. Each rule must specify required simultaneous worker
count (or approved range), machine-set count, skill requirements, effective
period, source document/revision, and approver. If a rule is derived from
time studies or industrial engineering standards, the derivation and
applicability conditions must be documented. CRAFT should reject missing,
ambiguous, or conflicting rules rather than defaulting to one worker.

## Current operation-accounting limit

The hash-pinned Factory 1 capture accounts for 2,879 operations: 171
completed; 82 partially executed but production-blocked by missing verified
continuation; 2,626 unstarted but missing source crew-size requirements.
None is established as source-admissible new scheduling work. Historical
operation-instance counts for factories 2–6 are 2,124, 1,329, 94, 2,288,
and 85 respectively; **their present completed/blocked/schedulable counts
are UNKNOWN** because equivalent pinned full-source captures were not
available and no safe live account was configured.

The existing per-factory reader and normalizer can be reused after the access
gate. They do not yet extract verified attendance events, machine-status
history, material readiness, or approved operation-crew standards. Those
inputs must be assessed before declaring a complete factory scheduling
contract or starting ~200-operation cycles (with <=25 operations per CP-SAT
window). Live production activation remains disabled.

## Non-modification evidence and limit

The environment check read local configuration names/flags only. The schema,
seeder, rules, and captures were read from files. No legacy or Django database
connection was attempted, so this process issued no SQL and performed no
database modification. The prior offline audit independently hashed three
protected files before and after its run; it found them unchanged. No new
database before/after comparison was possible, so this report does **not**
claim that other actors left the database unchanged.
