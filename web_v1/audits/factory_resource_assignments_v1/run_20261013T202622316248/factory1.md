# Factory 1 resource assignment audit

| Metric | Result |
| --- | --- |
| Operations / orders | 2879 / 336 |
| Workers / machines / lines | 3103 / 958 / 33 |
| Operations with TEST machine / worker eligibility pools | 2879 / 2879 |
| Operations with a worker pool carrying a defined TEST calendar | 2879 |
| Historical lots / future lots excluded | 4941 / 52391 |
| Operations with historical assignments | 253 |
| Historical ops with one / multiple distinct workers across lots | 9 / 244 |
| Operations with multiple simultaneous lot workers | 221 |
| Cross-line assignments (all captured / historical) | 55624 / 4792 |
| Successor lot started before whole predecessor operation completed | 212 |
| Future WIP rows excluded at reference time | 4912 |
| Authoritative crew requirements / proven scheduling-ready operations | 0 / 0 |
| Source admission classifications | {'BLOCKED': 82, 'COMPLETED': 171, 'REQUIRES_EVIDENCE': 2626} |

Eligibility uses captured factory/line/capability/skill records and the existing code taxonomy. It is not evidence of approved staffing or complete legal line mobility. Historical means clipped before the dataset reference time; the source is generated TEST data, not verified factory actuals.

Worker roles: `{'Operator': 2921, 'Mechanic': 82, 'Line_Supervisor': 100}`. Recorded skill efficiency range: `[70.01, 98.0]` percent. Workers without skills: 182; without batch membership: 182; with multiple batches: 0; flagged leave: 235; missing calendar rows: 0.

Machine current statuses: `{'Active': 859, 'Under_Maintenance': 48, 'Idle': 23, 'Breakdown': 28}`. Missing order deadlines: 0.

## Resource pools and potential bottlenecks

| Machine requirement | Machines | Skilled active operators | Same-line batch candidates | Nominal SAM × quantity minutes |
| --- | ---: | ---: | ---: | ---: |
| Automated Pattern Tacker | 123 | 899 | 899 | 530743.64 |
| Feed-off-the-Arm Special | 111 | 880 | 751 | 397199.56 |
| Flatlock Coverstitch | 149 | 861 | 806 | 499782.1 |
| Overlock Serger (4-Thread) | 299 | 834 | 834 | 1041844.68 |
| Single-Needle Lockstitch | 276 | 920 | 920 | 1034711.09 |

Nominal workload is a demand screen across all captured operations, including completed work. It is not residual workload, utilization, elapsed duration, or proven bottleneck capacity; crew and calendar authority are missing.

## Assignment and calendar checks

Issues: `{}`.

Specific assignment checks (zero means no inconsistency detected): `{'WORKER_FACTORY_OR_ID': 0, 'MACHINE_FACTORY_OR_ID': 0, 'MACHINE_LINE_MISMATCH': 0, 'MACHINE_CAPABILITY_MISMATCH': 0, 'WORKER_SKILL_MISMATCH': 0, 'RECORDED_BATCH_MEMBERSHIP_MISMATCH': 0, 'HISTORICAL_LOT_PRECEDENCE_OR_QUANTITY': 0, 'PLANNED_LOT_PRECEDENCE_OR_QUANTITY': 0, 'LOT_QUANTITY_VS_ORDER_MISMATCH': 0, 'DUPLICATE_LOT_ID': 0, 'DUPLICATE_ROUTING_SEQUENCE': 0}`.

Model calendar segment checks: `{'pass': 5349, 'fail': 0, 'unknown': 0}`. Explicit schedule dates: `['2026-09-10']`.

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
| 1 | 27 | 204 | 3 | 7.556 | 129 |
| 2 | 34 | 204 | 3 | 6.0 | 182 |
| 3 | 31 | 204 | 3 | 6.581 | 172 |
| 4 | 35 | 204 | 3 | 5.829 | 203 |
| 5 | 29 | 204 | 3 | 7.034 | 148 |
| 6 | 22 | 204 | 3 | 9.273 | 130 |
| 7 | 27 | 204 | 3 | 7.556 | 160 |
| 8 | 26 | 204 | 3 | 7.846 | 129 |
| 9 | 32 | 204 | 3 | 6.375 | 181 |
| 10 | 22 | 204 | 3 | 9.273 | 109 |
| 11 | 30 | 204 | 3 | 6.8 | 139 |
| 12 | 27 | 204 | 3 | 7.556 | 132 |
| 13 | 33 | 204 | 3 | 6.182 | 144 |
| 14 | 32 | 204 | 3 | 6.375 | 164 |
| 15 | 27 | 65 | 1 | 2.407 | 125 |
| 45 | 29 | 0 | 0 | 0.0 | 150 |
| 46 | 32 | 0 | 0 | 0.0 | 176 |
| 47 | 25 | 0 | 0 | 0.0 | 134 |
| 48 | 38 | 0 | 0 | 0.0 | 187 |
| 49 | 26 | 0 | 0 | 0.0 | 119 |
| 50 | 29 | 0 | 0 | 0.0 | 152 |
| 51 | 18 | 0 | 0 | 0.0 | 96 |
| 52 | 20 | 0 | 0 | 0.0 | 104 |
| 53 | 31 | 0 | 0 | 0.0 | 159 |
| 54 | 23 | 0 | 0 | 0.0 | 108 |
| 55 | 29 | 0 | 0 | 0.0 | 141 |
| 89 | 28 | 0 | 0 | 0.0 | 140 |
| 90 | 28 | 0 | 0 | 0.0 | 150 |
| 91 | 34 | 0 | 0 | 0.0 | 186 |
| 100 | 29 | 0 | 0 | 0.0 | 160 |
| 101 | 27 | 0 | 0 | 0.0 | 157 |
| 124 | 36 | 0 | 0 | 0.0 | 167 |
| 139 | 42 | 0 | 0 | 0.0 | 208 |

These are roster ratios across rotations, not simultaneous crews, capacity guarantees, or utilization.

## Staffing evidence for every operation type

| Operation | Machine type | Evidence | Historical lots | Max concurrent lot workers | Required crew |
| --- | --- | --- | ---: | ---: | --- |
| Armhole binding | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 58 | 4 | UNKNOWN |
| Bartack (pocket corners, zip base) | Automated Pattern Tacker | OBSERVED (seeded TEST records) | 72 | 4 | UNKNOWN |
| Bartack reinforcement | Automated Pattern Tacker | OBSERVED (seeded TEST records) | 201 | 6 | UNKNOWN |
| Belt loop attach | Automated Pattern Tacker | OBSERVED (seeded TEST records) | 229 | 9 | UNKNOWN |
| Bottom hem | Flatlock Coverstitch | OBSERVED (seeded TEST records) | 310 | 8 | UNKNOWN |
| Bottom hem | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 85 | 4 | UNKNOWN |
| Bottom rib / hem | Flatlock Coverstitch | OBSERVED (seeded TEST records) | 83 | 5 | UNKNOWN |
| Buttonhole + button attach | Automated Pattern Tacker | OBSERVED (seeded TEST records) | 71 | 5 | UNKNOWN |
| Buttonhole + button attach | Single-Needle Lockstitch | UNKNOWN (seeded TEST records) | 0 | 0 | UNKNOWN |
| Collar attach | Overlock Serger (4-Thread) | UNKNOWN (seeded TEST records) | 0 | 0 | UNKNOWN |
| Collar make + attach | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 100 | 5 | UNKNOWN |
| Cuff / sleeve rib attach | Flatlock Coverstitch | OBSERVED (seeded TEST records) | 95 | 6 | UNKNOWN |
| Cuff attach | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 77 | 6 | UNKNOWN |
| Fly / zip attach | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 276 | 9 | UNKNOWN |
| Hood / collar attach | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 117 | 9 | UNKNOWN |
| Hood / mock-neck collar make | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 145 | 11 | UNKNOWN |
| Inseam close | Feed-off-the-Arm Special | OBSERVED (seeded TEST records) | 249 | 9 | UNKNOWN |
| Neck rib attach | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 112 | 6 | UNKNOWN |
| Neck topstitch | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 105 | 4 | UNKNOWN |
| Panel serging | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 452 | 20 | UNKNOWN |
| Placket attach | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 108 | 7 | UNKNOWN |
| Placket topstitch | Single-Needle Lockstitch | UNKNOWN (seeded TEST records) | 0 | 0 | UNKNOWN |
| Pocket attach | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 113 | 10 | UNKNOWN |
| Pocket attach / welt | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 293 | 11 | UNKNOWN |
| Shoulder join | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 274 | 13 | UNKNOWN |
| Side seam + armhole close | Feed-off-the-Arm Special | OBSERVED (seeded TEST records) | 94 | 5 | UNKNOWN |
| Side seam + underarm close | Feed-off-the-Arm Special | OBSERVED (seeded TEST records) | 206 | 9 | UNKNOWN |
| Side seam close | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 246 | 8 | UNKNOWN |
| Sleeve attach | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 256 | 8 | UNKNOWN |
| Sleeve hem | Flatlock Coverstitch | OBSERVED (seeded TEST records) | 47 | 6 | UNKNOWN |
| Waistband attach | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 243 | 8 | UNKNOWN |
| Yoke / shoulder join | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 102 | 7 | UNKNOWN |
| Zip attach | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 122 | 13 | UNKNOWN |

A scalar employee/machine pair per lot and parallel execution of distinct lots do not establish a mandatory crew size. No requirement is classified AUTHORITATIVE or silently inferred. Factory-approved operation staffing rules, line mobility, calendar/absence authority, and unresolved execution boundaries remain required.

Detailed pool IDs, every operation identity, line roster ratios, quantities and bounded conflict examples are in the corresponding JSON. No database or scheduling component was modified.
