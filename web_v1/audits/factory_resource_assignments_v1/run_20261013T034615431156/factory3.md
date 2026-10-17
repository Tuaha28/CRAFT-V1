# Factory 3 resource assignment audit

| Metric | Result |
| --- | --- |
| Operations / orders | 1329 / 156 |
| Workers / machines / lines | 1338 / 339 / 11 |
| Operations with TEST machine / worker eligibility pools | 1329 / 1329 |
| Historical lots / future lots excluded | 2073 / 21983 |
| Operations with historical assignments | 125 |
| Operations with multiple simultaneous lot workers | 107 |
| Authoritative crew requirements / proven scheduling-ready operations | 0 / 0 |
| Source admission classifications | {'BLOCKED': 45, 'COMPLETED': 80, 'REQUIRES_EVIDENCE': 1204} |

Eligibility uses captured factory/line/capability/skill records and the existing code taxonomy. It is not evidence of approved staffing or complete legal line mobility. Historical means clipped before the dataset reference time; the source is generated TEST data, not verified factory actuals.

## Resource pools and potential bottlenecks

| Machine requirement | Machines | Skilled active operators | Same-line batch candidates | Nominal SAM × quantity minutes |
| --- | ---: | ---: | ---: | ---: |
| Automated Pattern Tacker | 48 | 391 | 391 | 213840.23 |
| Feed-off-the-Arm Special | 42 | 356 | 356 | 155715.92 |
| Flatlock Coverstitch | 42 | 369 | 369 | 181538.16 |
| Overlock Serger (4-Thread) | 98 | 412 | 412 | 391862.49 |
| Single-Needle Lockstitch | 109 | 364 | 364 | 422250.92 |

Nominal workload is a demand screen across all captured operations, including completed work. It is not residual workload, utilization, elapsed duration, or proven bottleneck capacity; crew and calendar authority are missing.

## Assignment and calendar checks

Issues: `{'RECORDED_BATCH_LINE_OR_MEMBERSHIP_MISMATCH': 21994, 'ASSIGNMENT_OUTSIDE_WORKER_BATCH_LINE_POLICY_UNKNOWN': 21994, 'HISTORICAL_WHOLE_OPERATION_PRECEDENCE': 105, 'WIP_AFTER_REFERENCE_NOT_CURRENT_EVIDENCE': 2068}`.

Model calendar segment checks: `{'pass': 2112, 'fail': 0, 'unknown': 0}`. Explicit schedule dates: `['2026-09-10']`.

| Conflict check | Overlap pairs | Resources affected |
| --- | ---: | ---: |
| worker_span | 0 | 0 |
| machine_span | 0 | 0 |
| worker_productive | 0 | 0 |
| machine_productive | 0 | 0 |
| planned_worker_span | 0 | 0 |
| planned_machine_span | 0 | 0 |

Span overlap includes pauses; productive overlap means actual productive segments overlap. Planned assignments are checked separately and do not become approved commitments. Current machine status and leave flags cannot establish historical breakdown or attendance intervals.

## Staffing evidence for every operation type

| Operation | Machine type | Evidence | Historical lots | Max concurrent lot workers | Required crew |
| --- | --- | --- | ---: | ---: | --- |
| Armhole binding | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 45 | 4 | UNKNOWN |
| Bartack (pocket corners, zip base) | Automated Pattern Tacker | OBSERVED (seeded TEST records) | 16 | 3 | UNKNOWN |
| Bartack reinforcement | Automated Pattern Tacker | OBSERVED (seeded TEST records) | 89 | 6 | UNKNOWN |
| Belt loop attach | Automated Pattern Tacker | OBSERVED (seeded TEST records) | 101 | 6 | UNKNOWN |
| Bottom hem | Flatlock Coverstitch | OBSERVED (seeded TEST records) | 184 | 7 | UNKNOWN |
| Bottom hem | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 7 | 2 | UNKNOWN |
| Bottom rib / hem | Flatlock Coverstitch | OBSERVED (seeded TEST records) | 16 | 2 | UNKNOWN |
| Buttonhole + button attach | Automated Pattern Tacker | OBSERVED (seeded TEST records) | 4 | 3 | UNKNOWN |
| Buttonhole + button attach | Single-Needle Lockstitch | UNKNOWN (seeded TEST records) | 0 | 0 | UNKNOWN |
| Collar attach | Overlock Serger (4-Thread) | UNKNOWN (seeded TEST records) | 0 | 0 | UNKNOWN |
| Collar make + attach | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 12 | 2 | UNKNOWN |
| Cuff / sleeve rib attach | Flatlock Coverstitch | OBSERVED (seeded TEST records) | 16 | 3 | UNKNOWN |
| Cuff attach | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 4 | 2 | UNKNOWN |
| Fly / zip attach | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 142 | 7 | UNKNOWN |
| Hood / collar attach | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 16 | 2 | UNKNOWN |
| Hood / mock-neck collar make | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 17 | 3 | UNKNOWN |
| Inseam close | Feed-off-the-Arm Special | OBSERVED (seeded TEST records) | 139 | 8 | UNKNOWN |
| Neck rib attach | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 114 | 7 | UNKNOWN |
| Neck topstitch | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 102 | 7 | UNKNOWN |
| Panel serging | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 178 | 16 | UNKNOWN |
| Placket attach | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 19 | 3 | UNKNOWN |
| Placket topstitch | Single-Needle Lockstitch | UNKNOWN (seeded TEST records) | 0 | 0 | UNKNOWN |
| Pocket attach | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 16 | 2 | UNKNOWN |
| Pocket attach / welt | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 145 | 7 | UNKNOWN |
| Shoulder join | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 138 | 10 | UNKNOWN |
| Side seam + armhole close | Feed-off-the-Arm Special | OBSERVED (seeded TEST records) | 11 | 2 | UNKNOWN |
| Side seam + underarm close | Feed-off-the-Arm Special | OBSERVED (seeded TEST records) | 115 | 4 | UNKNOWN |
| Side seam close | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 135 | 7 | UNKNOWN |
| Sleeve attach | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 84 | 7 | UNKNOWN |
| Sleeve hem | Flatlock Coverstitch | OBSERVED (seeded TEST records) | 51 | 3 | UNKNOWN |
| Waistband attach | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 128 | 6 | UNKNOWN |
| Yoke / shoulder join | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 13 | 2 | UNKNOWN |
| Zip attach | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 16 | 4 | UNKNOWN |

A scalar employee/machine pair per lot and parallel execution of distinct lots do not establish a mandatory crew size. No requirement is classified AUTHORITATIVE or silently inferred. Factory-approved operation staffing rules, line mobility, calendar/absence authority, and unresolved execution boundaries remain required.

Detailed pool IDs, every operation identity, line roster ratios, quantities and bounded conflict examples are in the corresponding JSON. No database or scheduling component was modified.
