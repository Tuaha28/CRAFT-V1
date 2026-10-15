"""Serialization and clock primitives for the Execution Boundary Contract V1."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from decimal import Decimal

CONTRACT = "EXECUTION_BOUNDARY_V1"
SOURCE_CONTRACT = "frozen-plan-then-actual-v1"
LOT_STATES = ("COMPLETED", "IN_FLIGHT", "NOT_STARTED", "UNRESOLVED")


class BoundaryUnresolved(ValueError):
    def __init__(self, validation):
        super().__init__("Execution boundary is not safe for CRAFT; inspect validation errors")
        self.validation = validation


def json_value(value):
    # Captured snapshots contain millions of primitive leaves. Preserve exact
    # subclass behavior while avoiding five isinstance checks for each leaf.
    if type(value) in (str, int, float, bool, type(None)):
        return value
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return {"seconds": int(value.total_seconds())}
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else str(value)
    return value


def canonical_json(value):
    return json.dumps(json_value(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def ordered_source(value):
    """Table record ordering cannot change source identity. Segment order is retained."""
    value = json_value(value)
    # Rows have already been recursively normalized above. Re-converting each
    # row in the sorting key duplicated the traversal of every lot/resource.
    def normalized_key(row):
        return json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    for name in ("orders", "operations", "lots", "wip"):
        value[name] = sorted(value.get(name, []), key=normalized_key)
    value["resources"] = {name: sorted(rows, key=normalized_key)
                          for name, rows in sorted(value.get("resources", {}).items())}
    return value


def timestamp(value, reference=None):
    if value is None or value == "":
        return None
    result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if result.tzinfo is not None:
        raise ValueError("This dataset uses timezone-unspecified local timestamps; an offset needs a separate clock contract")
    if reference is not None and (result.tzinfo is None) != (reference.tzinfo is None):
        raise ValueError("Mixed naive/aware clocks; no implicit timezone conversion is permitted")
    return result


def iso(value):
    return value.isoformat() if value is not None else None


def quantity(value):
    if value is None or isinstance(value, bool):
        raise ValueError("Missing/invalid quantity")
    number = Decimal(str(value))
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        raise ValueError("Quantity must be a nonnegative whole number")
    return int(number)
