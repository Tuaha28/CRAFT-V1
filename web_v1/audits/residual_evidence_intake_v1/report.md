# Residual Evidence Intake V1

Status: **PARTIAL / FAIL-CLOSED**

## Source-of-truth hierarchy

1. Observed execution at the dataset reference time from timestamped lot execution evidence.
2. Explicit, authorized continuation evidence with provenance and validity window.
3. Defensible remaining-work estimate with documented model/assumptions.
4. CRAFT computes future projected completion through its existing calendar/resource model.

Status strings, planned rows, generated future actuals and lot-count fractions cannot override this hierarchy.

## Results

- Live partial operations: **278**
- Live operations resolved: **0**
- Live operations unresolved: **278**
- Committed fixture: **PASS**
- Splittable fixture: **PASS**
- Invalid fixtures rejected: **YES**
- CRAFT compatibility: **BLOCKED** (current operation-level interface cannot represent residual lot quantity independently).

## Integration path

Evidence can be captured in `craft_web_db` as an authorized, versioned packet referencing immutable legacy IDs and a source snapshot hash. The adapter must validate quantities, resources, calendars, provenance and precedence before any CRAFT preparation. It must never write back to `garment_fyp_db`.

The next integration step is an explicit residual-operation representation/adapter contract. Until it is implemented and independently validated, accepted evidence remains an audit artifact and cannot be passed to live CRAFT.

Legacy DB writes: 0. CRAFT core modified: no. Frozen artifacts modified: no.
