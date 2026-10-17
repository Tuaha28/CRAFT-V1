# Factory 1 application-approved TEST continuation (2026-10-11)

Result: **PASS for the isolated 25-operation governed TEST sequence; NOT a
production or full-factory scalability claim.**

The source-derived workload uses orders 790, 33, and 469 from the pinned
capture. Source SHA-256 is
`010541c698ca6c46aee096d322e1c197598b4d6fbd8ec2bfa6bfbca0b8974438`;
the simulation scenario hash is
`3c04126f8d1e24ebc41c4286ebc57132f59a542bb7a9f552a6a6a19ee87c36b3`.
The exporter checks both against the existing sealed 2,879-operation global
snapshot before each run. The original 82 partial operations remain blocked
for real production admission; 35 simulated holds remain hypothetical.

In a disposable Django application database, a permissioned manager requested
the first 12-operation region. The actual background subprocess ran bounded
CRAFT, and Django persisted and approved its validated candidate. The exporter
then read that *ACTIVE application-owned version*—not a hand-written boundary
fixture—and sealed its 12 assignments into the next run snapshot. The second
background subprocess scheduled the remaining 13 operations while retaining
all 35 simulated fixed holds and enforcing the approved predecessor completion
boundaries. Its source-derived independent assembled validation and global
conflict validation both passed; every accepted window reported exact replay
PASS, physical VALID, and scope SCOPE_VALID. Django persisted and promoted the
second candidate atomically. The first version became ARCHIVED, exactly one
version remained ACTIVE, and two promotion audit events were present. A
competing candidate made from the old sealed state was rejected as stale.

The focused test `governance.test_factory1_governed` passed **1/1** in
**116.907 seconds** wall clock on its final run (an earlier passing run took
167.432 seconds). It asserts the above identities, all 25
assembled rows, 12 exported outside approvals, 35 fixed holds, preservation of
the first version's assignments, an exact outside-machine conflict rejection,
validation statuses, stale rejection and
audit/history persistence. Per-run runtime and sampled RSS are retained in
each ephemeral candidate's evidence; this test did not export those values to
a durable benchmark file, so no comparable performance claim is made.
The bounded split comprises three first-region and four second-region
accepted windows (seven total); it does not optimize the entire factory.

The first attempted 8/17 split failed at the first second-region window:
CP-SAT returned INFEASIBLE in the restricted domain after a feasibility-only
first approval placed route work too late. It was classified as a
computational/domain limit, not proven factory infeasibility. The 12/13
controlled split uses the existing priority-optimized bounded window mode;
no physical constraint, objective weight, or validator was weakened.

Regression evidence after the final scheduling change:

- `manage.py test governance.tests`: **27 passed**, 0 failed, 159.866 s.
- `manage.py test governance.test_global_resource_state`: **8 passed**,
  0 failed, 129.970 s. These include precise machine/worker conflict,
  state-revision stale, route, fixed-hold, failure recovery, and factory
  isolation negative tests.
- `pytest tests/test_boundary_aware_modes.py tests/test_route_aware_crew_expansion.py tests/test_resource_domain_v3.py -q`:
  **40 passed**, 0 failed, two existing Torch/Python 3.14 warnings.
- `pytest --import-mode=importlib` over the three Factory 1 audit test modules:
  **20 passed**, 0 failed, two existing Torch/Python 3.14 warnings.
  A direct-path invocation without `--import-mode=importlib` failed during
  collection due relative imports; no test bodies ran in that invocation.
- `manage.py makemigrations --check --dry-run`: **no changes detected**.

The read-only 264-path protected-subset check found the same three previously
recorded core-source hash differences (`joint_modes.py`, `calendar_regimes.py`,
`objectives.py`), no missing path and no changed selected Factory 2 frozen
output. The full 7,640-path manifest was not rechecked. This work did not
write to `garment_fyp_db`, change a live schedule, or enable production
activation. Browser testing was not repeated because no React code changed.

Remaining gates: incomplete legal resource-domain coverage, unresolved
unscheduled-future competitor closure, real partial-operation evidence,
production database concurrency tests, and full-factory scalability.
