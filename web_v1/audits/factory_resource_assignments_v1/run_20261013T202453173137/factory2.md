# Factory 2 resource assignment audit

| Metric | Result |
| --- | --- |
| Operations / orders | 2124 / 248 |
| Workers / machines / lines | 2051 / 498 / 17 |
| Operations with TEST machine / worker eligibility pools | 2124 / 2124 |
| Operations with a worker pool carrying a defined TEST calendar | 2124 |
| Historical lots / future lots excluded | 3771 / 37481 |
| Operations with historical assignments | 219 |
| Operations with multiple simultaneous lot workers | 196 |
| Authoritative crew requirements / proven scheduling-ready operations | 0 / 0 |
| Source admission classifications | {'BLOCKED': 49, 'COMPLETED': 170, 'REQUIRES_EVIDENCE': 1905} |

Eligibility uses captured factory/line/capability/skill records and the existing code taxonomy. It is not evidence of approved staffing or complete legal line mobility. Historical means clipped before the dataset reference time; the source is generated TEST data, not verified factory actuals.

Worker roles: `{'Operator': 1945, 'Mechanic': 58, 'Line_Supervisor': 48}`. Recorded skill efficiency range: `[70.0, 97.98]` percent. Workers without skills: 106; without batch membership: 106; with multiple batches: 0; flagged leave: 177; missing calendar rows: 0.

Machine current statuses: `{'Active': 456, 'Idle': 11, 'Under_Maintenance': 22, 'Breakdown': 9}`. Missing order deadlines: 0.

## Resource pools and potential bottlenecks

| Machine requirement | Machines | Skilled active operators | Same-line batch candidates | Nominal SAM × quantity minutes |
| --- | ---: | ---: | ---: | ---: |
| Automated Pattern Tacker | 62 | 576 | 576 | 337575.5 |
| Feed-off-the-Arm Special | 61 | 599 | 599 | 251648.2 |
| Flatlock Coverstitch | 91 | 563 | 563 | 305027.44 |
| Overlock Serger (4-Thread) | 141 | 598 | 598 | 631989.38 |
| Single-Needle Lockstitch | 143 | 563 | 563 | 658083.15 |

Nominal workload is a demand screen across all captured operations, including completed work. It is not residual workload, utilization, elapsed duration, or proven bottleneck capacity; crew and calendar authority are missing.

## Assignment and calendar checks

Issues: `{'ASSIGNMENT_OUTSIDE_WORKER_BATCH_LINE_POLICY_UNKNOWN': 38781, 'WHOLE_OPERATION_BOUNDARY_DIFFERS_FROM_SEEDED_LOT_MODEL': 186, 'WIP_AFTER_REFERENCE_NOT_CURRENT_EVIDENCE': 3766}`.

Specific assignment checks (zero means no inconsistency detected): `{'WORKER_FACTORY_OR_ID': 0, 'MACHINE_FACTORY_OR_ID': 0, 'MACHINE_LINE_MISMATCH': 0, 'MACHINE_CAPABILITY_MISMATCH': 0, 'WORKER_SKILL_MISMATCH': 0, 'RECORDED_BATCH_MEMBERSHIP_MISMATCH': 0, 'HISTORICAL_LOT_PRECEDENCE_OR_QUANTITY': 0, 'PLANNED_LOT_PRECEDENCE_OR_QUANTITY': 0, 'LOT_QUANTITY_VS_ORDER_MISMATCH': 0, 'DUPLICATE_LOT_ID': 0, 'DUPLICATE_ROUTING_SEQUENCE': 0}`.

Model calendar segment checks: `{'pass': 3872, 'fail': 0, 'unknown': 0}`. Explicit schedule dates: `['2026-09-10']`.

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
| 19 | 29 | 174 | 3 | 6.0 | 222 |
| 20 | 33 | 204 | 3 | 6.182 | 218 |
| 21 | 31 | 204 | 3 | 6.581 | 246 |
| 22 | 30 | 204 | 3 | 6.8 | 194 |
| 23 | 37 | 204 | 3 | 5.514 | 272 |
| 24 | 31 | 204 | 3 | 6.581 | 235 |
| 25 | 29 | 204 | 3 | 7.034 | 245 |
| 26 | 25 | 204 | 3 | 8.16 | 174 |
| 27 | 30 | 204 | 3 | 6.8 | 239 |
| 28 | 31 | 139 | 3 | 4.484 | 232 |
| 29 | 28 | 0 | 0 | 0.0 | 215 |
| 30 | 20 | 0 | 0 | 0.0 | 145 |
| 31 | 27 | 0 | 0 | 0.0 | 236 |
| 32 | 31 | 0 | 0 | 0.0 | 228 |
| 129 | 19 | 0 | 0 | 0.0 | 133 |
| 130 | 30 | 0 | 0 | 0.0 | 271 |
| 134 | 37 | 0 | 0 | 0.0 | 266 |

These are roster ratios across rotations, not simultaneous crews, capacity guarantees, or utilization.

## Staffing evidence for every operation type

| Operation | Machine type | Evidence | Historical lots | Max concurrent lot workers | Required crew |
| --- | --- | --- | ---: | ---: | --- |
| Armhole binding | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 18 | 3 | UNKNOWN |
| Bartack (pocket corners, zip base) | Automated Pattern Tacker | OBSERVED (seeded TEST records) | 116 | 7 | UNKNOWN |
| Bartack reinforcement | Automated Pattern Tacker | OBSERVED (seeded TEST records) | 120 | 6 | UNKNOWN |
| Belt loop attach | Automated Pattern Tacker | OBSERVED (seeded TEST records) | 125 | 6 | UNKNOWN |
| Bottom hem | Flatlock Coverstitch | OBSERVED (seeded TEST records) | 256 | 7 | UNKNOWN |
| Bottom hem | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 20 | 3 | UNKNOWN |
| Bottom rib / hem | Flatlock Coverstitch | OBSERVED (seeded TEST records) | 119 | 6 | UNKNOWN |
| Buttonhole + button attach | Automated Pattern Tacker | OBSERVED (seeded TEST records) | 18 | 3 | UNKNOWN |
| Collar make + attach | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 29 | 2 | UNKNOWN |
| Cuff / sleeve rib attach | Flatlock Coverstitch | OBSERVED (seeded TEST records) | 121 | 5 | UNKNOWN |
| Cuff attach | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 5 | 2 | UNKNOWN |
| Fly / zip attach | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 140 | 6 | UNKNOWN |
| Hood / collar attach | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 125 | 7 | UNKNOWN |
| Hood / mock-neck collar make | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 128 | 11 | UNKNOWN |
| Inseam close | Feed-off-the-Arm Special | OBSERVED (seeded TEST records) | 131 | 6 | UNKNOWN |
| Neck rib attach | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 168 | 10 | UNKNOWN |
| Neck topstitch | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 160 | 11 | UNKNOWN |
| Panel serging | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 234 | 11 | UNKNOWN |
| Placket attach | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 40 | 5 | UNKNOWN |
| Pocket attach | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 125 | 6 | UNKNOWN |
| Pocket attach / welt | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 144 | 7 | UNKNOWN |
| Shoulder join | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 304 | 12 | UNKNOWN |
| Side seam + armhole close | Feed-off-the-Arm Special | OBSERVED (seeded TEST records) | 23 | 3 | UNKNOWN |
| Side seam + underarm close | Feed-off-the-Arm Special | OBSERVED (seeded TEST records) | 274 | 8 | UNKNOWN |
| Side seam close | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 130 | 7 | UNKNOWN |
| Sleeve attach | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 288 | 8 | UNKNOWN |
| Sleeve hem | Flatlock Coverstitch | OBSERVED (seeded TEST records) | 124 | 8 | UNKNOWN |
| Waistband attach | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 126 | 5 | UNKNOWN |
| Yoke / shoulder join | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 35 | 3 | UNKNOWN |
| Zip attach | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 125 | 8 | UNKNOWN |

A scalar employee/machine pair per lot and parallel execution of distinct lots do not establish a mandatory crew size. No requirement is classified AUTHORITATIVE or silently inferred. Factory-approved operation staffing rules, line mobility, calendar/absence authority, and unresolved execution boundaries remain required.

Detailed pool IDs, every operation identity, line roster ratios, quantities and bounded conflict examples are in the corresponding JSON. No database or scheduling component was modified.
