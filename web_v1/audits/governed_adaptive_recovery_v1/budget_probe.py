"""TEST fault injection: real CP-SAT, deliberately exhausted search budget.

Not an alternative scheduling engine or a performance benchmark. No physical
inputs/constraints are changed. Subsequent retries use the unmodified worker.
"""
from dataclasses import replace
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from web_v1.audits.factory1_realistic_neighborhood_v1 import run_bounded
from web_v1.backend.integrations.craft.factory1_cycles import advance


def main():
    original = run_bounded.run_adaptive_planning_schedule

    def exhausted_budget(state, **kwargs):
        kwargs['policy'] = replace(kwargs['policy'], max_expansion_rounds=0)
        kwargs['canonical_plan_factory'] = lambda m, w: ((m * w, None, 0.000001),)
        kwargs['objective_mode'] = False
        return original(state, **kwargs)

    run_bounded.run_adaptive_planning_schedule = exhausted_budget
    result = advance(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8')))
    first = result['checkpoint']['windows'][0]
    assert first['status'] == 'REJECTED', first
    assert any(stage['status'] == 'UNKNOWN' for stage in first['stages']), first['stages']
    result['fault_injection'] = 'REAL_CP_SAT_SEARCH_BUDGET_1_MICROSECOND_NO_PHYSICAL_CHANGE'
    Path(sys.argv[2]).write_text(json.dumps(result, sort_keys=True, default=str), encoding='utf-8')


if __name__ == '__main__':
    main()
