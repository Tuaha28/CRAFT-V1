"""Evidence helpers for the machine/worker domain factorization audit.

This module intentionally does not replace ``ResourceConfiguration``.  It
records the exact conditions under which candidate *generation* can be
projected into machine and worker dimensions, while preserving paired
configurations for the canonical CP/replay path unless a complete proof is
available.
"""
from __future__ import annotations

from collections import defaultdict


def factorization_projection(configurations):
    """Return line/k projections and the exact paired Cartesian reconstruction."""
    machine = defaultdict(set)
    worker = defaultdict(set)
    paired = defaultdict(set)
    for config in configurations:
        key = (int(config.line_id), int(config.k))
        machine[key].add(tuple(int(x) for x in config.machine_ids))
        worker[key].add(tuple(str(x) for x in config.worker_ids))
        paired[key].add((tuple(int(x) for x in config.machine_ids),
                         tuple(str(x) for x in config.worker_ids)))
    product = {
        key: {(m, w) for m in machine[key] for w in worker[key]}
        for key in machine
    }
    return {
        "machine_sets": {str(k): sorted(v) for k, v in machine.items()},
        "worker_crews": {str(k): sorted(v) for k, v in worker.items()},
        "paired": {str(k): sorted(v) for k, v in paired.items()},
        "cartesian": {str(k): sorted(v) for k, v in product.items()},
        "exact_per_line_k": all(paired[key] == product[key] for key in paired),
    }


def audit_factorization_semantics():
    """Return the repository-level semantic verdict.

    Candidate construction is separable for a fixed line/k under the current
    benchmark rules, but the complete scheduling representation is not
    replaced: regimes, calendars, commitments and sequence constraints still
    require paired materialized choices and exact replay.  This is therefore a
    partial research result, not permission to relax the canonical model.
    """
    return {
        "verdict": "PARTIAL_FACTORIZATION_VALID",
        "candidate_generation_scope": "FIXED_LINE_AND_K_ONLY",
        "proof_obligations": [
            "machine-set eligibility and factory/line ownership are independent of worker identity",
            "worker skill/factory/line eligibility is independent of machine identity",
            "productive duration and setup depend on k/operation semantics, not the machine-worker pair",
            "all machine-set × worker-crew combinations must be rechecked for exact regime legality",
        ],
        "non_factorable_canonical_context": [
            "k and line are shared decisions for both resource dimensions",
            "worker productive segments and pause/resume are crew-calendar specific",
            "machine continuous occupancy and sequence-dependent changeover are machine-specific",
            "committed/reserved intervals can invalidate a pair at a particular start",
            "the existing CP model selects paired regime literals and replay consumes paired assignments",
        ],
        "implementation_decision": "retain_paired_resource_configurations",
        "reason": "No full CP/replay equivalence proof exists for a factorized replacement; paired domains remain authoritative.",
    }

