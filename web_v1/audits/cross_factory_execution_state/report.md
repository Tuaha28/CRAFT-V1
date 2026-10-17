# Cross-factory execution-state audit

Status: **PASS** (read-only classification; no scheduling)

## Clock and source

- Database: `garment_fyp_db`
- Reference time(s): 2026-09-10T00:00:00
- Same reference time across factories: **True**
- Classification reuses `normalize_lot`, `normalize_snapshot` and the conservative residual-tail contract.
- Generated future `actual_*` values remain projections, never commitments.

## Operation-state comparison

| Factory | Total | Fully completed | Partial | Unstarted | Partial % | Boundary unresolved |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 2879 | 171 | 82 | 2626 | 2.8482% | 82 |
| 2 | 2124 | 170 | 49 | 1905 | 2.3070% | 49 |
| 3 | 1329 | 80 | 45 | 1204 | 3.3860% | 45 |
| 4 | 94 | 28 | 3 | 63 | 3.1915% | 3 |
| 5 | 2288 | 256 | 89 | 1943 | 3.8899% | 89 |
| 6 | 85 | 19 | 10 | 56 | 11.7647% | 10 |

Total partial operations: **278**.
Factories with partial operations: **1, 2, 3, 4, 5, 6**.
All partial operations are `BOUNDARY_UNRESOLVED` under the current contract: 0 committed boundaries and 0 independently splittable residuals.

## Interpretation

Partially executed operations occur in all six factories. Factory 5 has the largest absolute count (89); Factory 6 has the largest percentage (11.7647%). Factory 1 is not unique, although its count is substantial.

A partial operation can affect predecessor/successor readiness and may carry machine, worker and line occupancy. The detailed JSON records those holds and dependency edges for every partial operation.

The current validator intentionally blocks a complete factory snapshot when any residual boundary is unresolved. Local blocking could be considered only after resource/precedence closure proves that an affected neighborhood has no unrepresented boundary dependency; that evidence is not produced here. Therefore scope-local blocking is `needs-more-evidence` and no scheduler behavior was changed.

## Per-factory pattern counts

### Factory 1

{"completed + in-flight + unstarted": 16, "completed + unstarted, no in-flight": 65, "in-flight + unstarted, no completed": 1}

### Factory 2

{"completed + in-flight + unstarted": 6, "completed + unstarted, no in-flight": 42, "other observed pattern": 1}

### Factory 3

{"completed + in-flight + unstarted": 3, "completed + unstarted, no in-flight": 42}

### Factory 4

{"completed + in-flight + unstarted": 1, "completed + unstarted, no in-flight": 2}

### Factory 5

{"completed + in-flight + unstarted": 16, "completed + unstarted, no in-flight": 69, "in-flight + unstarted, no completed": 1, "other observed pattern": 3}

### Factory 6

{"completed + in-flight + unstarted": 3, "completed + unstarted, no in-flight": 7}

## Safety

- Database read-only: yes
- Database writes: 0
- CRAFT core modified: no
- Frozen artifacts modified: no
- Scheduler invoked: no
- ERP/web application modified: no

Detailed partial-operation evidence is in `partial_operations.json`; deterministic identity records are in `factory_identity.json`.
