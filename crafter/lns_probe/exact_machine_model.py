"""Small deterministic physical-machine-set generation for CRAFT E1.6."""
from __future__ import annotations

from collections import defaultdict

from .anchored_model import outside_machine_reservations
from .line_k_model import _available_machine_pool


def _set_rank(machine_ids, reservations):
    rows = []
    for mid in machine_ids:
        intervals = reservations.get(mid, ())
        rows.append((max((int(row[1]) for row in intervals), default=0),
                     len(intervals), int(mid)))
    return tuple(rows)


def generate_machine_set_candidates(problem, index, neighborhood, released_keys,
                                    candidate_domains, *, max_sets=3,
                                    machine_pool_limit=30):
    """Return key -> (line,k) -> exact bounded machine-ID tuples.

    The generator never enumerates combinations.  It keeps the incumbent set
    and at most two deterministic greedy alternatives per LINE+k mode.
    """
    mutable = set(neighborhood.operation_keys)
    released = set(released_keys)
    reservations = outside_machine_reservations(problem, index, mutable)
    output, provenance = {}, {}
    for key in sorted(released):
        operation = problem.operation_by_key[key]
        row = index.by_key[key]
        incumbent = tuple(sorted(int(v) for v in row["machine_ids"]))
        modes = candidate_domains.get(key, ())
        per_mode = {}
        per_prov = {}
        for mode in modes:
            line_id, k = int(mode["line_id"]), int(mode["k"])
            pool = list(_available_machine_pool(problem, operation, line_id))
            pool.sort(key=lambda mid: _set_rank((mid,), reservations))
            if len(pool) < k:
                per_prov[(line_id, k)] = {"removed": "insufficient_usable_machines",
                                          "available": len(pool), "required": k}
                continue
            bounded = list(pool[:max(k, machine_pool_limit)])
            sets = []
            if line_id == int(row["line_id"]) and k == len(incumbent) and set(incumbent).issubset(bounded):
                sets.append(incumbent)
            # Greedy earliest-window set and deterministic shifted alternative.
            for offset in range(0, min(3, max(1, len(bounded)-k+1))):
                candidate = tuple(sorted(bounded[offset:offset+k]))
                if len(candidate) == k and candidate not in sets:
                    sets.append(candidate)
            # A low-transition variant emphasizing the machines with the
            # fewest frozen occupations.
            sparse = tuple(sorted(sorted(bounded, key=lambda mid: (len(reservations.get(mid, ())), mid))[:k]))
            if len(sparse) == k and sparse not in sets:
                sets.append(sparse)
            sets = list(dict.fromkeys(sets))[:max_sets]
            if not sets:
                per_prov[(line_id, k)] = {"removed": "no_bounded_exact_set"}
                continue
            per_mode[(line_id, k)] = sets
            per_prov[(line_id, k)] = {"sets": [list(item) for item in sets],
                                      "pool_size": len(pool), "bounded_pool_size": len(bounded),
                                      "reasons": ["incumbent", "earliest_window", "sparse_frozen_use"][:len(sets)]}
        output[key] = per_mode
        provenance[key] = per_prov
    return output, provenance
