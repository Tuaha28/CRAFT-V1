"""Conservative aggregate labour screen for the CP-SAT master.

This is only a necessary master constraint.  Exact identity, calendar and
reservation feasibility remains the responsibility of worker_bridge.
"""
from __future__ import annotations

from collections import defaultdict


def capacity_map(problem):
    out = {}
    for row in problem.labour_capacities:
        key = (int(row.factory_id), row.skill.strip().lower())
        if key in out:
            raise ValueError(f"duplicate labour capacity {key}")
        if row.capacity < 0:
            raise ValueError(f"negative labour capacity {key}")
        out[key] = int(row.capacity)
    return out


def fixed_labour_by_pool(problem):
    out = defaultdict(list)
    for row in problem.resources.fixed_worker_commitments:
        out[(int(row.factory_id), row.skill.strip().lower())].append(row)
    return out
