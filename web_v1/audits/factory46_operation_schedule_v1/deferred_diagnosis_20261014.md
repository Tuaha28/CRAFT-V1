# Factory 4/6 deferred-operation diagnosis — 2026-10-14

This is an additive diagnostic record. It does not replace any earlier cohort result.

## Evidence identity

Factory 4 raw source SHA-256: `101e8d59859699386301a17c6e8c9163c575638c1e768d530564f966b15f0c1c`  
Factory 4 normalized snapshot: `7c4b5d10cd91be10a10fc995bd6840b7ae9b016fad40a61dc8452b290db900d6`  
Factory 4 TEST scenario: `2bad06d2a51981e560fd5e41278b9fc7023f100761ef78479a4f45200018fcc7`

Factory 6 raw source SHA-256: `491eab8556c213ac7373c71b8f67776c0bf76c1008d8e28ca396654c19050697`  
Factory 6 normalized snapshot: `68832344d67061fdc3586e61ef690dd1876499b90c8a19571928f5360e1075c3`  
Factory 6 TEST scenario: `4cd708abeabb3b0be8f45d5ba9fe16e8491bc0bd00dc551b1ccee34e42148acf`

The rerun selected operation IDs match the saved original 25-operation Factory 4 and 10-operation Factory 6 cohorts exactly. Scenario hashes match. No database connection was permitted by the runner's PyMySQL replacement and SQLAlchemy `do_connect` tripwire.

## Factory 4, 25 operations

The one-round result remains 9/25 (3 accepted, 5 rejected windows; 39.23 s, 484.54 MiB sampled RSS). A four-round expansion rerun exposed caps 4 through 8 and also remained 9/25 (3 accepted, 5 rejected; 127.22 s, 545.07 MiB sampled RSS). The five rejected route groups and all 16 deferred keys are listed in `report.md`. The route-level probes did not isolate a single cause: at cap 8 the eligible machine and worker pools were still truncated, most routing-relaxed probes were UNKNOWN, and one route became feasible only when routing was disabled. These are not proofs of physical infeasibility.

A full captured-domain run was attempted with the same 25 keys and TEST assumptions. It was manually interrupted after approximately eight minutes because the process had consumed about 460 CPU seconds and its working set reached approximately 875 MiB without completing the cohort. The runner did not persist its in-progress windows, so that attempt generated no result JSON. It is explicitly classified as an abandoned compute-bounded attempt, not a solver failure or scheduling result. The next runner revision must checkpoint per-window evidence before attempting this expansion again.

## Factory 6, 10 operations

The previous bounded-domain result was 6/10. With all captured candidate alternatives exposed under the same TEST assumptions, it remained 6/10 (4 accepted, 2 rejected windows; 232.81 s, 581.29 MiB sampled RSS). The two route windows each had 1,920 resource configurations; calendar regimes numbered 2,512 and 2,752. CP-SAT returned INFEASIBLE for the complete captured TEST domains. Relaxing machine/worker overlap did not restore feasibility; the routing-relaxed probes were UNKNOWN at the 5-second diagnostic limit. That is a TEST-contract result only; no real-factory infeasibility is claimed.

The six-operation accepted prefix in the latest artifact passed exact replay, physical validation, scope validation, and captured-hold/global conflict validation. The four deferred IDs are `817_2694`, `817_2695`, `527_3191`, and `527_3192`.

## Validation and safety

Current focused tests after the runner changes: 53 passed, 0 failed (9 Factory 4/6 plus source-authoritative tests; 44 execution-contract, resource-domain, calendar, replay, and rolling-integration tests). Two existing PyTorch deprecation warnings remain.

The runner still uses operation-level CP-SAT, whole-operation route precedence, captured worker efficiency, captured worker rotation/rest-day logic, the same one-worker/one-machine TEST assumption, same-line pairing, 900-second setup, active-status-only machine availability, and fixed-hold quarantine. No constraints were relaxed. Production activation remains disabled; source captures and DB records were not modified; no frozen Factory 2 artifact was touched.

## Conclusion

The four-round adaptive cap increase is safe but insufficient to meet either success target. Factory 6 has a complete-domain, TEST-assumption infeasibility result with unresolved route/calendar localization. Factory 4 remains blocked by a combination of incomplete-domain route failures and the full-domain diagnostic's resource cost. Neither factory's requested target is achieved. Do not scale up until the seven deferred route windows receive checkpointed, route-aware diagnostics and any missing factory-approved execution semantics are supplied.
