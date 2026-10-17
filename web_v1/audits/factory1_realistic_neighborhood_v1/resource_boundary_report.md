# Factory 1 outside-boundary and resource-domain checkpoint

Status: **PARTIAL**. The 25-operation source-derived schedule is physically valid with a larger *sampled* resource domain. Full legal-alternative coverage and factory-wide external scheduling-right closure remain unproven. All work is `SIMULATION_ONLY`; the 82 real partial operations remain blocked for production admission.

## Boundary contract

A bounded candidate is conflict-closed only against a pinned factory state when (1) all fixed in-flight holds and all approved outside worker/machine intervals touching its eligible resources are present, (2) crossing routing dependencies have proven completion/release boundaries, and (3) the candidate's source and active-schedule version remain unchanged through governed promotion. Unscheduled outside operations have **no invented fixed assignment**. They can be explicitly deferred, but this does not give the candidate exclusive future resource rights unless a coordinator serializes later scheduling and promotion against the same version.

The isolated [boundary contract](boundary_policy.py) accepts a source-pinned, versioned, hashed `APPROVED_TEST_SCHEDULE_SNAPSHOT`. The runner adds its assignments as immutable machine and worker reservations and rejects overlap with the 35 simulated holds. The separate [assembled validator](validate_bounded.py) rejects local overlap with approved outside machine/worker intervals, stale or missing boundary evidence, unsupported external route release, and source-ineligible or off-shift outside assignments. It does not treat planned source lot assignments as commitments.

There is **no authoritative Factory 1 approved outside schedule snapshot** in this captured benchmark. The [single outside assignment fixture](approved_outside_fixture_v1.json) emulates an approved TEST state, derived from a previously validated simulated candidate; it is not a real governance approval. With that fixture, an eight-operation two-window solve and independent assembled check passed. Negative tests rejected an approved outside machine/worker collision, a stale hash, and an approved interval colliding with an existing fixed hold. The 2,683 outside incomplete operations still lack committed future assignments. Consequently this is a tested boundary mechanism, **not full Factory 1 conflict closure**.

## Legal alternatives and measured run

The source supplies 96-268 active capable machines and 771-853 active, not-on-leave skilled workers per selected operation, with crew size one. The previous three-by-three sampling produced 225 configurations. A controlled four-by-four expansion produced **400**, with every prior configuration retained. The [independent source audit](integrity_bounded_25_expanded_v1.json) records each operation's eligible counts, generated worker and machine identities, bounded exclusions, and truncation reason; it found no invalid generated configuration. Calendar feasibility and reservations still depend on exact regime checks. This expansion is **not domain completeness**; no restricted-domain failure is labeled full physical infeasibility.

The expanded [25-operation run](bounded_25_expanded_v1.json) used the same pinned source and simulation, completed **25/25 operations across seven accepted windows**, and passed exact replay, physical validation, scope validation, and [independent assembled validation](independent_bounded_25_expanded_v1.json). All 35 simulated holds remained protected. The full scheduling-input fingerprint changed intentionally because the domain changed; source and scenario fingerprints were unchanged.

| Equivalent pinned input | Configurations | Regimes | Calendar | CP model | CP solve | Total wall | Sampled peak RSS |
|---|---:|---:|---:|---:|---:|---:|---:|
| Previous sampled domain | 225 | 4,571 | 29.674 s | 0.385 s | 3.147 s | 40.140 s | 611.06 MB |
| Expanded sampled domain | 400 | 8,259 | 42.411 s | 0.779 s | 3.902 s | 54.637 s | 616.98 MB |

This is a coverage-cost comparison, not a speedup claim. Calendar-regime generation remains the main measured cost. The seven accepted windows may choose different equal-objective assignments because CP-SAT is parallel; both assembled results passed independent checks.

## Verification and next gate

This phase's four new boundary/domain tests and 15 existing assembled-validator tests passed, zero failed; two focused post-change tests also passed. No CRAFT mathematical-core or Web V1 source file changed, so the 172-operation Factory 2 run and browser workflow were preserved but not rerun. The runner forbids legacy database connections. Protected historical outputs were not written in this phase; the previous subset check found no changes among 78 checked Factory 2 outputs, but the full 7,640-entry protection manifest remains unverified.

Do **not** run a 30-70 operation success benchmark yet. First connect the boundary contract to a genuinely application-owned, validated active-schedule snapshot with optimistic version checks. Then prove that outside approved assignments, fixed holds, crossing routes, and deferred future operations survive promotion. Next, implement staged domain expansion in the normal CRAFT controller with explicit computational-limit status and benchmark its calendar cost on the pinned 25-operation state before scaling.

Technical references: [OR-Tools job-shop resource exclusivity](https://developers.google.com/optimization/scheduling/job_shop), [CP-SAT statuses and unknown/time limits](https://developers.google.com/optimization/cp/cp_solver/), and [Perron et al., Parallel Large Neighborhood Search](https://www.iro.umontreal.ca/~gendron/Pisa/References/CPHybrid/Perron03b.pdf). These support preserving fixed outside work and keeping computational limits separate from proven physical infeasibility; they do not establish the missing Factory 1 governance evidence.
