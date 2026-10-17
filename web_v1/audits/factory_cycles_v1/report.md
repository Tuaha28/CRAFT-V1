# CRAFT tentative Factory 1 cycle checkpoint (2026-10-13)

Status: PARTIAL, controlled TEST simulation only. This is not an approved factory execution record.

The new `crafter.production.cycles` contract seals a source-bound tentative checkpoint with selected operation identities, bounded window/cycle policy, accepted-window evidence, deferral reasons, and revision. It never grants manager approval. `factory1_cycles.advance` runs one real source-derived CP-SAT window per worker invocation and checks each accepted prefix with the independent assembled and global-conflict validators before Django durably stores the checkpoint. The existing manager approval path remains the only way to activate a TEST schedule.

The focused Django test `governance.test_factory1_cycles` passed (1 test, 206.025 s) with eight pinned Factory 1 operations, two four-operation cycles, four two-operation windows, one simulated abandoned-worker lease, idempotent submission, independent assembled PASS, global conflict PASS, no candidate until all windows completed, and authorized TEST promotion. A separate stale-revision test passed (1 test, 5.706 s): checkpoint admission failed while preserving an empty active TEST schedule. This proves controlled restart at an accepted checkpoint; it does not prove recovery from every interruption point or 200-operation throughput. The combined Django Stage A/Stage B governance regression then passed **13 tests, zero failures in 623.977 s**, including the 12+13 application-approved TEST workflows. This combined run started before the added stale-revision test was included in discovery; that test's separate pass is reported independently. The older general governance suite passed **27 tests, zero failures in 145.001 s**, including worker failure and manager approval safeguards. Frontend tests: 13 passed; lint and production build passed. Focused CRAFT tests: 46 passed, zero failed (three warnings). A separate failure-path test passed after correcting its mocked window number (1 passed, 18.40 s).

The sealed TEST global-state snapshot contains a 2,879-operation ledger and 35 hypothetical fixed holds, but the *admitted scheduling domain* is still restricted to 25 selected operation identities. Scheduling 50/100/200 operations and manager-priority-aware cycle selection have not been validated. The saved 35-operation Stage B run accepted 28 and left seven unresolved at window 8, reporting `RESOURCE_DOMAIN_EXPANSION_LIMIT`; it is not evidence of physical infeasibility. The source capture has no authoritative crew-size field for these operation rows, so multi-worker requirements cannot be inferred from this capture. Real partial operations remain blocked and the 35 holds are hypothetical.

The governed checkpointing worker retains the 180-second per-invocation limit and resumes accepted work; a rejected window now returns a terminal computational-limit result and cannot be repeatedly claimed as a resumable checkpoint. Unrelated work after a failed window still needs a sound skip/repair policy and independent validation. The fallback in `ProductionControllerV1._run_direct_matching` now returns the actual initial result instead of referring to an undefined variable; its focused regression passed.

The separately saved Stage B integrated-adaptive 25-operation artifact (`../stage_b_resource_domain_v1/factory1_25_integrated_adaptive_v3.json`) reports 25/25 in seven windows, 45.347 s wall time, 32.087 s summed calendar preparation, 8.202 s summed solve-and-validation time, and 852.69 MB sampled peak RSS. Its integrated independent assembled and global conflict statuses are PASS, protecting 35 simulated holds and zero application-approved outside assignments. These are historical measurements on a different 25-operation run, not timings for the new eight-operation checkpointed cycle test or evidence of complete legal-domain coverage.

No legacy database write, live activation, or frozen benchmark rewrite was performed. A read-only subset check found zero missing paths among 264 protected paths, seven changed **source** files, and no changed Factory 2 frozen output in that subset. The full 7,640-path manifest was not verified.

## Reproduce focused checks

From the repository root:

```powershell
venv\Scripts\python.exe -m pytest tests/test_factory_cycles.py tests/test_production_controller_v1.py tests/test_stage_b_source_authoritative.py tests/test_stage_b_resource_domains.py tests/test_resource_domain_v2.py tests/test_resource_domain_v3.py web_v1/backend/integrations/craft/tests/test_calendar_cache_safety.py -q
$env:DJANGO_SECRET_KEY='isolated-cycle-test-key'
web_v1\backend\.venv\Scripts\python.exe web_v1/backend/manage.py test governance.test_factory1_cycles --settings=config.settings_integration --noinput
```

The Django test uses an isolated test database and launches the actual CRAFT worker subprocess. Do not substitute the legacy factory database or enable live admission.
