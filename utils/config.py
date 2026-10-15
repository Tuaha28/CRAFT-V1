import os
import torch
from urllib.parse import quote_plus
from dotenv import load_dotenv

# Load environment variables from .env file if present
load_dotenv()

# Base Directory Paths (OS Agnostic for Windows/Linux)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(BASE_DIR, "logs")
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")
VECTOR_DB_DIR = os.path.join(BASE_DIR, "data", "vector_store") # For RAG Agent

# Ensure directories exist
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(VECTOR_DB_DIR, exist_ok=True)

# Database Configuration
# NOTE: DB_PASS has no hardcoded fallback on purpose — a real password should
# only ever live in a local .env file (which must be in .gitignore), never in
# source that gets pushed to GitHub. If DB_PASS is missing, fail loudly
# instead of silently using a default.
DB_USER = os.getenv("DB_USER", "root")
DB_PASS = os.getenv("DB_PASS")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_NAME = os.getenv("DB_NAME", "garment_fyp_db")
DB_PORT = os.getenv("DB_PORT", "3306")

if not DB_PASS:
    raise RuntimeError(
        "DB_PASS is not set. Create a .env file in the project root with "
        "DB_USER, DB_PASS, DB_HOST, DB_NAME, DB_PORT — see .env.example."
    )

# Safely URL-encode password
encoded_pass = quote_plus(DB_PASS)
DATABASE_URL = f"mysql+pymysql://{DB_USER}:{encoded_pass}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# Factory Operational Constants
#
# The factory runs THREE 8-hour shifts back to back — 06:00-14:00,
# 14:00-22:00, 22:00-06:00 — which tile the full 24 hours. Machines are
# therefore continuously available and there is no window to snap work into,
# so these are set to a full day and ga/schedule_utils.py::snap_to_shift_window
# becomes a no-op.
#
# These were 6 and 22, describing a single 16-hour day. That understated
# machine availability by a third and did not match the roster.
#
# Set them to a real window (e.g. 6 and 22) to reinstate shift snapping if the
# factory ever stops running nights. The individual shift BANDS live in
# database/queries.py, where operators are matched to the hour they work —
# these two values are only about when machines can run.
SHIFT_START_HOUR = 0
SHIFT_END_HOUR = 24

# ---------------------------------------------------------
# FYP SPECIFIC CONFIGURATIONS (GNN, RAG, MULTI-AGENT)
# ---------------------------------------------------------

# Deep Learning / GNN Config
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
GNN_HIDDEN_CHANNELS = int(os.getenv("GNN_HIDDEN_CHANNELS", 64))
# WAS 100 -- found during the model-module audit (see chat) to have never
# matched what was actually being measured. Every fair-fight comparison
# and the 0H.15 methodology it follows trained for 150 epochs; production
# (run_final_pipeline.py's do_train() calls train_gnn() with no epochs
# argument, i.e. this default) was silently training a DIFFERENT, shorter
# run than every number that had been reported about the model. Checked
# directly against a real 150-epoch run's per-epoch log before changing
# this: at epoch 100, val_total was 0.3974 and still improving; the real
# optimum (best-val-loss checkpoint) didn't arrive until epoch 147 (0.3875)
# -- a 2.3% relative gap left on the table by the old default. 150 chosen
# to match every existing measurement exactly, not a fresh guess -- the
# same run showed val_total flattening/reversing slightly by epoch 148-150,
# so there's no evidence more epochs would help further.
GNN_EPOCHS = int(os.getenv("GNN_EPOCHS", 150))
GNN_LEARNING_RATE = float(os.getenv("GNN_LEARNING_RATE", 0.005))
GNN_DROPOUT = float(os.getenv("GNN_DROPOUT", 0.3))

# Multi-Agent / Genetic Algorithm Scheduler Config
#
# BUG FIX (PIPELINE_METHODOLOGY_TRACE.md Stage 10 / FULL_CODEBASE_AUDIT.md):
# these two were previously dead -- run_final_pipeline.py hardcoded its own
# POP, GENS = 50, 50 module-level constant, and agent/decision_engine.py's
# trigger_rescheduling() separately hardcoded pop_size = 50, generations = 50
# locally, and GA_GENERATIONS's old default (100) never matched what either
# call site actually ran. Both call sites now import these instead of
# hardcoding their own copy; the defaults below are set to 50/50 to match
# what has actually been run and reported throughout this project's history,
# not reset to some other value under this fix.
GA_POPULATION_SIZE = int(os.getenv("GA_POPULATION_SIZE", 50))
GA_GENERATIONS = int(os.getenv("GA_GENERATIONS", 50))

# GA_MUTATION_RATE intentionally REMOVED (was here, unused, looked like a
# real knob and wasn't -- PIPELINE_METHODOLOGY_TRACE.md Stage 10 /
# FULL_CODEBASE_AUDIT.md). The per-gene mutation probability is derived
# dynamically from problem size instead: ga/scheduler.py::GAScheduler.__init__
# sets self.mutation_indpb = min(0.1, MUTATIONS_PER_INDIVIDUAL / num_ops)
# (MUTATIONS_PER_INDIVIDUAL = 6.0), targeting ~6 expected gene mutations per
# individual regardless of chromosome length -- a fixed rate here would have
# meant ~600+ mutations per individual on the real ~7,000-operation problem
# (pure randomisation, not mutation) or an ineffectually low rate on a small
# test problem. See ga/scheduler.py for the actual, live mechanism.

# Order-Level Completion Quoting Config
# Extra buffer (in days, on top of each operation's own GNN-delay-risk
# buffer) added before an order's projected completion date is quoted to a
# customer. Separate from ga.fitness.CHANGEOVER_MINUTES / the per-operation
# cushion — this is a coarser, order-level safety margin.
ORDER_QUOTE_CUSHION_DAYS = float(os.getenv("ORDER_QUOTE_CUSHION_DAYS", 2.0))

# Maximum number of machines the GA is allowed to assign to a single
# operation running in parallel (see ga/chromosome.py's MC gene). Bounded
# further at runtime by however many eligible machines actually exist on
# the operation's assigned line — this is just the business-policy ceiling
# on top of that. Matches the real per-operation lot count used when this
# factory's data was generated (N_LOTS=60 in the seed script) as an upper
# sanity bound; 40 is the default business preference.
GA_MAX_MACHINES_PER_OPERATION = int(os.getenv("GA_MAX_MACHINES_PER_OPERATION", 40))

# How many parallel lots a production order's quantity is actually split
# into on the shop floor (matches N_LOTS in seed_database.py). The GA
# schedules at OPERATION granularity, not lot granularity (see the earlier
# design discussion on graph/lot-level modeling) — but the underlying
# duration math still needs to reflect that a real operation's total
# quantity runs as ~60 parallel lots, not as one undivided block on a
# handful of machines. Without this scaling, serial_processing_mins used
# an order's FULL target_quantity as if a single line's few eligible
# machines had to grind through all of it serially — producing multi-day/
# multi-week durations for a single operation. See ga/scheduler.py.
GA_LOTS_PER_OPERATION = int(os.getenv("GA_LOTS_PER_OPERATION", 60))

# THE duration calibration factor, shared by the data generator and the
# scheduler. ONE definition -- this is the only place it is declared.
#
# WHY THIS MOVED HERE: the GA's duration formula
# (ga/scheduler.py::serial_processing_mins) omitted this factor entirely
# while the seeder applied it (data_generation/seed_database_fixed.py),
# so the GA planned every operation at 1/0.8255 = 1.21x the duration the
# GNN was actually trained on. Two files computing the same quantity
# differently is the exact failure this codebase has already paid for once
# (the double-division bug); the fix is for there to be one definition and
# for both consumers to import it.
#
# PROVENANCE: derived from real data (DATA_AUDIT.md Module 5) as
#   real_median_min_per_unit * mean_real_efficiency / mean_sam_per_step
# against PPC-1.xlsx's real median of 2.51 min/unit. Replaced an invented
# 1.3 and a separate undocumented /50.0 divisor, neither of which had any
# cited derivation.
#
# IMPORTANT -- the seeder RE-DERIVES this from the source spreadsheets on
# every seed run and does not read this value for its own math. It does
# compare against it and warn loudly on divergence, so the two can never
# drift apart silently. If you reseed and see that warning, update this
# value to the newly derived one: the GA must plan against the same
# calibration the database was actually generated with.
DURATION_CALIBRATION_FACTOR = float(os.getenv("DURATION_CALIBRATION_FACTOR", 0.8255))

# ---------------------------------------------------------
# BUSINESS CONSTRAINTS
# ---------------------------------------------------------

# Fraction of machines held back as a standby reserve, applied PER MACHINE
# TYPE COMPANY-WIDE (not per line — with ~6 machines of a type on a line, a
# per-line 7% would round to zero on almost every line and the policy would
# quietly evaporate). Reserved machines are never assigned to scheduled work;
# they exist so a breakdown can be covered immediately without preempting
# another order. See utils/machine_reserve.py for how membership is chosen
# and agent/impact_assessor.py for the breakdown path that draws on them.
#
# Sized with integer arithmetic rather than round(0.07 * n): in float,
# 0.07 * 600 evaluates to 42.000000000000006, so a ceil() would reserve 43
# and disagree with "93% of the type's total" (558). Integer math keeps the
# reserve count and the usable cap exactly complementary.
MACHINE_RESERVE_FRACTION = float(os.getenv("MACHINE_RESERVE_FRACTION", 0.07))

# Weight on the idle-labour penalty in ga/fitness.py. Deliberately heavy —
# labour utilisation is a primary business objective here — but SOFT, for the
# same reason customer deadlines are soft: a hard "no idle operators"
# constraint is trivially infeasible (this factory has 8,302 present
# operators and only ~2,790 usable machines, so at least two thirds of the
# workforce is idle in ANY schedule) and would reject every candidate.
# Expressed on the same hours scale as the other fitness terms: a schedule
# that leaves the usable machine pool completely idle costs the equivalent of
# this many hours of makespan.
IDLE_LABOR_WEIGHT = float(os.getenv("IDLE_LABOR_WEIGHT", 480.0))

# Rolling planning horizon: how many orders (earliest release date first) a
# single scheduling pass covers. None schedules the entire book.
#
# 300 chosen from a measured sweep of 100 / 300 / 600 / full, then CORRECTED
# by a rolling multi-window validation. Both numbers matter, because the first
# pass was misleading:
#
# The isolated single-window sweep suggested 41.9 orders/day for a 300-order
# window against 12.0 for the full book, implying a 3.5x throughput win. That
# extrapolation was WRONG by a factor of 3.4. It multiplied window 1's
# makespan by the window count, and window 1 is the only one that starts on an
# idle floor. Running five consecutive windows and carrying real machine
# busy-times across the boundaries gives:
#
#                       full book      rolling 300
#   throughput          12.0 /day      12.4 /day     <- no real difference
#   makespan            110.3 d        107.2 d
#   ON-TIME RATE        26.7%          52.6%         <- the actual benefit
#   compute per pass    813 s          ~250 s
#
# So the horizon is kept for SEQUENCING DISCIPLINE, not speed. Windowing by
# release date forces earlier-released orders -- which generally carry earlier
# deadlines -- to be scheduled and finished first, where a full-book pass
# freely interleaves all 1,328 and pushes early-deadline work behind later
# work. Same total time, far better ordering. The shorter per-pass runtime is
# a secondary benefit: you can re-plan a window several times a day.
PLANNING_HORIZON_ORDERS = (
    None if os.getenv("PLANNING_HORIZON_ORDERS", "").strip().lower() in ("none", "0")
    else int(os.getenv("PLANNING_HORIZON_ORDERS", 300))
)

# RAG Stock Intelligence Agent Config
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2") # Fast, open-source fallback