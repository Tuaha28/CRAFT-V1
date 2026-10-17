# Residual Execution End-to-End Compatibility Proof V1

## Decision

RESIDUAL_END_TO_END_PROOF_V1 = PARTIAL
RESIDUAL_SOLVER_INPUT = BLOCKED
ISOLATED_CP_SAT_SOLVE = NOT_RUN
RESIDUAL_MATERIALIZATION = NOT_RUN
EXACT_REPLAY = NOT_RUN
INDEPENDENT_PHYSICAL_VALIDATION = PASS for preparation structure only
SCOPE_VALIDATION = NOT_RUN

## Why the vertical slice is blocked

The existing CRAFT model can syntactically accept a positive remaining duration
in OperationSpec, but that interval is then treated as the complete logical
operation. OperationAssignment, selected schedule rows, materialization, replay
and independent physical validation have no required/completed/remaining
quantity ledger or residual-to-lot mapping. A CP-SAT candidate would therefore
not prove that completed production was excluded or that the output represents
the remaining quantity. Running it would be a false success.

Whole-operation predecessor/successor semantics are also the only supported
boundary. Split-successor behavior is rejected.

## Fixture evidence

The controlled committed and uncommitted residual fixtures pass preparation,
quantity, resource/calendar, provenance and whole-operation precedence checks.
Contradictory quantities, projected future actuals, stale evidence, and
resource conflicts are rejected. Reordered input produces an identical
preparation hash. No solver or live schedule was invoked.

The live audit remains 278 partial operations, 0 resolved and 278 unresolved.

## Minimum change proposal

Keep CP-SAT mathematics unchanged, but add an independently specified boundary
around materialization, replay and physical/scope validation. It must carry the
original operation/lot identity, completed and residual quantities, validated
remaining work, fixed resource segments and whole-operation completion boundary.
The independent validator must reconcile quantities and map every output row back
to source lots. Only after that proof may craft_input_allowed be enabled for a
controlled residual fixture.

## Safety

Legacy database writes: 0. CRAFT core modified: no. Frozen artifacts modified:
no. Live scheduling and promotion: none. SAFE_TO_RESUME_V2_STEP_2 = NO.
