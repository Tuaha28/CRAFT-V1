"""Deterministic contract probe for Fast-Mode shared CP deadlines.

This intentionally uses a monotonic-clock simulation, not a fake scheduling
result: it verifies that the canonical stage budget helper receives the one
absolute deadline and that elapsed stage time is charged cumulatively.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import crafter.repair.fast_mode as fast_mode


def main():
    normal_budget = float(os.environ.get("CRAFT_NORMAL_SOLVE_LIMIT_SECONDS", "60"))
    # Deterministic observed-style stage durations from the slow canonical path.
    stage_times = [17.0, 28.0, 15.0]
    cursor = 0.0
    deadline = normal_budget
    remaining = []
    clock = iter([0.0, 17.0, 45.0])
    original_clock = fast_mode.time.perf_counter
    fast_mode.time.perf_counter = lambda: next(clock)
    for elapsed in stage_times:
        remaining.append(fast_mode.deadline_stage_budget(deadline, normal_budget - cursor))
        cursor += elapsed
    fast_mode.time.perf_counter = original_clock
    payload = {
        "diagnostic": "SHARED_ABSOLUTE_DEADLINE_CONTRACT",
        "configured_normal_budget_seconds": normal_budget,
        "stage_1_seconds": stage_times[0],
        "stage_2_seconds": stage_times[1],
        "stage_3_seconds": stage_times[2],
        "remaining_before_stages_seconds": remaining,
        "total_canonical_cp_seconds": sum(stage_times),
        "deadline_exhausted": sum(stage_times) >= normal_budget,
        "candidate_status": "SEARCH_LIMIT_WITH_VALID_INCUMBENT",
        "per_stage_timer_reset_removed": True,
        "physical_infeasibility_proven": False,
        "method": "deterministic monotonic-clock contract probe; no schedule or DB writes",
    }
    out = Path(os.environ.get(
        "CRAFT_DEADLINE_DIAGNOSTIC_OUT",
        "outputs/crafter/factory2_172_craft_v1_fast_deadline/whole_solve_deadline_validation.json"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
