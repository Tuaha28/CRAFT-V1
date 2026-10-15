"""Optional, dependency-free CP-SAT hint contract for Crafter."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from .model import EXPANDED_PAIRWISE


@dataclass(frozen=True)
class OperationHint:
    operation_key: str
    start_seconds: int
    line_id: int
    machine_ids: Tuple[int, ...]


@dataclass(frozen=True)
class CrafterHint:
    source: str
    operations: Tuple[OperationHint, ...]


def apply_hint(problem, artifacts, hint: CrafterHint):
    """Add a complete incumbent hint; never add or relax a constraint."""
    by_key = {row.operation_key: row for row in hint.operations}
    expected = set(problem.operation_by_key)
    if set(by_key) != expected:
        missing = sorted(expected - set(by_key))
        extra = sorted(set(by_key) - expected)
        raise ValueError(f"Crafter hint must be complete; missing={missing}, extra={extra}")
    model = artifacts.model
    for operation_key, row in by_key.items():
        modes = artifacts.modes[operation_key]
        selected_count = len(set(row.machine_ids))
        chosen = [i for i, mode in enumerate(modes)
                  if mode.line_id == row.line_id and mode.machine_count == selected_count]
        if len(chosen) != 1:
            raise ValueError(f"hint has no unique legal line/count mode for {operation_key}")
        chosen_index = chosen[0]
        mode = modes[chosen_index]
        if set(row.machine_ids) - set(mode.candidate_machine_ids):
            raise ValueError(f"hint contains ineligible physical machine for {operation_key}")
        model.AddHint(artifacts.starts[operation_key], int(row.start_seconds))
        model.AddHint(
            artifacts.ends[operation_key], int(row.start_seconds + mode.duration_seconds))
        for index, _mode in enumerate(modes):
            model.AddHint(
                artifacts.mode_literals[(operation_key, index)],
                int(index == chosen_index),
            )
        if artifacts.formulation == EXPANDED_PAIRWISE:
            for index, candidate_mode in enumerate(modes):
                for machine_id in candidate_mode.candidate_machine_ids:
                    model.AddHint(
                        artifacts.machine_literals[(operation_key, index, machine_id)],
                        int(index == chosen_index and machine_id in row.machine_ids),
                    )
        else:
            candidate_ids = {machine_id for candidate_mode in modes
                             for machine_id in candidate_mode.candidate_machine_ids}
            for machine_id in candidate_ids:
                model.AddHint(
                    artifacts.machine_literals[(operation_key, machine_id)],
                    int(machine_id in row.machine_ids),
                )
