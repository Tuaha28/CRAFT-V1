"""Factory-local deterministic and matched-control neighborhood selection."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, asdict
import random


@dataclass(frozen=True)
class Neighborhood:
    strategy: str
    seed: int
    target_size: int
    factory_id: int
    seed_operation: str
    operation_keys: tuple[str, ...]
    order_ids: tuple[str, ...]
    line_ids: tuple[int, ...]
    capabilities: tuple[str, ...]
    start_seconds: int
    end_seconds: int

    def as_dict(self):
        return asdict(self)


def _factory_keys(index, factory_id):
    return {
        key for key, row in index.operation_features.items()
        if int(row["factory_id"]) == int(factory_id)
    }


def _expand(index, seed_key, target_size, *, randomizer=None,
            preferred_window=None):
    """Expand over realized adjacency; use nearest-time fallback if disconnected."""
    factory_id = int(index.operation_features[seed_key]["factory_id"])
    allowed = _factory_keys(index, factory_id)
    selected = set(index.order_operations[
        index.operation_features[seed_key]["order_id"]]) & allowed
    frontier = deque(sorted(selected))
    while frontier and len(selected) < target_size:
        key = frontier.popleft()
        candidates = [item for item in index.adjacency.get(key, ())
                      if item in allowed and item not in selected]
        if randomizer:
            randomizer.shuffle(candidates)
        else:
            candidates.sort(key=lambda item: (-index.operation_scores[item], item))
        for item in candidates:
            selected.add(item)
            frontier.append(item)
            if len(selected) >= target_size:
                break
    if len(selected) < target_size:
        seed_feature = index.operation_features[seed_key]
        centre = (seed_feature["start_seconds"]+seed_feature["end_seconds"])/2
        remaining = list(allowed-selected)
        if preferred_window:
            window_centre = sum(preferred_window)/2
        else:
            window_centre = centre
        remaining.sort(key=lambda item: (
            abs((index.operation_features[item]["start_seconds"]+
                 index.operation_features[item]["end_seconds"])/2-window_centre),
            -index.operation_scores[item] if not randomizer else 0,
            item,
        ))
        if randomizer:
            # Preserve temporal coherence while randomizing within small bands.
            for offset in range(0, len(remaining), 8):
                band = remaining[offset:offset+8]
                randomizer.shuffle(band)
                remaining[offset:offset+8] = band
        selected.update(remaining[:max(0, target_size-len(selected))])
    if len(selected) > target_size:
        # Always retain the seed; prioritize coupled/high-scoring operations.
        others = list(selected-{seed_key})
        others.sort(key=lambda item: (
            0 if item in index.adjacency.get(seed_key, ()) else 1,
            -index.operation_scores[item], item))
        selected = {seed_key, *others[:target_size-1]}
    return tuple(sorted(selected))


def _make(strategy, seed, target_size, seed_key, keys, index):
    features = [index.operation_features[key] for key in keys]
    factories = {int(item["factory_id"]) for item in features}
    if len(factories) != 1:
        raise AssertionError("neighborhood crossed factory ownership")
    return Neighborhood(
        strategy=strategy, seed=int(seed), target_size=int(target_size),
        factory_id=factories.pop(), seed_operation=seed_key,
        operation_keys=keys,
        order_ids=tuple(sorted({str(item["order_id"]) for item in features})),
        line_ids=tuple(sorted({int(item["line_id"]) for item in features})),
        capabilities=tuple(sorted({str(item["capability"]) for item in features})),
        start_seconds=min(int(item["start_seconds"]) for item in features),
        end_seconds=max(int(item["end_seconds"]) for item in features),
    )


def guided_neighborhood(index, target_size, seed=42, rank=0):
    candidates = sorted(index.operation_scores, key=lambda key: (
        -index.operation_scores[key], key))
    late = [key for key in candidates
            if index.operation_features[key]["order_tardiness_seconds"] > 0]
    if not late:
        # Tiny semantic fixtures may have no late orders. Keep the selector
        # deterministic and use its strongest pressure/scarcity score.
        late = candidates
    seed_key = late[rank % len(late)]
    keys = _expand(index, seed_key, target_size)
    return _make("guided", seed, target_size, seed_key, keys, index)


def random_neighborhood(index, target_size, *, factory_id, seed=42,
                        preferred_window=None):
    rng = random.Random(int(seed))
    candidates = sorted(_factory_keys(index, factory_id))
    if preferred_window:
        low, high = preferred_window
        coherent = [key for key in candidates if
                    index.operation_features[key]["end_seconds"] >= low and
                    index.operation_features[key]["start_seconds"] <= high]
        if coherent:
            candidates = coherent
    seed_key = rng.choice(candidates)
    keys = _expand(index, seed_key, target_size, randomizer=rng,
                   preferred_window=preferred_window)
    return _make("random", seed, target_size, seed_key, keys, index)


def transactional_footprint(problem, index, neighborhood):
    mutable = set(neighborhood.operation_keys)
    route_preds, route_succs = set(), set()
    machine_boundary = set()
    for key in mutable:
        pred = index.route_predecessor.get(key)
        succ = index.route_successor.get(key)
        if pred and pred not in mutable:
            route_preds.add(pred)
        if succ and succ not in mutable:
            route_succs.add(succ)
        for _machine, other in index.machine_predecessor.get(key, ()):
            if other not in mutable:
                machine_boundary.add(other)
        for _machine, other in index.machine_successor.get(key, ()):
            if other not in mutable:
                machine_boundary.add(other)
    fixed_machines = [
        row for row in problem.resources.fixed_machine_commitments
        if row.start_seconds < neighborhood.end_seconds and
        row.end_seconds > neighborhood.start_seconds
    ]
    outside = route_preds | route_succs | machine_boundary
    expanded = len(mutable)+len(outside)+len(fixed_machines)
    return {
        "strategy": neighborhood.strategy,
        "target_size": neighborhood.target_size,
        "factory_id": neighborhood.factory_id,
        "mutable_operations": sorted(mutable),
        "routing_boundary_predecessors": sorted(route_preds),
        "routing_boundary_successors": sorted(route_succs),
        "machine_sequence_boundary_operations": sorted(machine_boundary),
        "relevant_fixed_machine_commitments": len(fixed_machines),
        "affected_orders": list(neighborhood.order_ids),
        "lines": list(neighborhood.line_ids),
        "worker_skill_pools": list(neighborhood.capabilities),
        "time_window_seconds": [neighborhood.start_seconds,
                                neighborhood.end_seconds],
        "seed_size": len(mutable),
        "expanded_footprint_size": expanded,
        "closure_status": (
            "NEIGHBORHOOD_CLOSURE_TOO_LARGE"
            if expanded > max(150, 3*len(mutable)) else "ACCEPTED"),
    }
