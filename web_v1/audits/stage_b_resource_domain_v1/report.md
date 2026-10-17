# CRAFT Stage B continuation — 2026-10-11

Status: **PARTIAL**, simulation-only. No production activation or legacy database write was authorized.

## Implemented and tested

- `crafter/repair/resource_configs.py` now exposes mixed-calendar, skill-eligible crews to exact calendar checking. A calendar grouping is a prioritization witness, not a physical exclusion. The fixed 30-machine prefix no longer prevents eligible machines beyond it from appearing after expansion. Candidate sets have deterministic, prefix-preserving order and a lazy exhaustive fallback. Audit counts are exact *timing-independent* combination counts; they are not a proof that every combination has an executable calendar regime. The audit explicitly says the machine pool limit was not applied and that worker calendar feasibility belongs to regime generation.
- `crafter/repair/resource_domain_v2.py` reuses the exact-signature calendar cache across expansion rounds. A zero-calendar compact domain can expand legal worker/machine alternatives without falsely reporting CP-SAT infeasibility. A budget-limited result records the last **attempted** caps and explicitly does not prove physical infeasibility. `planning.py` retains its default zero-regime rejection for all other callers.
- Successful adaptive results explicitly distinguish `FEASIBLE_ON_RESTRICTED_DOMAIN` from completeness of the timing-independent configuration domain; neither label claims complete calendar coverage or factory-wide optimality.
- Small exhaustive-oracle tests prove that expansion from one to six machine sets and one to three worker crews retains the compact domain and exposes all 18 combinations in that case. A 40-machine test reaches machines beyond the old 30-prefix. Mixed shift calendars execute only in shared shift time; disjoint shifts remain non-executable.
- The pinned Factory 1 cache check found identical full regime payload hashes for fresh and warmed caches under unchanged inputs, changed productive duration, changed worker reservations, and an approved-style machine commitment. The machine commitment changed the regime hash relative to the uncommitted state, so this also exercises invalidation rather than an irrelevant no-op. See [cache_equivalence_v1.json](cache_equivalence_v1.json).
- The existing TEST Factory 1 governance recipe now accepts an explicit `stage_b_diverse` policy, binds it into the request fingerprint, rejects unknown policies, and includes the shared resource-selector implementation in candidate freshness hashing. The default `incumbent_prefix` behavior remains available. A real background worker produced and a manager TEST-approved 12 operations, then produced and approved 13 more against the first 12 outside assignments and all 35 simulated fixed holds. Independent assembled, replay, physical, scope, and global conflict checks passed; the second global check covered 47 outside commitments. This is an application-owned TEST approval, not a real factory approval.

## Factory 1 pinned comparison

The saved [comparison](factory1_25_comparison_v1.json) verified identical raw source, normalized snapshot, simulation scenario, and selected operation identities. The older `incumbent_prefix` and new `stage_b_diverse` runs both exposed **400 sampled configurations** (four machines × four workers per operation), but selected different legal resources. Both scheduled 25/25 operations across seven accepted windows. Every window reports exact replay PASS, physical VALID, and scope SCOPE_VALID. Independent assembled and global conflict validation each returned PASS against all 35 hypothetical fixed holds. The global check had **zero application-approved outside assignments** in this particular benchmark; the separate Stage A approval test covers 12+13 governed operations.

The selected operations have 96–268 source-eligible machines and 771–853 source-eligible skilled workers each. The four-by-four sample is therefore visibly truncated even though every exposed configuration is source-eligible and independently checked for execution feasibility.

| Saved run | Configs | Regimes | Calendar | CP-SAT reported | Total wall | Sampled peak RSS |
|---|---:|---:|---:|---:|---:|---:|
| Prior 25-op sample | 400 | 8,259 | 42.411 s | 3.902 s | 54.637 s | 616.98 MB |
| Stage B diverse 25-op sample | 400 | 8,439 | 36.554 s | 3.615 s | 47.404 s | 620.83 MB |

This is **not** a controlled speedup: the resource choices changed, the runs were not paired/repeated, and neither sample is a complete legal domain. The benchmark runner now has an opt-in `--stage-b-25` selection policy and writes a new artifact; its existing default and historical outputs are unchanged. The runner uses CRAFT's shared deterministic set selector, but still constructs source-derived sampled configurations rather than calling the generic core configuration generator. Thus it does **not** prove Stage B's full application integration.

An additional hash-checked **diagnostic-only** Factory 1 prepared checkpoint has 2,870 global operations and a 22-operation bounded window, so it is not the authoritative 2,879-operation simulation. The current generic core generated 75 configurations and 825 regimes in 0.183 s and 1.046 s respectively; CP-SAT was not run. Its output hash differs from an older profiled run because exposed resource alternatives changed, and profiling modes differ. See [stage_b_factory1_prepared_v1.json](../industrial_readiness_v1/stage_b_factory1_prepared_v1.json).

## Regressions and safety

- Adaptive/temporal/rolling tests: **55 passed, 0 failed, 2 Torch deprecation warnings**.
- Resource/calendar/boundary tests: **40 passed, 0 failed, 2 Torch deprecation warnings**.
- Final focused test after the audit-metadata change: **15 passed, 0 failed, 2 Torch deprecation warnings**. These runs overlap; counts must not be added as unique tests.
- Final status-classification check: **13 passed, 0 failed, 2 Torch deprecation warnings**; overlaps the sets above.
- Isolated Django Stage A governance and global-state tests: **9 passed, 0 failed**, including the application-approved 12+13 Factory 1 workflow, stale-state protection, and promotion checks. The worker used the application test database; it did not access the legacy database.
- The final opt-in Stage B governed workflow: **2 passed, 0 failed in 130.301 s**. One test exercised manager-approved 12+13 Factory 1 operations with the diverse resource policy; the other rejected an unknown policy before run creation. The earlier default-policy Stage A test also passed after the recipe change before an interrupted combined invocation; its independently completed nine-test run remains the authoritative reported regression.
- The read-only protected subset check examined 264 paths from a 7,640-entry manifest. Six **source** files differ (three pre-existing and three in this work); none of the checked frozen Factory 2 result files differed. The full manifest was not verified. Existing Factory 2 output was not overwritten or rerun.
- Browser verification was **not rerun**; Web V1 source was not changed here. Live production activation remains disabled. The 82 real Factory 1 partial operations remain production-blocked; the 35 holds are explicitly simulated.

## Why Stage B is not complete

The 25-operation runner still samples four worker/machine alternatives per operation. The governed Stage B policy uses the shared diverse selection utility, **not** the generic adaptive core generator. Generic adaptive expansion is unit-tested, including zero-calendar recovery and an exhaustive small oracle, but has not yet produced a governed Factory 1 candidate using that full generator. The generic legacy line/k-domain source is partly based on frozen research inputs and its fixed-duration contract does not yet express the captured source's per-worker efficiency-derived durations; blindly substituting it for this Factory 1 adapter would alter scheduling semantics. There is no demonstrated complete domain for this workload, no 30–70-operation Stage B expansion, and no full-factory scalability claim. Restricting the domain may yield a physically valid schedule but can never prove global infeasibility or optimality.

Next gate: reconcile the source-derived line/k and per-worker duration contracts with the generic core generator and adaptive controller, preserving approved outside assignments and all simulated holds. Then compare equal-budget governed 25-operation runs with actual adaptive expansion and explicit truncation/budget status. Only after independent replay, physical, scope, global-conflict, and assembled validation should a bounded 30–70-operation region be attempted.

Reproduce the focused checks from the repository root:

```powershell
venv\Scripts\python.exe -m pytest -q tests/test_stage_b_resource_domains.py tests/test_resource_domain_v2.py tests/test_repair_resource_config_independence.py
venv\Scripts\python.exe web_v1/audits/factory1_realistic_neighborhood_v1/run_bounded.py --stage-b-25
venv\Scripts\python.exe web_v1/audits/stage_b_resource_domain_v1/verify_factory1.py
venv\Scripts\python.exe web_v1/audits/stage_b_resource_domain_v1/verify_cache.py
$env:DJANGO_SECRET_KEY='stage-b-isolated-test-only'
web_v1\backend\.venv\Scripts\python.exe web_v1/backend/manage.py test governance.test_factory1_governed governance.test_global_resource_state --settings=config.settings_integration --verbosity 1
web_v1\backend\.venv\Scripts\python.exe web_v1/backend/manage.py test governance.test_stage_b_factory1 --settings=config.settings_integration --verbosity 1
```

The reusable CP-SAT resource-alternative architecture remains consistent with [OR-Tools job-shop modeling](https://developers.google.com/optimization/scheduling/job_shop); the code does not replace the bounded CP-SAT optimizer or relax resource exclusivity.
