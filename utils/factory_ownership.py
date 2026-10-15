"""
Which branch produces which order -- read from the dataset that owns it.

THE SINGLE SOURCE OF TRUTH. Two consumers need this answer and must never
disagree about it:

  * ga/chromosome.py scopes each operation's eligible LINES to its order's
    factory, so a schedule can't scatter one order across branches.
  * graph/edge_builder.py scopes disjunctive (contention) cliques the same
    way, so the model isn't told two operations compete when they share no
    eligible resource.

Before this existed, ga/chromosome.py re-derived the assignment on every
construction. Two independent derivations of one quantity is the drift risk
this module removes: if they ever diverged, the GNN would learn contention
the GA does not permit, or miss contention it does -- silently, and only
visible as unexplained scheduling quality.

WHY IT IS PERSISTED RATHER THAN INFERRED. It cannot be recovered from
Job_Shop_Schedules: the pre-fix seeder placed every one of the 1,029 orders
across ~5.76 factories on average, so inferring ownership from existing
schedule rows would re-import exactly the violation the constraint exists to
prevent. It is recorded at generation time, in the same transaction as the
rows it describes.

WIP SAFETY. Ownership is written once per order and never reassigned. A
released or partially-produced order keeps its branch across every horizon
rebuild -- physically obvious (you cannot move work-in-progress to another
site because a re-plan felt like it), and the reason a fresh per-build
derivation was unacceptable even when deterministic.
"""

from sqlalchemy import text

from utils.logger import get_logger

logger = get_logger("factory_ownership", "events.log")

OWNERSHIP_TABLE = "Order_Factory_Ownership"


class FactoryOwnershipError(RuntimeError):
    """Ownership is missing, or does not cover the orders being scheduled."""


def _table_exists(conn, database):
    return conn.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=:db AND TABLE_NAME=:t"
        ),
        {"db": database, "t": OWNERSHIP_TABLE},
    ).scalar_one()


def load_factory_ownership(engine=None) -> dict:
    """
    {production_order_id (str): factory_id (int)} for the connected database.

    Returns {} -- not an error -- when the table does not exist at all. That
    is a legacy dataset seeded before this artifact, and callers decide
    whether they can proceed without it. A table that exists but cannot be
    read IS an error: silently treating a broken read as "no ownership"
    would hand the GA a fresh unconstrained assignment, which is the
    behaviour being removed.
    """
    if engine is None:
        from database.db import get_engine
        engine = get_engine()
    with engine.connect() as conn:
        database = conn.execute(text("SELECT DATABASE()")).scalar_one()
        if not _table_exists(conn, database):
            return {}
        rows = conn.execute(
            text(f"SELECT production_order_id, factory_id FROM {OWNERSHIP_TABLE}")
        ).fetchall()
    return {str(r[0]): int(r[1]) for r in rows}


from dataclasses import dataclass

LEGACY_COMPANY_WIDE = 'legacy_company_wide'


@dataclass(frozen=True)
class OwnershipSnapshot:
    """Immutable branch assignments read once for a graph/scheduling context."""
    entries: tuple

    def for_orders(self, order_ids):
        mapping = dict(self.entries)
        wanted = {str(o) for o in order_ids}
        missing = wanted - mapping.keys()
        if missing:
            raise FactoryOwnershipError(f'Orders have no recorded producing branch: {sorted(missing)}')
        return {o: mapping[o] for o in wanted}


def resolve_ownership(order_ids, snapshot=None, *, legacy_mode=None, engine=None):
    if legacy_mode is not None:
        if legacy_mode != LEGACY_COMPANY_WIDE or snapshot is not None:
            raise ValueError('Legacy mode must be explicitly named and cannot accompany ownership')
        logger.warning('Explicit legacy_company_wide mode: ownership guarantees are disabled')
        return None
    if snapshot is None:
        # Read failures propagate. Missing artifacts are not a legacy opt-in.
        mapping = load_factory_ownership(engine)
        if not mapping:
            raise FactoryOwnershipError('No Order_Factory_Ownership in this dataset. Re-seed or explicitly opt into legacy_company_wide mode.')
        snapshot = OwnershipSnapshot(tuple(sorted(mapping.items())))
    if not isinstance(snapshot, OwnershipSnapshot):
        raise TypeError('Expected immutable OwnershipSnapshot')
    snapshot.for_orders(order_ids)
    return snapshot


def require_factory_ownership(order_ids, engine=None):
    return resolve_ownership(order_ids, engine=engine).for_orders(order_ids)
