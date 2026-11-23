# Completing operations and orders — design and plan (2026-11-23)

Approved by the user ("ok build it") after the summary in chat: research-based roles, a no-pieces "Mark complete",
order stages Sewn → Final inspection → Packed → Shipped → Closed.

## Behaviour (spec)

**Operations (steps)**, keyed `"<order>_<operation_id>"` per dataset + factory, across plan runs:
- State (derived unless a row says otherwise): NOT_STARTED (no good pieces) → IN_PROGRESS → COMPLETE (good pieces ≥
  step pieces, automatic) or a `StepStatus` row: COMPLETE (marked), SHORT (marked with fewer pieces + reason), OPEN
  (reopened by the manager: stays in progress). PAUSED is shown when an open Machine down issue names one of its machines.
- Good pieces = recorded output − defects of unreworked checks for that step.
- **Mark complete** (supervisor own line, or manager): optional `pieces` (default the step's pieces); fewer → SHORT,
  reason required. Missing pieces are recorded as one OutputEntry by that user (so all counts stay right), logged
  "completed without hourly counts" when nothing was recorded. The previous step of the order must be complete;
  a step's pieces may not exceed the previous step's final pieces.
- `save_output`: no output on a COMPLETE/SHORT step; a step's total may not exceed the previous step's good pieces.
- **Rework**: QC (or supervisor) marks a check's defects reworked → they count again.
- Every action logs a PlanEvent (Activity).

**Orders** (`OrderStatus` per dataset + factory + order): stage SEWN is derived when every planned step is
complete/short; then INSPECT (QC: PASS → QC_PASSED, FAIL → QC_FAILED with a note; re-inspect allowed) → PACKED
(merchandiser or manager; cartons) → SHIPPED (merchandiser or manager; carrier, tracking, date) → CLOSED (manager or
planner; reason required unless shipped). Reopen (manager) returns an order to SEWN (its row removed) or a step to OPEN.
Closed or shipped orders leave plan-ahead's next plans. Customer portal shows Packing / Shipped / carrier / tracking.

**Permissions** (new `floor.complete_step`, `floor.ship_order`, `floor.close_order`): supervisor and manager complete
steps; QC inspects (`floor.record_defect`); merchandiser and manager pack/ship; manager and planner close; manager
reopens (`planning.approve_plan`).

**Demo copies** (write-back on): order SEWN/QC/… → `production_orders.status`='Completed' when sewn; shipped →
`delivery_logistics` carrier/tracking/date/'In_Transit' and `purchase_orders.po_status`='Shipped' when all its
production orders are shipped. Originals never written.

## Tasks

1. **Steps** — models (`StepStatus`, `DefectCheck.reworked_at`, perms + roles), `floor/completion.py`
   (`step_states`, `complete_step`, `reopen_step`, `mark_reworked`), `save_output` rules. Tests `floor/test_completion.py`.
2. **Orders** — `OrderStatus`, `order_stage`, `inspect_order`, `pack_order`, `ship_order`, `close_order`, `reopen_order`;
   plan-ahead skips shipped/closed; writer hooks; portal stages. Tests in the same file + portal.
3. **API** — step complete/reopen, rework, order status/actions, statuses for the Orders page; line day carries step state.
4. **Screens** — My line (Mark complete dialog, state chips, rework), order page actions, Orders/Home chips, portal journey.
5. **Finish** — README, all suites, review, live check with the simulator.
