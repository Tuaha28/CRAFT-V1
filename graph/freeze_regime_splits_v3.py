"""Freeze no-test-label v3 split/graph manifests for an isolated regime DB."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _sha(values):
    return hashlib.sha256(",".join(sorted(map(str, values))).encode("utf-8")).hexdigest()


def _counts(graph):
    return {
        "nodes": {name: int(graph[name].x.shape[0]) for name in graph.node_types},
        "edges": {"|".join(kind): int(graph[kind].edge_index.shape[1]) for kind in graph.edge_types},
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("database")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    # Must precede every v3/database import; this process is DB-scoped only.
    os.environ["DB_NAME"] = args.database
    from graph.feature_schema_v3 import operation_schema_v3
    from graph.graph_builder_v3 import FactoryGraphBuilderV3

    builder = FactoryGraphBuilderV3(seed=42)
    train, validation = builder.build_train_validation()
    if builder.label_access.test_accessed:
        raise RuntimeError("Test labels opened while freezing split graph manifest")
    test = builder.build_test_graph_unlabeled()
    if builder.label_access.test_accessed:
        raise RuntimeError("Unlabeled test graph opened test labels")
    result = {
        "manifest_version": 1,
        "database": args.database,
        "dataset_identity": builder.dataset_identity,
        "seed": 42,
        "feature_schema": operation_schema_v3(),
        "splits": {
            name: {"order_count": len(bundle.order_ids), "order_ids_sha256": _sha(bundle.order_ids), "operation_count": int(bundle.graph['operation'].x.shape[0]), "graph_fingerprint": bundle.graph_fingerprint, **_counts(bundle.graph)}
            for name, bundle in (("train", train), ("validation", validation), ("test", test))
        },
        "inductive_contract": "split-specific operation graphs; shared known machine/worker identities only; no cross-split operation edges",
        "label_access": {"requests": [name for name, _ in builder.label_access.requests], "test_accessed": builder.label_access.test_accessed, "test_graph_labeled": bool(test.labels)},
    }
    path = Path(args.output); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))

if __name__ == "__main__": main()
