# Auto-planning makes new plans (plan ahead) — design (2026-11-17)

User (2026-11-17): auto-planning should make new plans, not only re-plan ("yes build this").

## Behaviour

For every factory with Auto-planning on (the existing per-factory switch; a new "all factories" switch turns it
on for every factory of the chosen data):

1. **First plan** — no approved plan and none waiting: CRAFT plans the next `AUTO_PLAN_ORDERS` (5) orders in the
   factory's queue (Quick), as `craft_auto`, trigger `FIRST_PLAN`.
2. **Next plan** — the approved plan is at least `AUTO_PLAN_NEXT_AT` (0.8) done (recorded output, any plan run,
   against its pieces): CRAFT plans the approved plan's unfinished orders plus the next 5 orders after its last
   one in the order book, trigger `NEXT_PLAN`.
3. Re-plans after problems/Priority/rush/new orders stay as they are.
4. A manager approves every plan; the existing e-mail goes out when a CRAFT plan is ready.

## Guards

- One heavy run at a time (existing `_busy`); factories are taken smallest first (orders in the data).
- Nothing new for a factory while a plan of it is queued, running or waiting for a decision.
- A first/next plan the manager sent back is not retried for 24 hours.
- Partial progress is not passed to CRAFT (it plans whole orders); the simulator counts output per operation
  across plan runs, so finished pieces are not redone.
