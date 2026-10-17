# Factory 6 resource assignment audit

| Metric | Result |
| --- | --- |
| Operations / orders | 85 / 9 |
| Workers / machines / lines | 110 / 49 / 2 |
| Operations with TEST machine / worker eligibility pools | 85 / 85 |
| Operations with a worker pool carrying a defined TEST calendar | 85 |
| Historical lots / future lots excluded | 402 / 1887 |
| Operations with historical assignments | 29 |
| Operations with multiple simultaneous lot workers | 25 |
| Authoritative crew requirements / proven scheduling-ready operations | 0 / 0 |
| Source admission classifications | {'BLOCKED': 10, 'COMPLETED': 19, 'REQUIRES_EVIDENCE': 56} |

Eligibility uses captured factory/line/capability/skill records and the existing code taxonomy. It is not evidence of approved staffing or complete legal line mobility. Historical means clipped before the dataset reference time; the source is generated TEST data, not verified factory actuals.

Worker roles: `{'Operator': 105, 'Line_Supervisor': 4, 'Mechanic': 1}`. Recorded skill efficiency range: `[70.08, 97.96]` percent. Workers without skills: 5; without batch membership: 5; with multiple batches: 0; flagged leave: 9; missing calendar rows: 0.

Machine current statuses: `{'Active': 45, 'Breakdown': 1, 'Idle': 2, 'Under_Maintenance': 1}`. Missing order deadlines: 0.

## Resource pools and potential bottlenecks

| Machine requirement | Machines | Skilled active operators | Same-line batch candidates | Nominal SAM × quantity minutes |
| --- | ---: | ---: | ---: | ---: |
| Automated Pattern Tacker | 7 | 26 | 26 | 24033.0 |
| Feed-off-the-Arm Special | 4 | 23 | 23 | 21241.2 |
| Flatlock Coverstitch | 4 | 36 | 36 | 37309.0 |
| Overlock Serger (4-Thread) | 15 | 39 | 39 | 76999.6 |
| Single-Needle Lockstitch | 19 | 34 | 34 | 48846.6 |

Nominal workload is a demand screen across all captured operations, including completed work. It is not residual workload, utilization, elapsed duration, or proven bottleneck capacity; crew and calendar authority are missing.

## Assignment and calendar checks

Issues: `{'ASSIGNMENT_OUTSIDE_WORKER_BATCH_LINE_POLICY_UNKNOWN': 1071, 'WHOLE_OPERATION_BOUNDARY_DIFFERS_FROM_SEEDED_LOT_MODEL': 26, 'WIP_AFTER_REFERENCE_NOT_CURRENT_EVIDENCE': 399}`.

Specific assignment checks (zero means no inconsistency detected): `{'WORKER_FACTORY_OR_ID': 0, 'MACHINE_FACTORY_OR_ID': 0, 'MACHINE_LINE_MISMATCH': 0, 'MACHINE_CAPABILITY_MISMATCH': 0, 'WORKER_SKILL_MISMATCH': 0, 'RECORDED_BATCH_MEMBERSHIP_MISMATCH': 0, 'HISTORICAL_LOT_PRECEDENCE_OR_QUANTITY': 0, 'PLANNED_LOT_PRECEDENCE_OR_QUANTITY': 0, 'LOT_QUANTITY_VS_ORDER_MISMATCH': 0, 'DUPLICATE_LOT_ID': 0, 'DUPLICATE_ROUTING_SEQUENCE': 0}`.

Model calendar segment checks: `{'pass': 414, 'fail': 0, 'unknown': 0}`. Explicit schedule dates: `['2026-09-10']`.

| Conflict check | Overlap pairs | Resources affected |
| --- | ---: | ---: |
| worker_span | 0 | 0 |
| machine_span | 0 | 0 |
| worker_productive | 0 | 0 |
| machine_productive | 0 | 0 |
| planned_worker_span | 0 | 0 |
| planned_machine_span | 0 | 0 |

Span overlap includes pauses; productive overlap means actual productive segments overlap. Planned assignments are checked separately and do not become approved commitments. Current machine status and leave flags cannot establish historical breakdown or attendance intervals.

The seeder explicitly selects workers from factory-wide skill pools (`seed_database_fixed.py::build_frozen_planned_schedule`) and releases each successor after its matching lot predecessor (`simulate_actual_execution`). Therefore cross-batch-line assignments and whole-operation overlap are semantic differences requiring factory policy confirmation, not proof of a violation of the seeded lot model.

## Line staffing patterns

| Line | Machines | Roster across shifts | Batches | Roster / machine | Historical lot assignments |
| --- | ---: | ---: | ---: | ---: | ---: |
| 86 | 28 | 102 | 3 | 3.643 | 216 |
| 103 | 21 | 3 | 1 | 0.143 | 186 |

These are roster ratios across rotations, not simultaneous crews, capacity guarantees, or utilization.

## Staffing evidence for every operation type

| Operation | Machine type | Evidence | Historical lots | Max concurrent lot workers | Required crew |
| --- | --- | --- | ---: | ---: | --- |
| Bartack (pocket corners, zip base) | Automated Pattern Tacker | OBSERVED (seeded TEST records) | 20 | 2 | UNKNOWN |
| Bartack reinforcement | Automated Pattern Tacker | UNKNOWN (seeded TEST records) | 0 | 0 | UNKNOWN |
| Belt loop attach | Automated Pattern Tacker | UNKNOWN (seeded TEST records) | 0 | 0 | UNKNOWN |
| Bottom hem | Flatlock Coverstitch | UNKNOWN (seeded TEST records) | 0 | 0 | UNKNOWN |
| Bottom hem | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 16 | 2 | UNKNOWN |
| Bottom rib / hem | Flatlock Coverstitch | OBSERVED (seeded TEST records) | 21 | 2 | UNKNOWN |
| Buttonhole + button attach | Automated Pattern Tacker | OBSERVED (seeded TEST records) | 16 | 3 | UNKNOWN |
| Collar make + attach | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 16 | 1 | UNKNOWN |
| Cuff / sleeve rib attach | Flatlock Coverstitch | OBSERVED (seeded TEST records) | 22 | 2 | UNKNOWN |
| Cuff attach | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 16 | 2 | UNKNOWN |
| Fly / zip attach | Single-Needle Lockstitch | UNKNOWN (seeded TEST records) | 0 | 0 | UNKNOWN |
| Hood / collar attach | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 28 | 2 | UNKNOWN |
| Hood / mock-neck collar make | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 32 | 2 | UNKNOWN |
| Inseam close | Feed-off-the-Arm Special | UNKNOWN (seeded TEST records) | 0 | 0 | UNKNOWN |
| Panel serging | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 16 | 2 | UNKNOWN |
| Placket attach | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 16 | 2 | UNKNOWN |
| Pocket attach | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 25 | 2 | UNKNOWN |
| Pocket attach / welt | Single-Needle Lockstitch | UNKNOWN (seeded TEST records) | 0 | 0 | UNKNOWN |
| Shoulder join | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 32 | 3 | UNKNOWN |
| Side seam + armhole close | Feed-off-the-Arm Special | OBSERVED (seeded TEST records) | 16 | 2 | UNKNOWN |
| Side seam + underarm close | Feed-off-the-Arm Special | OBSERVED (seeded TEST records) | 24 | 2 | UNKNOWN |
| Side seam close | Overlock Serger (4-Thread) | UNKNOWN (seeded TEST records) | 0 | 0 | UNKNOWN |
| Sleeve attach | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 40 | 2 | UNKNOWN |
| Waistband attach | Single-Needle Lockstitch | UNKNOWN (seeded TEST records) | 0 | 0 | UNKNOWN |
| Yoke / shoulder join | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 16 | 2 | UNKNOWN |
| Zip attach | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 30 | 3 | UNKNOWN |

A scalar employee/machine pair per lot and parallel execution of distinct lots do not establish a mandatory crew size. No requirement is classified AUTHORITATIVE or silently inferred. Factory-approved operation staffing rules, line mobility, calendar/absence authority, and unresolved execution boundaries remain required.

Detailed pool IDs, every operation identity, line roster ratios, quantities and bounded conflict examples are in the corresponding JSON. No database or scheduling component was modified.
