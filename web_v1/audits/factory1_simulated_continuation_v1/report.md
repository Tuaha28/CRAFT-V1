# Factory 1 simulated continuation V1 — partial result

Reference: `2026-09-10T00:00:00`. This audit uses only the prior captured
`factory1_source.json` and its normalized snapshot. It never queries the
legacy database, changes production admission, or creates an active schedule.

## What was built and independently checked

- Source SHA-256: `010541c698ca6c46aee096d322e1c197598b4d6fbd8ec2bfa6bfbca0b8974438`.
  Normalized snapshot hash: `5f58bb16132c5ecdb9a9fe24ebdee5b09cad53f156bbe6eabf48832411461e37`.
  Scenario ID: `F1_SIMULATED_CONTINUATION_STANDARD_WORK_V1`, seed `0` (no random draws),
  scenario hash: `3c04126f8d1e24ebc41c4286ebc57132f59a542bb7a9f552a6a6a19ee87c36b3`.
- The [identity ledger](identity_ledger.json) retains all 2,879 operation keys:
  171 completed, 82 partial, 2,626 unstarted. The [scenario](scenario.json)
  supplements **only** the 82 partials. All observed completed lot IDs and
  accepted quantities remain immutable. No future generated `actual_*` value
  is treated as an observation.
- The partial ledgers reconcile 58,336 required units as 33,983 completed,
  1,311 in-flight, and 23,042 not-started units. The 35 in-flight lots have
  distinct recorded machine and worker identities. The scenario assumes **zero
  observed intra-lot output** at the reference, so all 1,311 in-flight units
  remain to be processed hypothetically; this is not a live quantity claim.
- Processing work assumes routing `standard_allowed_minutes` is nominal
  per-unit productive time at 100% standard efficiency. It yields 63.90
  productive machine-hours for the 35 fixed in-flight lot continuations and
  1,024.87 serial productive hours for the 82 unstarted tails. These are
  **work-model estimates**, not measured duration or guaranteed completion.
  Setup for the bounded smoke is a separately declared 900-second benchmark
  assumption. No worker is assigned to unstarted tails by the overlay. The
  historical data generator actually multiplies lot quantity and SAM by a
  dataset calibration factor and divides by assigned-worker efficiency
  (`data_generation/seed_database_fixed.py`, `_build_frozen_plan`). That
  calibration value was not retained in this capture, and future generated
  durations cannot substitute for it. Consequently this nominal standard-work
  scenario is **not** a replay of the generator's original processing rates.
- For the 35 in-flight lots, the hypothetical hold keeps the recorded machine
  from reference time through projected completion, including off-shift
  pauses; the same recorded worker only produces during the recorded batch's
  rotating shift. No held machine or worker is shared by two in-flight lots.
  Seven held workers are off-shift at the reference instant; their next legal
  shift is respected. The latest hypothetical hold ends 32.475 hours after
  reference. The validator independently rechecks lot quantities, machine
  capability/line, worker skill, batch calendar, work seconds, overlap, and
  the fixed-hold lower bound on successor release.
- [Validation](validation.json): PASS with 0 consistency errors and all 2,879
  source identities preserved. Critically, the **independent real-production
  admission validator remains BLOCKED for all 82 partial operations**.

## Actual CRAFT execution

The [bounded smoke](bounded_smoke.json) used source operation `887_3501`:
171 historically completed units, one unstarted 19-unit lot, and no in-flight
hold. It selected one verified eligible machine (`2424`) and worker (`8025`)
only for this narrow fixture. CRAFT generated eight calendar regimes, produced
an `ACCEPTED` CP-SAT candidate, materialized it, returned exact replay `PASS`,
physical validation `VALID`, and scope validation `SCOPE_VALID`. Independent
work/quantity reconciliation passed: only the 19 remaining units were assigned
a productive work budget, with the original operation key retained. This is a
source-derived **simulation smoke**, not a complete resource-domain or
factory-wide scheduling proof. The chosen 900-second setup and 100% standard
efficiency remain benchmark assumptions.

Measured within the Python command after imports: 3.005 seconds to read and
deserialize source/snapshot/scenario, 0.026 seconds calendar generation,
0.045 seconds solve plus validation, 3.084 seconds wall time, sampled peak RSS
661.94 MB ([timing](bounded_smoke_timing.json)). The prior *separate* global
state preflight took 11.120 seconds and 344.75 MB sampled RSS for source read,
normalization, and production boundary validation. These are different
processes and scopes; they must not be added into a claimed full scheduling
runtime.

## Why a 2,879-operation run was not launched

The overlay is a deterministic simulation instrument, **not yet a validated
CRAFT scheduling input**. Sixty-five partials have unstarted tails only; 17
also have 35 concurrent fixed in-flight lots. CRAFT's present operation-level
interval must be shown to preserve each fixed lot hold, schedule only the
unstarted tail, and release the successor no earlier than both the fixed
continuation and the tail completion. A single-operation smoke cannot prove
that combined constraint or demonstrate all legal resource alternatives.
The other 2,626 unstarted operations also require a quantity-calibrated
standard-work contract and complete consistent machine/worker domains before
rolling windows can be admitted. The older 2,870-operation prepared checkpoint
omits order 790 and is not a safe substitute. Sending all 2,879 through that
checkpoint, or ignoring the 17 multi-hold partials, would invalidate the
benchmark. No factory-wide graph, rolling windows, assembled schedule, or
full-state physical validation is claimed here.

The first engineering blocker is therefore **simulation-only full-state CRAFT
admission**, not a demonstrated CP-SAT limit or physical infeasibility. Build
an isolated, quantity-aware source-to-CRAFT adapter that handles fixed
multi-lot occupancy and a separate residual interval with a provable
whole-operation completion boundary. Independently validate that model and
then attempt checkpointed bounded windows under a stated runtime/memory cap.
Do not use this simulation to relax the real production gate.

## Reproduction and safety

From repository root:

```powershell
venv\Scripts\python.exe web_v1/audits/factory1_simulated_continuation_v1/build_scenario.py
venv\Scripts\python.exe web_v1/audits/factory1_simulated_continuation_v1/validate_scenario.py
venv\Scripts\python.exe web_v1/audits/factory1_simulated_continuation_v1/run_bounded_smoke.py
venv\Scripts\python.exe -m pytest web_v1/audits/factory1_simulated_continuation_v1/test_scenario.py -q
```

The first two commands refuse to overwrite differing evidence. The smoke
refuses a changed semantic result and retains its first measured timing.
New scenario tests: 5 passed. Targeted existing residual/boundary/calendar/
replay regressions: 48 passed, 24 subtests passed, 2 Python 3.14 Torch
deprecation warnings, 0 failed. The protected-output comparison found zero
hash changes among 7,432 accessible prior output files; 22 missing paths were
transient pytest-basetemp files in the older manifest. No legacy DB read or
write occurred in this work, no scheduling core was modified, and live
production activation remains disabled.

Outcome: **PARTIAL**. The scenario and one CRAFT vertical slice are verified;
Factory-1-wide scheduling and industrial scalability are not yet proven.
