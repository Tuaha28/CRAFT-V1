# Controlled manager-priority proof (2026-10-10)

Classification: `SYNTHETIC_TEST_ONLY`. This is not a Factory-1 source-order
priority run, a live continuation, or production activation. The two-order
fixture fixes one machine, one eligible worker, one line, the 06:00–14:00
calendar and a 0–300-second resource hold. Both orders have 40 historically
completed synthetic units and 60 unstarted units; only their residual workloads
enter CP-SAT. Order A also has a required final route step. Customer deadlines
are A=1,050 s and B=3,000 s from the fixture reference. The physical input is
identical for the baseline and urgent runs; only a manager B `URGENT` override
with target 900 s differs.

| Metric | Baseline | B urgent |
|---|---:|---:|
| A whole-order completion | 1,020 s | 3,120 s |
| B whole-order completion | 3,120 s | 900 s |
| A customer tardiness | 0 s | 2,070 s |
| B customer tardiness | 120 s | 0 s |
| Makespan | 3,120 s | 3,120 s |

B improves by 2,220 s while A worsens by 2,100 s. This demonstrates priority
sensitivity and a real tradeoff, not a global quality improvement. The fixed
hold, machine/worker exclusivity, calendar, route precedence and two residual
quantity ledgers pass the existing CRAFT solve, exact replay, independent
physical/scope acceptance, and integration quantity validation. The persisted
candidate comparison reconstructs completion/tardiness/makespan independently
from assignment rows rather than trusting the fixture summary.

Reproducible direct-worker inputs and full outputs are `baseline_snapshot.json`,
`urgent_snapshot.json`, `baseline_result.json`, and `urgent_result.json` in this
directory. Both returned `ACCEPTED`, `OBJECTIVE_OPTIMIZATION_COMPLETE`, replay
`PASS`, physical `VALID`, scope `SCOPE_VALID`, quantity `PASS`, with three
calendar regimes each. Solver-process measured execution after Python startup
was 0.151 s baseline and 0.164 s urgent; observed CLI wall time was about
21–22 s per invocation due primarily to startup/import costs. The combined
authenticated two-scenario browser run took 91.52 s. These times are for a tiny
synthetic fixture, not a Factory-1 scale claim.

Governance is isolated to application-owned TEST data. Manager permission and
factory access are required; priority revision, actor, reason, target and
source fingerprint enter the immutable run request. Recording a new priority
stales prior candidates. Promotion requires validated evidence and manager
review. The legacy connector is disabled in the priority worker subprocess.

Verification:

- `venv\Scripts\python.exe -m pytest -q tests/test_repair_objectives.py tests/test_calendar_regime_coalescing.py tests/test_replay_semantics.py tests/test_production_controller_v1.py web_v1/backend/integrations/craft/tests`: 132 passed, 24 subtests passed, 2 dependency warnings.
- Disposable Django integration DB: 27 governance tests passed; system check clear.
- Frontend: 13 tests passed (including cross-scope display isolation), lint passed, production build passed.
- Edge Playwright after selected-scope filtering: 2 authenticated tests passed in 91.52 s, including prior TEST_SIM scheduling and new baseline→priority→reschedule→compare→Gantt→TEST promotion flow. Evidence: `../fyp_completion_20261010/browser_e2e_20261010_142956.json`. An intermediate rerun timed out because the test did not reselect the scope after navigating back from Gantt; it is retained as `browser_e2e_20261010_142205.json` and was corrected in the test, not hidden.

Source SHA-256 at this checkpoint:

- `priority_fixture.py`: `F1F56B3C740D196E9060353DE6DC54322BB31FFAB55BF064FEE77764DC2EC2A2`
- `run_priority_fixture.py`: `10EA31941FF456B2E867CE4CA17CB8C0E31C142A6948581A838D4875AA40B3DE`
- `crafter/repair/objectives.py`: `AAC385CB9F8D912AADD92AE194E9E14C4320CA2BB5392B837DEA5F8723D28FD3`

Remaining: source-derived Factory-1 conflict closure, complete legal resource
domains, manager priority for real captured orders, full 35-hold simulation,
and factory-scale scheduling. The 82 real Factory-1 partial operations remain
blocked at production admission. No real schedule was promoted.

The 37 files listed in the preserved Factory-2
`outputs/crafter/factory2_172_v1/artifact_hash_manifest.json` matched their
recorded SHA-256 values in a read-only check (0 mismatches). This verifies that
specific benchmark set, not the entire older 7,640-path protection manifest;
the latter has previously recorded path-set discrepancies. No historical
benchmark output was deliberately edited by this phase.
