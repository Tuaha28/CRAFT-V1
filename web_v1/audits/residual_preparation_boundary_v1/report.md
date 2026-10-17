# Residual Preparation Boundary V1

## Decision

RESIDUAL_PREPARATION_BOUNDARY_V1 = PARTIAL
PREPARATION_CONTRACT = PASS
CRAFT_SOLVER_COMPATIBILITY = BLOCKED

The implementation is a deterministic, side-effect-free preparation artifact.
It preserves original operation identity, completed immutable history, in-flight
commitments, residual lot quantities, resource availability, precedence and
provenance. It deliberately remains PREPARED_NOT_ADMITTED.

## Compatibility evidence

The inspected OperationSpec has one complete serial productive duration and no
quantity/lot fields. The CP model creates one interval per logical operation.
ResourceConfiguration is keyed by one logical operation, and the existing
materialization/independent validation path has no contract for reconciling a
completed quantity plus a residual interval. Whole-operation successor release
also cannot be split by this boundary. Existing worker/calendar carry-forward
helpers preserve supplied segments but do not infer or authorize residual work.

Therefore no residual fixture was passed to CP-SAT. Exact replay,
materialization, physical validation and scope validation for a CRAFT candidate
are NOT_RUN, not passing claims.

## Fixture proof

Committed continuation and splittable residual fixtures pass preparation and
independent structural validation. Quantity, residual identity, fixed resource
commitments, calendar coverage, precedence and provenance are checked. Invalid
quantity, future-actual, stale-evidence and resource-overlap fixtures are
rejected. Reordered evidence produces the same preparation hash.

These are compatibility fixtures only and do not resolve the 278 live partial
operations. The live audit remains 0 resolved and 278 unresolved.

## Historical leakage

Event/as-of time remains distinct from evidence capture time. Post-as-of
evidence may be retained when valid for the historical as-of, but future
actual/projection fields are rejected as observed execution.

## Remaining capability

Before live admission, CRAFT needs a residual-capable preparation,
materialization, replay and independent physical-validation contract, or an
explicitly proven whole-operation residual semantics. This task intentionally
does not modify CRAFT core or activate scheduling.

SAFE_TO_RESUME_V2_STEP_2 = NO
