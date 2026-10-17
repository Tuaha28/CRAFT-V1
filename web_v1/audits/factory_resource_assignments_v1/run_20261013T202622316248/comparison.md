# Six-factory worker and machine assignment audit

Pinned input files verified: 14. Database connections: 0. Database writes: 0. Production activation: DISABLED.

All counts below are from isolated generated TEST captures, not verified factory actuals. Crew-size requirements are absent from the captured schema; historical worker rows are observations only.

| Factory | Orders | Operations | Workers | Machines | Lines | Historical lots | Ops 1 worker / >1 workers across lots | Ops with concurrent lot workers | Lot successors before full predecessor completion | Cross-line assignments | Model-calendar segments pass/fail | Span/productive conflicts | Blocked partial | Crew-unknown unstarted |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 336 | 2879 | 3103 | 958 | 33 | 4941 | 9/244 | 221 | 212 | 55624 | 5349/0 | 0 | 82 | 2626 |
| 2 | 248 | 2124 | 2051 | 498 | 17 | 3771 | 12/207 | 196 | 186 | 38781 | 3872/0 | 0 | 49 | 1905 |
| 3 | 156 | 1329 | 1338 | 339 | 11 | 2073 | 6/119 | 107 | 105 | 21994 | 2112/0 | 0 | 45 | 1204 |
| 4 | 12 | 94 | 102 | 69 | 2 | 572 | 0/31 | 31 | 27 | 1329 | 600/0 | 0 | 3 | 63 |
| 5 | 268 | 2288 | 2296 | 1087 | 34 | 6454 | 12/332 | 323 | 287 | 47457 | 6819/0 | 0 | 89 | 1943 |
| 6 | 9 | 85 | 110 | 49 | 2 | 402 | 0/29 | 25 | 26 | 1071 | 414/0 | 0 | 10 | 56 |

No observed multi-worker crews are represented: each captured lot row stores one employee and one machine. Different lots of an operation run concurrently, which is not evidence of a crew requirement. Resource eligibility pools use the captured TEST capabilities/skills plus the existing taxonomy crosswalk; factory approval, complete legal alternatives, contemporaneous attendance, and line-mobility policy remain unverified.

Individual factory Markdown and JSON files contain operation-type staffing evidence, resource pools, line roster ratios, conflict examples, calendars, and operation accounting.
