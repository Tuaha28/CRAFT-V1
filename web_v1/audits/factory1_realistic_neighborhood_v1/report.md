# Factory 1 source-derived bounded scheduling checkpoint

Status: **validated physical candidates; incomplete factory conflict/domain closure**. All results are controlled `SIMULATION_ONLY`, not production admission. The original 82 partial operations remain blocked for live use.

## Pinned provenance

The [integrity audit](integrity_bounded_25_cached_v2.json) independently verifies this chain:

| Identity | SHA-256 |
|---|---|
| Raw Factory 1 capture file | `010541c698ca6c46aee096d322e1c197598b4d6fbd8ec2bfa6bfbca0b8974438` |
| Canonically ordered source semantic input | `88e86f939bddd5476c56280f83ced487bbfed11057cdc35ebe56deec7d51e3c0` |
| Normalized execution snapshot | `5f58bb16132c5ecdb9a9fe24ebdee5b09cad53f156bbe6eabf48832411461e37` |
| Simulated-continuation scenario | `3c04126f8d1e24ebc41c4286ebc57132f59a542bb7a9f552a6a6a19ee87c36b3` |
| 25-operation scheduling input | `c1a035d06dd350f8cc037685d039e7b19fc605935b786c4f8112ed9bf41e7585` |

The raw-file hash and normalized-input hash intentionally differ: the latter hashes canonically ordered JSON semantics. The scenario hash is recomputed excluding its own hash field. The saved scheduling-input packet is hashed separately. The independent validator verifies the candidate against source lots and resource records, not merely against the adapter packet.

## Scheduling results

| Controlled run | Operations | Accepted windows | Total wall | Calendar preparation | Sampled peak RSS | Independent assembled check |
|---|---:|---:|---:|---:|---:|---|
| Eight-operation baseline, [saved evidence](bounded_8_full_evidence_v2.json) | 8/8 | 2/2 | 6.54 s | not isolated in this report | not compared | PASS |
| 16-operation route prefix, [saved evidence](bounded_16_full_evidence_v2.json) | 16/16 | 4/4 | 32.73 s | 22.53 s | 602.85 MB | PASS |
| Three complete orders without cache reuse, [saved evidence](bounded_25_full_evidence_v1.json) | 25/25 | 7/7 | 62.88 s | 51.59 s | 605.35 MB | PASS |
| Same input with per-window calendar-cache reuse, [saved evidence](bounded_25_cached_v2.json) | 25/25 | 7/7 | 40.14 s | 29.67 s | 611.06 MB | PASS |

The 25-operation scope contains complete source routes for orders 790, 33, and 469. Each accepted window ran the existing CRAFT CP-SAT, materialization, exact replay, independent physical validator, and scope validator. All seven windows report `ACCEPTED`, replay `PASS`, physical `VALID`, scope `SCOPE_VALID`, and zero sampled calendar-regime equivalence mismatches. The separate [assembled validator](independent_bounded_25_cached_v2.json) checks all 25 unique identities, their source-lot quantity ledgers, fixed simulated holds, resource eligibility, minute-sampled batch shifts, worker/machine exclusivity, and whole-operation routing across windows. It passed. The maximum materialized end was 1,076,057 seconds after the reference time, within the 30-day horizon.

The cached run generated 4,571 exact calendar regimes from 225 bounded resource configurations. Source capability/skill pools contained 96–268 active capable machines and 771–853 active, not-on-leave skilled workers per selected operation; only three machines × three workers were sampled per capability, yielding nine configurations per operation. These are source-eligible witnesses, **not** a complete legal domain. `INFEASIBLE` at an intermediate stage means that stage's restricted temporal/configuration domain had no solution; later stages found valid solutions. It does not prove factory infeasibility.

The source-derived scope contains 35 simulated fixed in-flight holds, including two for selected operations and 33 outside. All 35 entered the reservation state and the independent assembled check. Complete order routes have no external predecessor or successor in the captured routing graph. Nevertheless, 2,683 outside partial/unstarted operations remain uncommitted and deferred under the earliest-due-order prefix policy. Source lots of 725 outside operations were planned on sampled machines and 256 on sampled workers. Those future planned values are **not** treated as commitments. Thus the selected schedule is physically valid under explicit deferment, but outside future resource-right closure and full factory schedule coverage are not proven.

## Calendar finding and measured improvement

The original sparse breakpoint set omitted legal route-ready calendar slices for a long partial tail. A predecessor could finish on day 4 while its successor had no emitted exact regime until approximately day 27. This is a **domain-coverage problem**, not evidence of a physical impossibility. The audit runner now adds route-ready breakpoint hints iteratively within each bounded window. These hints do not set completion times, weaken constraints, or bypass exact fixed-crew execution. A source-derived [cold-versus-warmed cache test](test_route_probe_cache.py) found identical final regime payloads and zero equivalence mismatches for a fixed window state.

Reusing the existing exact `CalendarRegimeCache` across probe iterations reduced the equivalent 25-operation run's calendar time from 51.59 to 29.67 seconds and total wall time from 62.88 to 40.14 seconds (1.57× total). The scheduling-input fingerprint and all seven optimized makespan values matched. Exact starts were not byte-identical because parallel CP-SAT selected different equal-objective solutions; subsequent route-ready probes therefore also differed. Both assembled schedules passed independent validation. This is not a claim of bitwise schedule determinism or a full-factory speedup.

For the cached 25-operation run, source construction took 4.82 seconds; CP model construction across stages took approximately 0.48 seconds, CP-SAT solving approximately 2.81 seconds, materialization 0.018 seconds, and replay plus scoped validation 0.038 seconds. Calendar preparation remained the dominant measured cost at 29.67 seconds. Stage components do not exhaust total wall time because Python setup, objective work, repeated source checks, and artifact serialization are separate.

## Verification and safety

Final focused regression tally is 51 passed, zero failed: the previously recorded 50 tests plus a new saved-25-operation assembled-validation test. See [test results](test_results.json). The evidence hashes and admission limits are pinned in [manifest.json](manifest.json).

The focused new tests passed: 14 independent-validator tests (including 12 original positive/negative cases and two input-fingerprint cases), one source-derived cache-parity test, 11 calendar/cache regressions, and 24 residual/rolling-horizon regressions; zero failed. The two pytest commands each emitted two existing Torch/Python 3.14 deprecation warnings. The complete Factory 2 172-operation benchmark and browser workflow were not rerun because no shared CRAFT core or Web V1 source was changed here.

The read-only [protected subset check](protected_subset_check.json) compared 264 paths from the historical 7,640-path manifest: 78 key Factory 2 output files and 186 core/legacy paths. No checked Factory 2 output changed or disappeared. Three core-source files differ from that older manifest (`joint_modes.py`, `calendar_regimes.py`, `objectives.py`); they were already present at this session's starting checkpoint and were not edited here. A full 7,640-file content scan was stopped after exceeding the useful diagnostic budget, so complete historical artifact integrity is **not verified**. No task command wrote to `garment_fyp_db`; the scheduling runner explicitly forbids `pymysql.connect`, and all run artifacts are in this isolated audit directory. Live admission and production activation remain disabled.

## Next engineering milestone

Do not extrapolate 25 operations to 2,879. First formalize the outside-uncommitted-work scheduling-right policy and test an expanding resource domain against the same captured state. Then benchmark 30–70 operation bounded neighborhoods with checkpointed continuity, profile calendar preparation, and independently validate every assembled result. Production admission still requires authoritative continuation evidence for the 82 real partial operations.
