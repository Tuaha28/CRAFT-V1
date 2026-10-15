from __future__ import annotations
import time
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scratch.run_factory1_craft_frozen_v1 import _build_loaded
from crafter.repair.planning import PlanningContextV1, build_planning_state, select_precedence_safe_window
from crafter.repair.resource_configs import generate_resource_configurations
from crafter.repair.calendar_regimes import generate_calendar_regimes_selected, CalendarRegimeCache

loaded, boundary, keys, identity, contract = _build_loaded()
problem = loaded[0]
ctx = PlanningContextV1("window-001", "INITIAL_PLAN", 1, 0, 0, 86400, 20000, (), "FROZEN_BENCHMARK_RAW", None, 0)
t = time.perf_counter()
state = build_planning_state(loaded, ctx, boundary=boundary)
print("BUILD_PLANNING_STATE_SECONDS", time.perf_counter()-t, flush=True)
t = time.perf_counter()
selected, provenance = select_precedence_safe_window(problem, state["after_index"], state["classified"], ctx, committed_operation_keys=boundary.committed_operation_keys, boundary=boundary, maximum_operations=22)
print("SELECT_SECONDS", time.perf_counter()-t, flush=True)
print("SELECTED_COUNT", len(selected), flush=True)
print("SELECTED_HEAD", list(selected)[:30], flush=True)
print("PROVENANCE_COUNT", len(provenance), flush=True)
print("PROVENANCE_SELECTED", sum(1 for x in provenance if x.get("selected")), flush=True)
print("STATE_KEYS", len(state["keys"]), flush=True)

selected_context = PlanningContextV1("window-001", "INITIAL_PLAN", 1, 0, 0, 86400, 20000, selected, "FROZEN_BENCHMARK_RAW", None, 0)
t = time.perf_counter()
state2 = build_planning_state(loaded, selected_context, boundary=boundary)
print("BUILD_SELECTED_STATE_SECONDS", time.perf_counter()-t, flush=True)
t = time.perf_counter()
configs, audit, summary = generate_resource_configurations(state2, max_machine_sets=1, max_worker_crews=3, machine_pool_limit=30)
print("RESOURCE_SECONDS", time.perf_counter()-t, flush=True)
print("RESOURCE_CONFIG_COUNT", sum(len(v) for v in configs.values()), flush=True)
cache = CalendarRegimeCache()
t = time.perf_counter()
regimes, rsummary, total, equiv = generate_calendar_regimes_selected(state2, configs, cache=cache)
print("CALENDAR_SECONDS", time.perf_counter()-t, flush=True)
print("REGIME_COUNT", total, flush=True)
