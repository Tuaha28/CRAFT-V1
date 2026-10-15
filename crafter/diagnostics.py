"""JSON-safe diagnostic rendering for experimental Crafter artifacts."""
from __future__ import annotations

from dataclasses import asdict
from enum import Enum


def _safe(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def solution_dict(solution):
    return _safe(asdict(solution))


def phenotype_dict(phenotype):
    return _safe(asdict(phenotype))
