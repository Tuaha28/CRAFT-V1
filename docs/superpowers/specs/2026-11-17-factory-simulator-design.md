# Factory simulator — design (2026-11-17)

## Intent

The data is a frozen snapshot, so an approved CRAFT plan never "happens". The simulator plays the approved plan
forward on a factory clock so the app behaves like a running factory: output and quality checks appear, machines
break and get repaired, workers are absent, auto-planning re-plans, managers approve, the portal's orders move.
It serves **both** a live demo and a measured experiment for the paper (user, 2026-11-16), with an **on-screen
speed control: pause, 1×, 10×, 60×** (user, 2026-11-17, option C).

## Goals

1. **Factory clock** per dataset + factory: the time the factory is at. Starts at the data's reference time;
   paused, 1× (1 factory hour per real minute), 10×, 60× (1 factory hour per real second); reset.
2. **Production follows the approved plan**: each hour, every planned operation records the pieces the plan
   expected times a performance factor (seeded; most steps a little slower than planned, some faster), as the
   same hourly output and quality-check records a supervisor and inspector would enter.
3. **Disruptions** happen at realistic rates and go through the normal paths: machine breakdowns (reported as
   Machine down, repaired after hours), workers absent for a shift, now and then a line stop or late material.
   With auto-planning on, CRAFT re-plans and a manager approves; the simulator then follows the new plan.
4. **Everything shows it**: Line board, My line, Quality, Problems, Dashboard and the customer portal use the
   factory clock as "now"; the portal's progress counts the simulated output.
5. **Experiment mode**: a command runs the same engine without a wall clock (fixed seeds, many runs) and writes
   planned-vs-actual results for the paper (on-time and lateness as planned vs as executed, schedule adherence,
   effect of breakdown rate).

## Non-goals / anti-goals

- Never touches the original databases; with demo copies on, it may write progress into the `_demo` copies only.
- Never looks real when it is not: every page shows a "Simulated factory clock" badge while it runs.
- CRAFT code is not changed; re-plans are normal CRAFT runs (one heavy run at a time).
- Not a discrete-event model of every worker: it replays the plan's own timing with noise and disruptions.

## Decisions (builder's calls)

- Engine is a pure function `advance(plan, state, t0, t1, rng) -> events` (output, checks, issues to open or
  close), applied by the planning worker every few real seconds; experiment mode calls it directly in a loop.
- Output per operation per factory hour = planned pieces in that hour × performance (lognormal, mean 0.95,
  sd 0.10, drawn once per operation) × availability (0 while its machine is down or its line is stopped;
  reduced while a crew worker is absent). Output an operation could not do is carried forward (later hours),
  so delays build up the way they do on a floor.
- Defects: one check per operation per day, 2–6% defects (seeded).
- Breakdowns: 0.15% per machine per working hour (demo default; experiment varies it); repair 2–8 h.
  Absence: 2% per planned worker per day. Line stop and late material: rare (demo only).
- Issues are opened and closed by a `craft_sim` system user through the existing services, so auto-planning,
  e-mail and the Problems page work unchanged.
- Progress is tracked per order operation across plan changes (outputs of every plan run count).
- Controls (pause/speed/reset) need `planning.approve_plan` or `floor.manage_users`; everyone sees the clock.
- Reset deletes only simulated records (entered by `craft_sim`) for that dataset and factory, after a confirm.

## Experiment output

`deliverables/paper/results/simulation/`: per run and seed — planned vs simulated finish per order, on-time %,
total and mean lateness (planned and executed), schedule adherence (share of planned pieces done in their
planned hour), breakdowns and absences; a summary table across seeds and breakdown rates.
