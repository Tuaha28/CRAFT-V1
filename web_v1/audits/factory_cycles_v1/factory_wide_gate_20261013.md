# Factory-wide cycle admission gate, 2026-10-13

Status: **PARTIAL**. This is controlled TEST simulation, not a factory deployment.

## Implemented in this continuation

- Aligned the policy, bounded runner and independent validator to permit a configurable **maximum of 25 operations per CP-SAT window**. The default remains four; a 25-operation CP-SAT window has **not** been validated.
- Added explicit source-admission reasons for operation identities outside the pinned 25-operation resource domain: completed work, unresolved real partial continuation, unknown identity, or missing independently validated execution domain. No worker count, skill, efficiency, calendar, machine eligibility or WIP was invented to admit more operations.
- Added a pure 202-identity coordinator accounting test: eight 25-identity windows in cycle one and one two-identity window in cycle two. This tests window/cycle accounting and duplicates only; it is **not** a CP-SAT schedule or evidence of 202-operation physical feasibility.
- The pure coordinator now has a checked failure-continuation case: after A1 is deferred for a domain limit, independent B1 and B2 are selected and accepted; dependent A2 is explicitly deferred for its missing predecessor, without retrying A1. The governed Factory 1 runner still stops at a rejected CP-SAT window rather than admitting a partial candidate, so this is not an end-to-end failure-repair claim.
- Preserved the checkpointed TEST worker, manager approval and independent validators from the prior milestone. The earlier real two-cycle eight-operation Django proof remains distinct from the pure 202-identity contract test.

## Verified source boundary

The sealed Factory 1 state has 2,879 canonical operation identities: 171 fully completed, 82 partially executed and 2,626 unstarted. Quantities reconcile for every ledger entry. It contains 35 explicitly hypothetical fixed holds. It has source-derived eligibility and routing boundary data for the pinned 25-operation TEST admission set, **not** for the other 2,854 identities; exact shift feasibility is checked by the existing calendar engine and independent validator for selected work. The raw captured operation records have machine type and standard minutes, but no required crew-size field. Admitting all remaining operations as single-worker jobs would therefore be an unsupported physical assumption. The real 82 partial operations remain production-blocked.

Pinned raw-source SHA-256: `010541c698ca6c46aee096d322e1c197598b4d6fbd8ec2bfa6bfbca0b8974438`; simulation scenario hash: `3c04126f8d1e24ebc41c4286ebc57132f59a542bb7a9f552a6a6a19ee87c36b3`. Both are verified by `pinned_base()` on admission.

The captured audit directory contains a complete Factory 1 source capture; the Factory 2 result is a distinct frozen scheduling benchmark. This work did not establish equivalent, source-consistent scheduling captures for all six factories. No six-factory run was attempted.

## Existing optimization and scale evidence

The canonical objective code lexicographically treats delivery priorities, then stability/makespan as applicable. Its utilization output is `REPORTING_ONLY_V1_EXACT_PARTIAL`; avoidable feasible idle is not measured or optimized. The current Factory 1 bounded runner creates `OrderSpec` rows with no effective due time, so its saved runs do not support on-time-delivery or tardiness claims. No objective coefficient or physical constraint was changed. FIFO, EDD and SPT were **not** compared because a shared independent feasibility/evaluation contract for these baselines is not established here.

The saved source-derived Factory 1 integrated-adaptive result remains 25/25 in seven windows, independent assembled/global conflict PASS, 45.347 s wall, 852.69 MB sampled peak. A separate 35-operation artifact accepted 28 and stopped at window eight with a resource-domain expansion limit (207.008 s, 1,087.61 MB sampled peak). Neither result proves 200-operation or full-factory capacity or physical infeasibility of the remaining seven.

## Verification

- Focused current CRAFT/resource/calendar/controller/admission tests: **50 passed, zero failed**, three warnings (two Torch Python 3.14 deprecation notices and a pytest cache permission warning).
- Two targeted Django admission/stale-state tests: **2 passed, zero failed**.
- Post-change real two-cycle Django regression: **3 passed, zero failed in 255.013 s**. It again reached three `CHECKPOINTED` states and one final `VALIDATED` state, including stale-state rejection and the new outside-domain admission check. This is eight real source-derived operations across two cycles/four windows, not 200 operations.
- Focused Stage B policy and global-state governance regression: **9 passed, zero failed in 166.073 s**. Existing approved TEST candidate and conflict behavior remained valid.
- Legacy database writes: **0**. Live activation: **disabled**. Frozen Factory 2 artifacts were not changed or rerun; full historic manifest integrity was not reverified.

## Required next input and engineering gate

Obtain a versioned, authoritative per-operation crew-size/resource-requirement source (or explicit authorization for a new simulation-only assumption), plus complete factory-specific eligibility, calendars and routing contracts outside the pinned set. Then extend the source adapter and independent global-state validator together, prove a bounded 30–50-operation multi-cycle run, implement skip/repair for unrelated work after a failed window, and only then raise cycle targets toward 100/200. A manager-facing priority override must be versioned and proven to affect future-cycle selection before CRITICAL handling can be claimed for Factory 1.
