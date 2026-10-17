# Factory-1 scalability checkpoint — 2026-10-09

Status: **global source processing measured; live CRAFT scheduling admission blocked**.
This is not a completed Factory-1 schedule. No database connection, solver,
schedule promotion, or live factory activation was used in this checkpoint.

## Consistent source

`factory1_source.json` was captured earlier in one read-only consistent legacy
transaction. Its SHA-256 is
`010541c698ca6c46aee096d322e1c197598b4d6fbd8ec2bfa6bfbca0b8974438`.
The calibrated reference time is `2026-09-10T00:00:00`; dataset version is
`9fa2915b-5f28-4463-8e4f-d21b5b57dc8d`. It contains 336 orders, 2,879
operations, 57,332 lots, 958 machines, 3,103 workers, 4,394 skill rows,
3,103 work schedules, and 33 lines. The source hash and normalized snapshot
hash match the prior execution-boundary audit.

The older CP preparation checkpoint has 2,870 operations. Set comparison shows
it omits exactly `790_972` through `790_980`. It also selects three operations
whose captured state is partially executed: `33_3578`, `887_3501`, and
`979_952`. It is useful only as an offline performance/diagnostic artifact;
it is not a safe live input or an equivalent 2,879-operation benchmark.

## Measured pipeline

From the hash-verified captured source, without profiling overhead:

| Stage | Wall time |
|---|---:|
| Source read and SHA-256 verification | 0.182 s |
| JSON source deserialization | 0.378 s |
| Operation/lot normalization | 5.093 s |
| Independent execution-boundary validation | 5.422 s |
| Total | 11.120 s |

CPU time was 11.031 s; sampled peak RSS was 344.8 MB. The normalized operation
partition is 171 fully completed, 82 partially executed, and 2,626 unstarted.
The independent validator returned `BLOCKED` with exactly 82
`BOUNDARY_UNRESOLVED` errors. No other validation error was reported.

Graph construction, neighborhood selection, resource configurations, calendar
regimes, CP model/solve, materialization, replay, physical validation, and scope
validation were **not run on this authoritative source**. The existing older
22-operation checkpoint measured 75 configurations, 898 regimes, and about
1.20 s calendar generation in a separate 2,870-operation performance-only
context; those numbers cannot be added to the times above or called a safe
end-to-end result. Its Window-1 rescue diagnostic ended in `COMPUTATIONAL_LIMIT`
with no accepted candidate, and it includes unresolved partial operations.

The 2,626 unstarted operations are not equivalent to 2,626 admitted operations.
Of them, 63 have an unresolved partial predecessor and 129 are marked ready at
the reference time. Seventeen partial operations have observed in-flight
resource holds. Neither a complete residual-continuation boundary nor a
proven resource/precedence closure for a smaller live scope exists, so the
factory-level fail-closed admission rule remains active.

## Engineering decision

The 2,879-operation source can be loaded and normalized within the measured
budget; a further normalization rewrite is not supported by this checkpoint.
The earliest blocker is missing authoritative continuation evidence for 82
partial operations, not CP-SAT time or proven physical infeasibility. Do not
remove those operations or substitute simulated future `actual_*` timestamps.
The next milestone is manager/production-confirmed residual workload and
commitment evidence, followed by independent boundary validation and a
source-consistent bounded CRAFT preparation/solve/replay test. Scope-local
admission requires a separately proved resource and precedence closure.

The latest separate Factory-2 fixed-input regression completed 172/172 in 34
accepted windows with final replay `PASS`, physical `VALID`, and scope
`SCOPE_VALID` (3,043 s); it is not a Factory-1 runtime comparison.

## Reproduction and safety

Run `venv\Scripts\python.exe web_v1/audits/factory1_scalability_20261009/run_preflight.py preflight_recheck.json`, then
`venv\Scripts\python.exe web_v1/audits/factory1_scalability_20261009/compare_checkpoint.py checkpoint_recheck.json`.
Both scripts refuse to overwrite their evidence files. The focused regression
suite passed 105 tests and 24 subtests, with two dependency deprecation
warnings. The protected-output check found zero changes among 7,432 accessible
historical non-test-temp output files. Legacy DB writes: 0; live activation: no.
