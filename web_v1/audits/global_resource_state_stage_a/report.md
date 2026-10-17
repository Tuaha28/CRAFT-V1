# CRAFT Stage A: versioned TEST resource state

Status: **PARTIAL industrial foundation; PASS for the isolated TEST vertical slice.**

## What executes

`GLOBAL_RESOURCE_STATE_V1` is a sealed, deterministic snapshot in each
`CraftRun.snapshot`. It records source identity/hash, reference time and
time-zone label, active schedule version, resource/calendar/eligibility and
manager-priority revisions, factory-qualified resource rows, completed and
remaining quantity ledger, separate execution and scheduling dispositions,
route boundaries, and fixed/approved intervals. The app-owned ACTIVE
`ScheduleVersion` is exported by `governance.services.global_state_for`.
The background TEST worker consumes these same intervals in CRAFT calendar
preparation. At run completion and TEST promotion, a separate pure validator
checks the materialized candidate against the sealed snapshot. Promotion also
rebuilds the snapshot, checks the active version/source/revisions and verifies
the assembled candidate preserves untouched approved rows before atomic CAS.

In the isolated two-order TEST run, A's approved machine/worker interval was
300–900 seconds. The next CRAFT solve scheduled B at 2,400–3,000 seconds on
the same machine/worker, respecting A and the 1,500-second changeover. Both
runs reached CP-SAT, materialization, exact replay, physical and scope
validation, global conflict validation, candidate persistence and authorized
TEST promotion. No production data or schedule was activated.

## Pinned Factory 1 simulation rerun

Raw source SHA-256: `010541c698ca6c46aee096d322e1c197598b4d6fbd8ec2bfa6bfbca0b8974438`.
Scenario hash: `3c04126f8d1e24ebc41c4286ebc57132f59a542bb7a9f552a6a6a19ee87c36b3`.
Final global-state fingerprint: `883900fcc32c6752d1c5b5c35929e1cab4062c4ff425a89ecc9aad962ded341c`.
The snapshot accounted for all **2,879** operation identities and quantities,
kept the **82** real partial operations blocked from production admission,
and bound all **35** hypothetical fixed holds. The bounded optimizer again
scheduled **25/25** selected operations over **7/7** accepted windows using
the same four-by-four sampled resource-domain policy. Every window reported
replay `PASS`, physical `VALID`, scope `SCOPE_VALID`; separate assembled and
global conflict checks reported `PASS`.

| Measured stage | Seconds |
|---|---:|
| Snapshot build | 0.170 |
| Resource construction | 5.403 |
| Calendar regime generation | 73.241 |
| Reported CP-SAT solve | 5.412 |
| Solve plus validation | 11.859 |
| Independent assembled check | 11.975 |
| Global conflict check | 0.069 |
| Bounded run wall | 90.832 |
| Complete audit-script wall | 108.657 |

Sampled peak RSS was **842.66 MB**. This audit loads the pinned source,
normalized snapshot and prior artifact in one process; its memory is not
directly comparable with the previous 616.98 MB run. A repeat of this audit
varied substantially in calendar cost (about 30.7 to 91.5 seconds), so no
speedup or stable industrial latency is claimed.

The Factory 1 capture contains **zero actual application-approved future
outside assignments**. Its independent assembled validator proves exact
source calendars, eligibility, quantities, routes and physical occupancy;
the global-state contract's broad 30-day calendar envelope only provides
an additional overlap/provenance gate. This does **not** establish complete
future competitor closure, legal-alternative coverage or full-factory
scheduling. The application-owned TEST approval proof is separate from this
Factory 1 simulation proof.

## Regression and safety

- Full Django governance suite: **35 passed**, 0 failed, in 218.281 seconds
  before the final all-operation accounting refinement. The final focused
  Stage-A suite then passed **8/8** tests in 140.148 seconds, 0 failed.
  One subsequent external-predecessor negative assertion passed separately.
- CRAFT integration/resource-domain regression: **106 passed**, 24 subtests
  passed, 0 failed, 2 existing Torch/Python 3.14 deprecation warnings.
- Django migration check: no missing migration; application database only.
- Read-only protected subset check: 264 selected paths, three pre-existing
  source-code hash differences (`joint_modes.py`, `calendar_regimes.py`,
  `objectives.py`), zero changed or missing selected Factory 2 frozen outputs.
  The full 7,640-path manifest was **not** rechecked.
- Browser workflow was not rerun in this Stage-A change. No React source was
  changed. Production activation remains disabled.

Reproduce with `python web_v1/audits/global_resource_state_stage_a/run_factory1.py`
in the CRAFT virtual environment; application TEST setup and tests are in
`web_v1/INTEGRATION.md`. Generated files are the sealed snapshot,
`factory1_bounded_25_stage_a.json`, and `factory1_stage_a_result.json`.
SHA-256 respectively:

- `0baed2447cd9b5bc9844d9791e60520aabda310c2f6d20725c76ea3b06bef58f`
- `7bddbf2c76c915d9a169875f61f34758466fe9bd332300fe2b3b0c9cc2acfc22`
- `825599fd72d1a66a36548ed49dda6cad391a9c9390d3b6c3898ded9e24ef3fd2`

References informing the unchanged interval/precedence model and limit
classification: [OR-Tools job shop](https://developers.google.com/optimization/scheduling/job_shop),
[OR-Tools CP-SAT statuses](https://developers.google.com/optimization/cp/cp_solver).

## Next gate

Use the same exporter with a **genuinely application-owned, validated active
Factory 1 TEST schedule** and a source-pinned follow-on neighborhood, then
prove crossing route boundaries and outside approved worker/machine intervals
through CRAFT, independent validation and promotion. Before production use,
obtain authoritative in-flight/attendance/calendar evidence, establish a
deployment database's concurrent-promotion behavior, and validate exact
time-zone semantics. SQLite tests alone are not a production concurrency proof.
