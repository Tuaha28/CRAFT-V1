"""
Factory ownership: persisted once, read by everyone, never re-derived.

Covers Batch-4 fix 5 (ownership is the single source of truth, WIP is never
reassigned) and fix 7 (contention cliques scope by that same ownership, and
stay company-wide until it exists).
"""

import numpy as np
import pandas as pd
import pytest
import torch

import graph.edge_builder as edge_builder
import ga.chromosome as chromosome
from ga.infeasibility import ScheduleInfeasibleError
from utils.factory_ownership import FactoryOwnershipError, require_factory_ownership


# ---------------------------------------------------------------------------
# Fix 7 -- disjunctive pools scope by ownership, guarded until it exists
# ---------------------------------------------------------------------------

def _routings(rows):
    return pd.DataFrame([
        {'production_order_id': oid, 'operation_id': op,
         'required_machine_type': mtype}
        for oid, op, mtype in rows
    ])


def _op_mapping(routings):
    return {f"{r.production_order_id}_{r.operation_id}": i
            for i, r in enumerate(routings.itertuples())}


def _directed_edge_count(routings, mapping, **kwargs):
    return edge_builder.build_disjunctive_edges(routings, mapping, **kwargs).shape[1]


def test_pools_stay_company_wide_when_no_ownership_exists(monkeypatch):
    # The guard: today's database has no ownership table, so scoping must
    # NOT engage -- its data would encode the pre-fix violation.
    monkeypatch.setattr(edge_builder, 'logger', edge_builder.logger)
    monkeypatch.setitem(
        __import__('sys').modules,
        'utils.factory_ownership',
        _fake_ownership_module({}),
    )
    routings = _routings([('1', '1', 'single-needle lockstitch'),
                          ('2', '1', 'single-needle lockstitch')])
    mapping = _op_mapping(routings)
    # Same optype, different orders -> one company-wide clique -> 2 directed.
    assert _directed_edge_count(routings, mapping, legacy_mode="legacy_company_wide") == 2


def test_pools_scope_by_factory_once_ownership_is_present(monkeypatch):
    # Orders 1 and 2 share an operation type but sit in DIFFERENT factories,
    # so they compete for nothing and must not be linked.
    monkeypatch.setitem(
        __import__('sys').modules,
        'utils.factory_ownership',
        _fake_ownership_module({'1': 1, '2': 2}),
    )
    routings = _routings([('1', '1', 'single-needle lockstitch'),
                          ('2', '1', 'single-needle lockstitch')])
    mapping = _op_mapping(routings)
    assert _directed_edge_count(routings, mapping) == 0


def test_same_factory_operations_still_contend(monkeypatch):
    monkeypatch.setitem(
        __import__('sys').modules,
        'utils.factory_ownership',
        _fake_ownership_module({'1': 1, '2': 1}),
    )
    routings = _routings([('1', '1', 'single-needle lockstitch'),
                          ('2', '1', 'single-needle lockstitch')])
    mapping = _op_mapping(routings)
    assert _directed_edge_count(routings, mapping) == 2


def test_partial_ownership_refuses_to_mix_scoped_and_unscoped(monkeypatch):
    # Order 2 has no recorded owner. Mixing would let some operations
    # contend company-wide while their real competitors contend locally --
    # worse than either mode, so the whole build falls back.
    monkeypatch.setitem(
        __import__('sys').modules,
        'utils.factory_ownership',
        _fake_ownership_module({'1': 1}),
    )
    routings = _routings([('1', '1', 'single-needle lockstitch'),
                          ('2', '1', 'single-needle lockstitch')])
    mapping = _op_mapping(routings)
    with pytest.raises(FactoryOwnershipError, match="no recorded producing branch"):
        _directed_edge_count(routings, mapping)


def _fake_ownership_module(mapping):
    # Keep the actual resolver and replace only the database read.
    import types
    import utils.factory_ownership as real
    mod = types.ModuleType('utils.factory_ownership')
    mod.FactoryOwnershipError = real.FactoryOwnershipError
    mod.load_factory_ownership = lambda engine=None: dict(mapping)
    def resolve(ids, snapshot=None, *, legacy_mode=None, engine=None):
        if legacy_mode == real.LEGACY_COMPANY_WIDE:
            return None
        if snapshot is None:
            if not mapping:
                raise real.FactoryOwnershipError('No ownership')
            snapshot = real.OwnershipSnapshot(tuple(mapping.items()))
        snapshot.for_orders(ids)
        return snapshot
    mod.resolve_ownership = resolve
    return mod


# ---------------------------------------------------------------------------
# Fix 5 -- require_factory_ownership fails loudly rather than inventing
# ---------------------------------------------------------------------------

def test_missing_ownership_table_raises_with_actionable_message(monkeypatch):
    import utils.factory_ownership as fo
    monkeypatch.setattr(fo, 'load_factory_ownership', lambda engine=None: {})
    with pytest.raises(FactoryOwnershipError, match="Re-seed"):
        require_factory_ownership(['1', '2'])


def test_partial_coverage_raises_rather_than_assigning_the_remainder(monkeypatch):
    import utils.factory_ownership as fo
    monkeypatch.setattr(fo, 'load_factory_ownership', lambda engine=None: {'1': 1})
    with pytest.raises(FactoryOwnershipError, match="no recorded producing branch"):
        require_factory_ownership(['1', '2', '3'])


def test_full_coverage_returns_only_the_requested_orders(monkeypatch):
    import utils.factory_ownership as fo
    monkeypatch.setattr(
        fo, 'load_factory_ownership', lambda engine=None: {'1': 1, '2': 2, '9': 5})
    got = require_factory_ownership(['1', '2'])
    assert got == {'1': 1, '2': 2}


# ---------------------------------------------------------------------------
# Fix 5 -- ChromosomeFactory honours recorded ownership (WIP never moves)
# ---------------------------------------------------------------------------

def _frames():
    routings = pd.DataFrame([
        {'production_order_id': 1, 'operation_id': 1, 'operation_sequence_number': 1,
         'sam': 2.0, 'target_quantity': 40, 'remaining_frac': 1.0,
         'required_machine_type': 'single-needle lockstitch'},
    ])
    # Several machines per factory: the 7% standby reserve
    # (utils/machine_reserve.py) holds one back per type, which on a 2-machine
    # fixture would wipe out an entire factory and make it infeasible for
    # reasons unrelated to what these tests are checking.
    machines = pd.DataFrame(
        [{'asset_id': i, 'factory_id': 1, 'line_id': 10,
          'operation_type': 'body assembly'} for i in range(1, 6)]
        + [{'asset_id': i, 'factory_id': 2, 'line_id': 20,
            'operation_type': 'body assembly'} for i in range(6, 11)]
    )
    lines = pd.DataFrame([
        {'line_id': 10, 'is_active': 1, 'target_efficiency_percentage': 85.0},
        {'line_id': 20, 'is_active': 1, 'target_efficiency_percentage': 85.0},
    ])
    return routings, machines, lines


def test_recorded_ownership_is_honoured_not_recomputed(monkeypatch):
    # Both factories can run the routing. Fresh allocation would pick by its
    # own rule; recorded ownership must win -- that is what stops a horizon
    # rebuild relocating an in-production order.
    monkeypatch.setattr(chromosome.ChromosomeFactory, '_load_ownership',
                        staticmethod(lambda order_ids: {'1': 2}))
    routings, machines, lines = _frames()
    factory = chromosome.ChromosomeFactory(routings, machines, lines)
    assert factory.order_factory['1'] == 2
    for line_id in factory.eligibility_map['1_1']:
        assert factory.line_factory[line_id] == 2


def test_ownership_naming_a_factory_that_cannot_run_the_routing_is_rejected(monkeypatch):
    # Factory 3 exists in the ownership record but holds no machine of the
    # required type -- silently yielding an empty pool would be worse than
    # failing, so it raises.
    monkeypatch.setattr(chromosome.ChromosomeFactory, '_load_ownership',
                        staticmethod(lambda order_ids: {'1': 3}))
    routings, machines, lines = _frames()
    with pytest.raises(ScheduleInfeasibleError, match="recorded owning factory"):
        chromosome.ChromosomeFactory(routings, machines, lines)


def test_absent_ownership_falls_back_to_fresh_allocation(monkeypatch):
    monkeypatch.setattr(chromosome.ChromosomeFactory, '_load_ownership',
                        staticmethod(lambda order_ids: {}))
    routings, machines, lines = _frames()
    factory = chromosome.ChromosomeFactory(routings, machines, lines)
    assert factory.order_factory['1'] in (1, 2)
