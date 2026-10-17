# Residual Operation Compatibility & Adapter Proof V1

## Result

RESIDUAL_ADAPTER_PROOF_V1 = PARTIAL

The selected representation is an isolated APPROACH_B_OPERATION_LEVEL_RESIDUAL_VIEW.
It preserves the original operation key, immutable completed history, residual lot
ledger, evidence-backed remaining work, resource commitments, precedence and
provenance. It is deliberately not admitted as a CRAFT operation input.

## Actual CRAFT boundary

Inspection of crafter/contracts.py, crafter/model.py and
crafter/repair/resource_configs.py shows that CRAFT accepts one indivisible
operation interval with one complete productive duration. The current resource
configuration and fixed-commitment interfaces preserve resources, but do not
represent residual lot quantities, completed history plus residual work, or a
split successor-release boundary. ga.schedule_utils.split_operation_work and
the calendar/crew helpers require a known work budget; they do not infer live
remaining work. Therefore CRAFT_CORE_COMPATIBILITY = BLOCKED and no solver,
materializer, replay, or production schedule was invoked.

## Proof performed

- Committed-continuation fixture: PASS.
- Splittable-residual fixture: PASS.
- Invalid quantity, projected-actual and resource-overlap fixtures: rejected.
- Quantity ledgers and residual lot IDs are independently checked.
- Reordered input produces the same adapter view and hash.
- Adapter admission remains closed (craft_input_allowed=false).
- Existing live audit remains fail-closed: 278 partial operations, 0 resolved,
  278 unresolved across six factories.

The fixtures prove the view and its invariants only; they do not resolve live
factory operations. Future actual timestamps remain rejected as evidence, and
post-as-of evidence capture is allowed only when its validity window covers the
as-of time.

## Required capability before live admission

A future integration must either extend or wrap CRAFT preparation/validation
with an explicit residual representation that accepts fixed executed history and
validated remaining work while preserving whole-operation precedence and exact
replay. It must also prove finite machine/worker continuation or explicitly
authorize a safely separable residual. This task intentionally does not change
the CRAFT core or activate such a representation.

## Source-of-truth hierarchy and minimum evidence

The adapter follows the established hierarchy: observed execution at the
reference time first; explicit authorized continuation evidence second;
defensible remaining-work estimates with model and assumptions third; and
future projected completion calculated by CRAFT only after admission. Planned
rows and generated future actual timestamps cannot override that order.

Minimum evidence for a future live admission is a reconciled quantity ledger,
lot identity, observed/in-flight state, authorized continuation or explicit
residual-splitting proof, resource assignments and finite availability/occupancy
segments, worker continuity where required, a working-duration source, and
precedence boundaries with provenance and validity. The legacy source currently
does not provide that complete residual packet for its 278 partial operations.

## Safety

Legacy database writes: 0. CRAFT core modified: no. Frozen artifacts modified:
no. SAFE_TO_RESUME_V2_STEP_2 = NO.
