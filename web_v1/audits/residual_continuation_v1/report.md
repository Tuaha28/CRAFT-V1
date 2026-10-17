# Residual Continuation Contract V1

Status: **PARTIAL / FAIL-CLOSED**

Reference time: `2026-09-10T00:00:00`
Database: `garment_fyp_db` (read-only)

| Factory | Total operations | Completed | Partial | Unstarted | Partial % |
|---:|---:|---:|---:|---:|---:|
| 1 | 2879 | 171 | 82 | 2626 | 2.8482% |
| 2 | 2124 | 170 | 49 | 1905 | 2.3070% |
| 3 | 1329 | 80 | 45 | 1204 | 3.3860% |
| 4 | 94 | 28 | 3 | 63 | 3.1915% |
| 5 | 2288 | 256 | 89 | 1943 | 3.8899% |
| 6 | 85 | 19 | 10 | 56 | 11.7647% |

Total partial operations: **278**.
Resolved committed boundaries: **0**.
Resolved splittable residuals: **0**.
Boundary unresolved: **278**.

## Why the gate remains closed

The legacy schema contains lot timestamps, planned segments, status labels and a lot-count `remaining_frac` query. It does not contain measured intra-lot progress, an approved residual commitment plan, a validated finite continuation end, or an operation-level residual representation that current CRAFT can safely consume.

Existing calendar/pause-resume helpers can walk a known positive work budget for a fixed crew and hold machines across pauses. They cannot derive that budget from elapsed time, quantity alone, planned rows, or generated future actuals.

All partial operations are therefore protected as non-optimizable and retain residual lot identity/resource evidence. Successor release remains gated. No scheduler was invoked and no CRAFT core change was made.

## Safety

Database writes: 0
CRAFT core modified: no
Frozen artifacts modified: no

