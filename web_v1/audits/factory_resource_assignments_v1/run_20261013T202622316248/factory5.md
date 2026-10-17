# Factory 5 resource assignment audit

| Metric | Result |
| --- | --- |
| Operations / orders | 2288 / 268 |
| Workers / machines / lines | 2296 / 1087 / 34 |
| Operations with TEST machine / worker eligibility pools | 2288 / 2288 |
| Operations with a worker pool carrying a defined TEST calendar | 2288 |
| Historical lots / future lots excluded | 6454 / 42397 |
| Operations with historical assignments | 344 |
| Historical ops with one / multiple distinct workers across lots | 12 / 332 |
| Operations with multiple simultaneous lot workers | 323 |
| Cross-line assignments (all captured / historical) | 47457 / 6298 |
| Successor lot started before whole predecessor operation completed | 287 |
| Future WIP rows excluded at reference time | 6431 |
| Authoritative crew requirements / proven scheduling-ready operations | 0 / 0 |
| Source admission classifications | {'BLOCKED': 89, 'COMPLETED': 256, 'REQUIRES_EVIDENCE': 1943} |

Eligibility uses captured factory/line/capability/skill records and the existing code taxonomy. It is not evidence of approved staffing or complete legal line mobility. Historical means clipped before the dataset reference time; the source is generated TEST data, not verified factory actuals.

Worker roles: `{'Operator': 2167, 'Mechanic': 67, 'Line_Supervisor': 62}`. Recorded skill efficiency range: `[70.01, 97.97]` percent. Workers without skills: 129; without batch membership: 129; with multiple batches: 0; flagged leave: 172; missing calendar rows: 0.

Machine current statuses: `{'Idle': 34, 'Active': 983, 'Under_Maintenance': 58, 'Breakdown': 12}`. Missing order deadlines: 0.

## Resource pools and potential bottlenecks

| Machine requirement | Machines | Skilled active operators | Same-line batch candidates | Nominal SAM × quantity minutes |
| --- | ---: | ---: | ---: | ---: |
| Automated Pattern Tacker | 154 | 673 | 673 | 484336.32 |
| Feed-off-the-Arm Special | 129 | 635 | 600 | 338337.54 |
| Flatlock Coverstitch | 157 | 672 | 672 | 407882.61 |
| Overlock Serger (4-Thread) | 333 | 675 | 675 | 870086.17 |
| Single-Needle Lockstitch | 314 | 610 | 582 | 924557.95 |

Nominal workload is a demand screen across all captured operations, including completed work. It is not residual workload, utilization, elapsed duration, or proven bottleneck capacity; crew and calendar authority are missing.

## Assignment and calendar checks

Issues: `{}`.

Specific assignment checks (zero means no inconsistency detected): `{'WORKER_FACTORY_OR_ID': 0, 'MACHINE_FACTORY_OR_ID': 0, 'MACHINE_LINE_MISMATCH': 0, 'MACHINE_CAPABILITY_MISMATCH': 0, 'WORKER_SKILL_MISMATCH': 0, 'RECORDED_BATCH_MEMBERSHIP_MISMATCH': 0, 'HISTORICAL_LOT_PRECEDENCE_OR_QUANTITY': 0, 'PLANNED_LOT_PRECEDENCE_OR_QUANTITY': 0, 'LOT_QUANTITY_VS_ORDER_MISMATCH': 0, 'DUPLICATE_LOT_ID': 0, 'DUPLICATE_ROUTING_SEQUENCE': 0}`.

Model calendar segment checks: `{'pass': 6819, 'fail': 0, 'unknown': 0}`. Explicit schedule dates: `['2026-09-10']`.

| Conflict check | Overlap pairs | Resources affected |
| --- | ---: | ---: |
| worker_span | 0 | 0 |
| machine_span | 0 | 0 |
| worker_productive | 0 | 0 |
| machine_productive | 0 | 0 |
| planned_worker_span | 0 | 0 |
| planned_machine_span | 0 | 0 |

Span overlap includes pauses; productive overlap means actual productive segments overlap. Planned assignments are checked separately and do not become approved commitments. Current machine status and leave flags cannot establish historical breakdown or attendance intervals.

The seeder explicitly selects workers from factory-wide skill pools (`seed_database_fixed.py::build_frozen_planned_schedule`) and releases each successor after its matching lot predecessor (`simulate_actual_execution`). Therefore cross-batch-line assignments and successor lots starting before every predecessor lot completes are differences from stricter CRAFT policies requiring factory confirmation, not proof of a violation of the seeded lot model. Concurrent distinct lots do not establish one-lot crew size.

## Line staffing patterns

| Line | Machines | Roster across shifts | Batches | Roster / machine | Historical lot assignments |
| --- | ---: | ---: | ---: | ---: | ---: |
| 58 | 30 | 102 | 3 | 3.4 | 174 |
| 59 | 35 | 102 | 3 | 2.914 | 209 |
| 60 | 24 | 102 | 3 | 4.25 | 167 |
| 61 | 30 | 102 | 3 | 3.4 | 201 |
| 64 | 31 | 102 | 3 | 3.29 | 164 |
| 65 | 29 | 102 | 3 | 3.517 | 181 |
| 66 | 28 | 102 | 3 | 3.643 | 158 |
| 67 | 36 | 102 | 3 | 2.833 | 200 |
| 68 | 31 | 102 | 3 | 3.29 | 159 |
| 69 | 38 | 102 | 3 | 2.684 | 234 |
| 70 | 38 | 102 | 3 | 2.684 | 218 |
| 71 | 34 | 102 | 3 | 3.0 | 162 |
| 72 | 31 | 102 | 3 | 3.29 | 184 |
| 73 | 34 | 102 | 3 | 3.0 | 191 |
| 74 | 33 | 102 | 3 | 3.091 | 198 |
| 75 | 35 | 102 | 3 | 2.914 | 197 |
| 76 | 31 | 102 | 3 | 3.29 | 173 |
| 77 | 40 | 102 | 3 | 2.55 | 225 |
| 78 | 24 | 102 | 3 | 4.25 | 128 |
| 79 | 37 | 102 | 3 | 2.757 | 235 |
| 80 | 14 | 102 | 3 | 7.286 | 87 |
| 81 | 38 | 25 | 1 | 0.658 | 244 |
| 82 | 31 | 0 | 0 | 0.0 | 191 |
| 83 | 31 | 0 | 0 | 0.0 | 193 |
| 84 | 36 | 0 | 0 | 0.0 | 232 |
| 85 | 36 | 0 | 0 | 0.0 | 225 |
| 96 | 42 | 0 | 0 | 0.0 | 266 |
| 126 | 33 | 0 | 0 | 0.0 | 192 |
| 127 | 35 | 0 | 0 | 0.0 | 220 |
| 128 | 31 | 0 | 0 | 0.0 | 196 |
| 137 | 29 | 0 | 0 | 0.0 | 154 |
| 138 | 26 | 0 | 0 | 0.0 | 172 |
| 140 | 32 | 0 | 0 | 0.0 | 185 |
| 141 | 24 | 0 | 0 | 0.0 | 139 |

These are roster ratios across rotations, not simultaneous crews, capacity guarantees, or utilization.

## Staffing evidence for every operation type

| Operation | Machine type | Evidence | Historical lots | Max concurrent lot workers | Required crew |
| --- | --- | --- | ---: | ---: | --- |
| Armhole binding | Overlock Serger (4-Thread) | UNKNOWN (seeded TEST records) | 0 | 0 | UNKNOWN |
| Bartack (pocket corners, zip base) | Automated Pattern Tacker | OBSERVED (seeded TEST records) | 92 | 6 | UNKNOWN |
| Bartack reinforcement | Automated Pattern Tacker | OBSERVED (seeded TEST records) | 199 | 6 | UNKNOWN |
| Belt loop attach | Automated Pattern Tacker | OBSERVED (seeded TEST records) | 237 | 5 | UNKNOWN |
| Bottom hem | Flatlock Coverstitch | OBSERVED (seeded TEST records) | 447 | 7 | UNKNOWN |
| Bottom hem | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 117 | 5 | UNKNOWN |
| Bottom rib / hem | Flatlock Coverstitch | OBSERVED (seeded TEST records) | 93 | 7 | UNKNOWN |
| Buttonhole + button attach | Automated Pattern Tacker | OBSERVED (seeded TEST records) | 116 | 5 | UNKNOWN |
| Buttonhole + button attach | Single-Needle Lockstitch | UNKNOWN (seeded TEST records) | 0 | 0 | UNKNOWN |
| Collar attach | Overlock Serger (4-Thread) | UNKNOWN (seeded TEST records) | 0 | 0 | UNKNOWN |
| Collar make + attach | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 125 | 7 | UNKNOWN |
| Cuff / sleeve rib attach | Flatlock Coverstitch | OBSERVED (seeded TEST records) | 94 | 8 | UNKNOWN |
| Cuff attach | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 84 | 6 | UNKNOWN |
| Fly / zip attach | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 331 | 10 | UNKNOWN |
| Hood / collar attach | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 100 | 14 | UNKNOWN |
| Hood / mock-neck collar make | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 108 | 13 | UNKNOWN |
| Inseam close | Feed-off-the-Arm Special | OBSERVED (seeded TEST records) | 306 | 9 | UNKNOWN |
| Neck rib attach | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 258 | 13 | UNKNOWN |
| Neck topstitch | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 248 | 40 | UNKNOWN |
| Panel serging | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 567 | 22 | UNKNOWN |
| Placket attach | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 134 | 11 | UNKNOWN |
| Placket topstitch | Single-Needle Lockstitch | UNKNOWN (seeded TEST records) | 0 | 0 | UNKNOWN |
| Pocket attach | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 95 | 9 | UNKNOWN |
| Pocket attach / welt | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 355 | 12 | UNKNOWN |
| Shoulder join | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 376 | 16 | UNKNOWN |
| Side seam + armhole close | Feed-off-the-Arm Special | OBSERVED (seeded TEST records) | 120 | 6 | UNKNOWN |
| Side seam + underarm close | Feed-off-the-Arm Special | OBSERVED (seeded TEST records) | 341 | 13 | UNKNOWN |
| Side seam close | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 299 | 7 | UNKNOWN |
| Sleeve attach | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 465 | 24 | UNKNOWN |
| Sleeve hem | Flatlock Coverstitch | OBSERVED (seeded TEST records) | 238 | 10 | UNKNOWN |
| Waistband attach | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 280 | 6 | UNKNOWN |
| Yoke / shoulder join | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 127 | 10 | UNKNOWN |
| Zip attach | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 102 | 14 | UNKNOWN |

A scalar employee/machine pair per lot and parallel execution of distinct lots do not establish a mandatory crew size. No requirement is classified AUTHORITATIVE or silently inferred. Factory-approved operation staffing rules, line mobility, calendar/absence authority, and unresolved execution boundaries remain required.

Detailed pool IDs, every operation identity, line roster ratios, quantities and bounded conflict examples are in the corresponding JSON. No database or scheduling component was modified.
