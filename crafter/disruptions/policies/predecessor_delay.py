from __future__ import annotations

from collections import deque


def propagate_predecessor_delay(index, predecessor_key: str, delayed_end_seconds: int) -> dict:
    """Propagate earliest starts through the routing DAG deterministically."""
    delayed_end_seconds = int(delayed_end_seconds)
    earliest = {str(predecessor_key): delayed_end_seconds}
    inherited = {str(predecessor_key): 0}
    reasons = []
    queue = deque([str(predecessor_key)])
    while queue:
        key = queue.popleft()
        successor = index.route_successor.get(key)
        if not successor:
            continue
        successor = str(successor)
        old = int(index.by_key[successor].get("start_minutes", 0) * 60)
        new = max(old, earliest[key])
        if new > old:
            delta = new - old
            earliest[successor] = new
            inherited[successor] = int(inherited[key]) + delta
            reasons.append({"operation_key": successor, "predecessor_operation_key": key,
                            "old_earliest_start_seconds": old,
                            "new_earliest_start_seconds": new,
                            "inherited_delay_seconds": inherited[successor],
                            "reason": "ROUTING_PRECEDENCE"})
            queue.append(successor)
    return {"predecessor_operation_key": str(predecessor_key),
            "delayed_end_seconds": delayed_end_seconds,
            "propagated": reasons}
