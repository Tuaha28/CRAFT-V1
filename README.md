# garment_ai

Production scheduling for a garment factory, combining a heterogeneous graph
neural network, a genetic-algorithm scheduler, and a fuzzy-logic disruption
agent over a MySQL model of the shop floor.

Scale of the modelled factory: 1,410 active orders, 7,050 operations, 99
production lines, 3,000 machines, 8,302 operators.

## Pipeline

```
MySQL (garment_fyp_db)
   |
   +-- graph/          builds a HeteroData graph: operation / machine / worker
   +-- models/         multi-task GNN: delay risk, completion time, priority
   +-- ga/             genetic-algorithm scheduler + dispatching baselines
   +-- agent/          disruption handling, fuzzy triage, XAI, orchestration
   +-- frontend/       Streamlit test dashboard
```

## Setup

```bash
python -m venv venv
venv\Scripts\activate            # Windows
pip install -r requirements.txt
copy .env.example .env           # then fill in your real DB password
python data_generation/seed_database_fixed.py
```

`.env` is gitignored and must never be committed.

On Windows, run with `PYTHONUTF8=1` if your console is cp1252 — log messages
contain emoji, and while the logger degrades them safely, UTF-8 output is
cleaner.

## Running

Every stage exits 0. Timings are CPU-only torch on 16 GB RAM.

| Command | Time | What it does |
|---|---|---|
| `python -m graph.graph_builder` | ~37s | Build the heterogeneous graph |
| `python -m models.train` | ~14min | 100 epochs, saves best-by-validation checkpoint |
| `python -m models.evaluate` | ~30s | Held-out metrics |
| `python -m ga.scheduler` | ~20s | Small GA smoke test |
| `python -m agent.fuzzy_logic` | instant | Tier scenarios, pure Python, no DB |
| `python -m agent.decision_engine` | ~30min | Full reschedule; writes `outputs/`, one DB row |
| `streamlit run frontend/dashboard.py` | - | Test dashboard (run from the project root) |

Outputs land in `outputs/`: three candidate schedules, an order-completion
rollup per candidate, and `schedule_options_summary.json` comparing them.

## Key design decisions

**Machine-level capacity.** A production line is not a serial resource. Each
operation claims a specific set of machines from its line's pool for its
duration, and other operations run concurrently on the rest. See
`ga/schedule_utils.py::select_machines_and_start`.

**One duration formula.** `calculate_operation_work()` in
`ga/schedule_utils.py` is the single definition, called by the fitness
function, the schedule exporter, and the breakdown assessor. It exists because
three independent copies of this expression previously drifted apart and
double-counted parallelism.

**7% standby machine reserve.** Per machine type, company-wide
(`utils/machine_reserve.py`). Reserved machines are never scheduled; they
exist so a breakdown can be covered without preempting another order. Reserve
membership is deterministic because the scheduler and the breakdown assessor
must resolve the identical set.

**Soft business objectives.** Deadline lateness and labour utilisation are
weighted penalties, not hard constraints — both are infeasible as hard
constraints on this data and would reject the entire search space.

**Dispatching baselines.** `ga/heuristics.py` implements FIFO, EDF, and
critical-ratio. They seed the GA's initial population *and* serve as
benchmarks, scored through the same simulation as the GA so the comparison
cannot drift.

## Project status

See `PROJECT_STATUS.txt` for the current state: what is fixed and verified,
what is outstanding, and the prioritised next steps. Read it before making
changes — several non-obvious defects in this codebase were subtle enough to
survive multiple passes, and the file records what they were and how they were
detected.

Known open items at time of writing: labour utilisation is capped at 33.6% by
a single-shift roster (a three-shift model is planned and lifts the ceiling to
~100%); `api/main.py` is an empty placeholder; there are no automated tests.
