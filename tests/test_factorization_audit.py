from types import SimpleNamespace

from crafter.repair.factorization_audit import audit_factorization_semantics, factorization_projection


def _cfg(line, k, machines, workers):
    return SimpleNamespace(line_id=line, k=k, machine_ids=tuple(machines), worker_ids=tuple(workers))


def test_fixed_line_k_projection_reconstructs_exact_pair_domain():
    configs = [_cfg(1, 1, (1,), ("a",)), _cfg(1, 1, (2,), ("a",)),
               _cfg(1, 1, (1,), ("b",)), _cfg(1, 1, (2,), ("b",))]
    report = factorization_projection(configs)
    assert report["exact_per_line_k"] is True


def test_audit_keeps_paired_canonical_representation():
    report = audit_factorization_semantics()
    assert report["verdict"] == "PARTIAL_FACTORIZATION_VALID"
    assert report["implementation_decision"] == "retain_paired_resource_configurations"

