"""Bounded, policy-aware event release fixpoint for local disruption repair.

The fixpoint is deliberately domain-agnostic: callers provide fresh mode
generation and concrete conflict screening callbacks.  It never relaxes CP
constraints and never mutates a selected mode.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Callable, Mapping, Sequence


@dataclass(frozen=True)
class FixpointConfig:
    max_iterations: int = 5
    max_new_promotions_per_iteration: int = 10
    max_total_released: int | None = None
    convergence_control: bool = False


def _canonical_value(value):
    """Convert conflict/mode evidence into deterministic JSON-safe values."""
    if isinstance(value, Mapping):
        return {str(k): _canonical_value(value[k]) for k in sorted(value, key=str)}
    if isinstance(value, (tuple, list, set)):
        values = [_canonical_value(item) for item in value]
        # Resource/operation evidence is order-insensitive, while intervals and
        # worker segments are intentionally kept in their supplied order.
        return values
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def canonical_conflict_record(row: Mapping) -> dict:
    """Canonicalize only physical frontier evidence.

    Iteration/source/policy bookkeeping is intentionally excluded so that a
    frontier repeats only when its physical conflict structure repeats.
    """
    fields = (
        "operation_key", "conflicts_with", "resource_type", "resource_ids",
        "worker_id", "machine_id", "left_segment", "right_segment",
        "left_interval", "right_interval", "overlap_seconds", "reason",
        "direct_conflict", "causal_distance",
    )
    value = {field: _canonical_value(row.get(field)) for field in fields if field in row}
    # IDs are semantically sets; retain deterministic ordering without touching
    # interval/segment order.
    for field in ("resource_ids",):
        if field in value and isinstance(value[field], list):
            value[field] = sorted(value[field], key=str)
    return value


def frontier_signature(rows: Sequence[Mapping]) -> dict:
    records = sorted(
        {json.dumps(canonical_conflict_record(row), sort_keys=True, separators=(",", ":"))
         for row in rows}
    )
    canonical = [json.loads(item) for item in records]
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return {
        "hash": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "records": canonical,
        "conflict_records": len(canonical),
        "unique_operations": sorted({str(row.get("operation_key")) for row in canonical if row.get("operation_key")}),
        "unique_workers": sorted({str(row.get("worker_id")) for row in canonical if row.get("worker_id")}),
        "unique_machines": sorted({str(row.get("machine_id")) for row in canonical if row.get("machine_id") is not None}),
        "unique_routing_conflicts": sum(1 for row in canonical if row.get("resource_type") == "ROUTING"),
    }


def mode_domain_signature(current: Mapping) -> dict:
    """Hash the complete fresh executable domain for progress detection."""
    modes = current.get("modes", {}) if isinstance(current, Mapping) else {}
    rows = []
    for key in sorted(modes, key=str):
        for mode in modes.get(key, ()):
            if hasattr(mode, "as_dict"):
                value = mode.as_dict()
            elif isinstance(mode, Mapping):
                value = dict(mode)
            else:
                value = {"repr": repr(mode)}
            rows.append({"operation_key": str(key), "mode": _canonical_value(value)})
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)
    return {
        "hash": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "mode_count": len(rows),
        "operation_count": len(modes),
    }


def _state_for(index, key: str, event_time_seconds: int) -> str:
    row = index.by_key.get(str(key), {})
    # Incumbent index rows in the authoritative benchmark retain the source
    # phenotype minute fields.  Some diagnostic adapters also expose seconds;
    # use either without defaulting a future row to zero (which would make it
    # appear completed and suppress a legitimate event promotion).
    if "start_seconds" in row:
        start = int(row.get("start_seconds", 0))
    else:
        start = int(round(float(row.get("start_minutes", 0.0)) * 60.0))
    if "end_seconds" in row:
        end = int(row.get("end_seconds", 0))
    else:
        end = int(round(float(row.get("end_minutes", 0.0)) * 60.0))
    if end <= int(event_time_seconds):
        return "COMPLETED"
    if start <= int(event_time_seconds) < end:
        return "IN_PROGRESS"
    return "FUTURE"


def _policy_allows(state: str, policy=None, key: str = "") -> tuple[bool, str]:
    if state == "COMPLETED":
        return False, "COMPLETED_IMMUTABLE"
    if state == "IN_PROGRESS":
        return False, "IN_PROGRESS_INTERRUPTION_UNSUPPORTED"
    if policy is not None and hasattr(policy, "allows_event_release"):
        result = policy.allows_event_release(key=key, state=state)
        if isinstance(result, tuple):
            return bool(result[0]), str(result[1])
        return bool(result), "POLICY_DECISION"
    return state == "FUTURE", "FUTURE_EVENT_RELEASE"


def _rank_rows(rows: Sequence[Mapping], index) -> list[dict]:
    def sort_key(row):
        key = str(row.get("operation_key", ""))
        left = int(index.by_key.get(key, {}).get("start_seconds", 0))
        overlap = -int(row.get("overlap_seconds", 0))
        direct = 0 if row.get("direct_conflict") else 1
        distance = int(row.get("causal_distance", 0))
        return (direct, left, overlap, distance, key)
    return sorted((dict(row) for row in rows), key=sort_key)


def resolve_event_release_fixpoint(*, index, event, neighborhood_keys: Sequence[str],
                                   initial_released: Sequence[str],
                                   generate: Callable[[tuple[str, ...], int], Mapping],
                                   conflicts: Callable[[tuple[str, ...], Mapping, int], Sequence[Mapping]],
                                   relaxed_witnesses: Callable[[tuple[str, ...], Mapping, int], Sequence[Mapping]] | None = None,
                                   cp_probe: Callable[[Mapping, tuple[str, ...], int], Mapping] | None = None,
                                   policy=None, config: FixpointConfig | None = None) -> dict:
    """Generate fresh domains and promote only concrete future conflicts.

    ``generate`` is called for every iteration with the complete current
    released set.  Its returned mapping is retained only for that iteration;
    callers must not append stale modes.  ``conflicts`` may return worker,
    machine, routing, or reservation evidence.  The optional
    ``relaxed_witnesses`` callback is diagnostic-only and may expose concrete
    productive-segment overlaps from a worker-relaxed model when ordinary
    frozen-incumbent screening has reached a local fixpoint.
    """
    cfg = config or FixpointConfig()
    keys = tuple(sorted(set(map(str, neighborhood_keys))))
    released = set(map(str, initial_released)) & set(keys)
    promoted_history: set[str] = set(released)
    iterations: list[dict] = []
    all_evidence: list[dict] = []
    current = None
    stop_reason = "ITERATION_LIMIT_REACHED"
    frontier_history: list[dict] = []
    domain_history: list[dict] = []
    cycles: list[dict] = []
    convergence = bool(cfg.convergence_control)
    for iteration in range(max(0, int(cfg.max_iterations))):
        released_tuple = tuple(sorted(released))
        current = generate(released_tuple, iteration)
        raw = [dict(row) for row in conflicts(released_tuple, current, iteration)]
        for row in raw:
            row.setdefault("iteration", iteration)
            row.setdefault("source", "fresh_mode_conflict_screen")
        candidates = list(raw)
        relaxed_rows: list[dict] = []
        if not any(str(row.get("operation_key")) not in released for row in candidates) and relaxed_witnesses is not None:
            relaxed_rows = [dict(row) for row in relaxed_witnesses(released_tuple, current, iteration)]
            for row in relaxed_rows:
                row.setdefault("iteration", iteration)
                row.setdefault("source", "worker_relaxed_concrete_witness")
            candidates.extend(relaxed_rows)
        ranked = _rank_rows(candidates, index)
        frontier = frontier_signature(ranked)
        domain = mode_domain_signature(current)
        previous_frontier = frontier_history[-1] if frontier_history else None
        previous_domain = domain_history[-1] if domain_history else None
        frontier_history.append(frontier)
        domain_history.append(domain)
        eligible: list[dict] = []
        rejected: list[dict] = []
        seen: set[str] = set()
        for row in ranked:
            key = str(row.get("operation_key", ""))
            if not key or key in released or key in seen or key not in keys:
                continue
            seen.add(key)
            state = _state_for(index, key, int(getattr(event, "event_time_seconds", 0)))
            allowed, policy_reason = _policy_allows(state, policy, key)
            row["operation_state"] = state
            row["policy_release_allowed"] = bool(allowed)
            row["policy_reason"] = policy_reason
            if allowed:
                eligible.append(row)
            else:
                rejected.append(row)
        budget = max(0, int(cfg.max_new_promotions_per_iteration))
        if cfg.max_total_released is None:
            remaining = budget
        else:
            remaining = max(0, int(cfg.max_total_released) - len(released))
            remaining = min(remaining, budget)
        selected = eligible[:remaining]
        selected_keys = [str(row["operation_key"]) for row in selected]
        for row in selected:
            row["promotion_iteration"] = iteration
            row["promotion_reason"] = row.get("reason", "CONCRETE_EVENT_RESOURCE_CONFLICT")
            all_evidence.append(row)
        cp_probe_result = None
        if cp_probe is not None:
            try:
                cp_probe_result = dict(cp_probe(current, released_tuple, iteration) or {})
            except Exception as exc:  # diagnostic only; never hide normal fixpoint behavior
                cp_probe_result = {"status": "PROBE_ERROR", "error": repr(exc)}
        movement = "CLOSED" if not ranked else "STABLE"
        if previous_frontier is not None:
            old_ops = set(previous_frontier.get("unique_operations", ()))
            new_ops = set(frontier.get("unique_operations", ()))
            old_records = int(previous_frontier.get("conflict_records", 0))
            new_records = int(frontier.get("conflict_records", 0))
            if not ranked:
                movement = "CLOSED"
            elif len(new_ops) < len(old_ops) or new_records < old_records:
                movement = "SHRINKING"
            elif len(new_ops) > len(old_ops) or new_records > old_records:
                movement = "EXPANDING"
            elif frontier["hash"] != previous_frontier.get("hash"):
                movement = "SHIFTING"
            else:
                movement = "STABLE"
        exact_repeat = bool(previous_frontier and frontier["hash"] == previous_frontier.get("hash")
                            and previous_domain and domain["hash"] == previous_domain.get("hash")
                            and not selected_keys)
        earlier = next((idx for idx, item in enumerate(frontier_history[:-1])
                        if item.get("hash") == frontier.get("hash")), None)
        cycle = bool(earlier is not None and not selected_keys and
                     (earlier != len(frontier_history) - 2 or exact_repeat is False))
        if cycle:
            cycles.append({"from_iteration": earlier, "to_iteration": iteration,
                           "hash": frontier.get("hash")})
        iterations.append({
            "iteration": iteration,
            "released_before": sorted(released),
            "candidate_conflicts": ranked,
            "relaxed_witnesses": relaxed_rows,
            "eligible_promotions": eligible,
            "rejected_promotions": rejected,
            "selected_promotions": selected,
            "released_after": sorted(released | set(selected_keys)),
            "mode_generation": {k: current.get(k) for k in ("counts", "failures", "mode_count", "generation_seconds") if k in current},
            "frontier": frontier,
            "frontier_signature_hash": frontier["hash"],
            "domain_signature": domain,
            "frontier_movement": movement,
            "exact_frontier_repeat": exact_repeat,
            "frontier_cycle": cycle,
            "cp_probe": cp_probe_result,
            "new_promotions": len(selected_keys),
        })
        if cp_probe_result and cp_probe_result.get("status") in ("OPTIMAL", "FEASIBLE"):
            stop_reason = "CP_FEASIBLE"
            break
        if convergence and not ranked and not selected_keys:
            stop_reason = "CAUSAL_CLOSURE_REACHED"
            break
        if convergence and exact_repeat:
            stop_reason = "SAME_FRONTIER_REPEAT"
            break
        if convergence and cycle:
            stop_reason = "FRONTIER_CYCLE_DETECTED"
            break
        if not selected_keys:
            if convergence:
                stop_reason = "NO_NEW_LEGAL_PROMOTIONS" if ranked else "CAUSAL_CLOSURE_REACHED"
            else:
                stop_reason = "NO_NEW_PROMOTIONS"
            break
        released.update(selected_keys)
        promoted_history.update(selected_keys)
        if cfg.max_total_released is not None and len(released) >= int(cfg.max_total_released):
            stop_reason = "RELEASE_BUDGET_REACHED"
            break
    else:
        stop_reason = "ITERATION_LIMIT_REACHED"
    if len(iterations) >= int(cfg.max_iterations) and stop_reason == "ITERATION_LIMIT_REACHED":
        stop_reason = "ITERATION_LIMIT_REACHED"
    return {
        "status": "FIXPOINT_REACHED" if stop_reason == "NO_NEW_PROMOTIONS" else "BOUNDED_STOP",
        "released": sorted(released),
        "promoted_history": sorted(promoted_history),
        "iterations": iterations,
        "promotion_evidence": all_evidence,
        "stop_reason": stop_reason,
        "config": {"max_iterations": int(cfg.max_iterations),
                    "max_new_promotions_per_iteration": int(cfg.max_new_promotions_per_iteration),
                    "max_total_released": cfg.max_total_released,
                    "convergence_control": convergence},
        "current": current,
        "frontier_history": frontier_history,
        "domain_history": domain_history,
        "cycles": cycles,
    }
