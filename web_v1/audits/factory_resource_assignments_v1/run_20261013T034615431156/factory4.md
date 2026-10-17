# Factory 4 resource assignment audit

| Metric | Result |
| --- | --- |
| Operations / orders | 94 / 12 |
| Workers / machines / lines | 102 / 69 / 2 |
| Operations with TEST machine / worker eligibility pools | 94 / 94 |
| Historical lots / future lots excluded | 572 / 1835 |
| Operations with historical assignments | 31 |
| Operations with multiple simultaneous lot workers | 31 |
| Authoritative crew requirements / proven scheduling-ready operations | 0 / 0 |
| Source admission classifications | {'BLOCKED': 3, 'COMPLETED': 28, 'REQUIRES_EVIDENCE': 63} |

Eligibility uses captured factory/line/capability/skill records and the existing code taxonomy. It is not evidence of approved staffing or complete legal line mobility. Historical means clipped before the dataset reference time; the source is generated TEST data, not verified factory actuals.

## Resource pools and potential bottlenecks

| Machine requirement | Machines | Skilled active operators | Same-line batch candidates | Nominal SAM × quantity minutes |
| --- | ---: | ---: | ---: | ---: |
| Automated Pattern Tacker | 10 | 22 | 22 | 17989.8 |
| Feed-off-the-Arm Special | 12 | 29 | 29 | 18816.4 |
| Flatlock Coverstitch | 7 | 24 | 24 | 22986.92 |
| Overlock Serger (4-Thread) | 23 | 38 | 38 | 46145.76 |
| Single-Needle Lockstitch | 17 | 30 | 30 | 44657.44 |

Nominal workload is a demand screen across all captured operations, including completed work. It is not residual workload, utilization, elapsed duration, or proven bottleneck capacity; crew and calendar authority are missing.

## Assignment and calendar checks

Issues: `{'RECORDED_BATCH_LINE_OR_MEMBERSHIP_MISMATCH': 1329, 'ASSIGNMENT_OUTSIDE_WORKER_BATCH_LINE_POLICY_UNKNOWN': 1329, 'HISTORICAL_WHOLE_OPERATION_PRECEDENCE': 27, 'WIP_AFTER_REFERENCE_NOT_CURRENT_EVIDENCE': 572}`.

Model calendar segment checks: `{'pass': 600, 'fail': 0, 'unknown': 0}`. Explicit schedule dates: `['2026-09-10']`.

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
| Armhole binding | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 8 | 2 | UNKNOWN |
| Bartack reinforcement | Automated Pattern Tacker | OBSERVED (seeded TEST records) | 31 | 2 | UNKNOWN |
| Belt loop attach | Automated Pattern Tacker | OBSERVED (seeded TEST records) | 32 | 2 | UNKNOWN |
| Bottom hem | Flatlock Coverstitch | OBSERVED (seeded TEST records) | 73 | 4 | UNKNOWN |
| Bottom hem | Single-Needle Lockstitch | UNKNOWN (seeded TEST records) | 0 | 0 | UNKNOWN |
| Buttonhole + button attach | Automated Pattern Tacker | UNKNOWN (seeded TEST records) | 0 | 0 | UNKNOWN |
| Collar make + attach | Single-Needle Lockstitch | UNKNOWN (seeded TEST records) | 0 | 0 | UNKNOWN |
| Cuff attach | Single-Needle Lockstitch | UNKNOWN (seeded TEST records) | 0 | 0 | UNKNOWN |
| Fly / zip attach | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 33 | 3 | UNKNOWN |
| Inseam close | Feed-off-the-Arm Special | OBSERVED (seeded TEST records) | 33 | 2 | UNKNOWN |
| Neck rib attach | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 41 | 3 | UNKNOWN |
| Neck topstitch | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 41 | 7 | UNKNOWN |
| Panel serging | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 33 | 3 | UNKNOWN |
| Placket attach | Single-Needle Lockstitch | UNKNOWN (seeded TEST records) | 0 | 0 | UNKNOWN |
| Pocket attach / welt | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 33 | 3 | UNKNOWN |
| Shoulder join | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 41 | 4 | UNKNOWN |
| Side seam + armhole close | Feed-off-the-Arm Special | UNKNOWN (seeded TEST records) | 0 | 0 | UNKNOWN |
| Side seam + underarm close | Feed-off-the-Arm Special | OBSERVED (seeded TEST records) | 41 | 4 | UNKNOWN |
| Side seam close | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 33 | 3 | UNKNOWN |
| Sleeve attach | Overlock Serger (4-Thread) | OBSERVED (seeded TEST records) | 33 | 4 | UNKNOWN |
| Sleeve hem | Flatlock Coverstitch | OBSERVED (seeded TEST records) | 33 | 4 | UNKNOWN |
| Waistband attach | Single-Needle Lockstitch | OBSERVED (seeded TEST records) | 33 | 4 | UNKNOWN |
| Yoke / shoulder join | Single-Needle Lockstitch | UNKNOWN (seeded TEST records) | 0 | 0 | UNKNOWN |

A scalar employee/machine pair per lot and parallel execution of distinct lots do not establish a mandatory crew size. No requirement is classified AUTHORITATIVE or silently inferred. Factory-approved operation staffing rules, line mobility, calendar/absence authority, and unresolved execution boundaries remain required.

Detailed pool IDs, every operation identity, line roster ratios, quantities and bounded conflict examples are in the corresponding JSON. No database or scheduling component was modified.
