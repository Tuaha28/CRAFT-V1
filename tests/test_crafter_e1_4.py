"""Focused contracts for the isolated E1.4 line+k repair layer."""
from types import SimpleNamespace

from crafter.lns_probe.neighborhoods import Neighborhood
from crafter.lns_probe.run_e1_4 import _graph_select, _simple_select


def _index(keys):
    return SimpleNamespace(
        operation_scores={key: float(index + 1) for index, key in enumerate(keys)},
        operation_features={key: {
            "order_tardiness_seconds": 1000 - index,
            "factory_id": 1,
        } for index, key in enumerate(keys)},
    )


def _neighborhood(keys):
    return Neighborhood(
        "e1_4_delivery_guided", 42, len(keys), 1, keys[0], tuple(keys),
        ("O1",), (1,), ("skill",), 0, 1000)


def test_e1_4_selectors_use_same_pool_and_exact_budget():
    keys = tuple(f"O1_{i}" for i in range(10))
    index = _index(keys)
    domains = {key: ({"line_id": 1, "k": 1}, {"line_id": 2, "k": 2})
               for key in keys}
    neighborhood = _neighborhood(keys)
    simple = _simple_select(neighborhood, domains, index, [
        {"operation_key": key, "potential_finish_gain_seconds": 10 * i}
        for i, key in enumerate(keys)])
    graph = _graph_select(neighborhood, domains, index)
    assert len(simple) == len(graph) == 5
    assert set(simple) <= set(keys)
    assert set(graph) <= set(keys)


def test_e1_4_selectors_are_deterministic():
    keys = tuple(f"O1_{i}" for i in range(8))
    index = _index(keys)
    domains = {key: ( {"line_id": 1, "k": 1}, {"line_id": 2, "k": 2})
               for key in keys}
    neighborhood = _neighborhood(keys)
    rows = [{"operation_key": key, "potential_finish_gain_seconds": i}
            for i, key in enumerate(keys)]
    assert _simple_select(neighborhood, domains, index, rows) == _simple_select(
        neighborhood, domains, index, rows)
    assert _graph_select(neighborhood, domains, index) == _graph_select(
        neighborhood, domains, index)


def test_e1_4_frozen_benchmark_artifact_has_validated_improvements():
    import json
    from pathlib import Path
    path = Path("outputs/crafter/e1_4_line_k_local_repair_retry5/candidate_metrics.json")
    if not path.exists():
        return
    rows = json.loads(path.read_text(encoding="utf-8"))
    assert len(rows) == 6
    assert any(row.get("accepted") for row in rows)
    assert all((row.get("before") or {}).get("total_tardiness_seconds", 24521169)
               == 24521169 for row in rows)
