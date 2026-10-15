"""
Canonical mapping between the two vocabularies used across this schema:

  - routing_operations.required_machine_type
        The MACHINE CLASS an operation needs (e.g. "Single-Needle Lockstitch")
  - machine_capabilities.operation_type / operator_skill_matrix.operation_type
        The TASK/SKILL CATEGORY (e.g. "Body Assembly")

These are genuinely different concepts (equipment class vs. garment-
construction task), not two names for the same thing that drifted apart —
but in this factory's operation set they correspond 1:1. That correspondence
was never expressed anywhere in the schema or code; graph/edge_builder.py
previously bridged the two with a fragile 4-character prefix match against
machine_model strings, which happened to work for machine eligibility
(machine_model names like "Juki-SING-83" coincidentally embed the type) but
never worked at all for worker-skill matching — 3 of the 5 machine types had
zero matching worker skill category, meaning 60% of all operations had no
eligible worker in the graph.

If a 6th machine/operation type is ever added and doesn't fit this mapping,
extend the dict below rather than falling back to string-prefix guessing.
"""

MACHINE_TYPE_TO_OPERATION_TYPE = {
    "single-needle lockstitch": "body assembly",
    "overlock serger (4-thread)": "seam edge overlock",
    "flatlock coverstitch": "flatlock hemming",
    "automated pattern tacker": "bar-tack pocketing",
    "feed-off-the-arm special": "tubular sleeve join",
}


def normalize(value) -> str:
    return str(value).strip().lower()


def machine_type_to_operation_type(required_machine_type) -> str | None:
    """
    Returns the canonical operation_type (skill/capability category) for a
    given required_machine_type, or None if it's not in the known mapping —
    callers should log and skip rather than silently guessing via substring
    matching.
    """
    return MACHINE_TYPE_TO_OPERATION_TYPE.get(normalize(required_machine_type))


# ---------------------------------------------------------------------------
# operation_type -> integer index, for the GNN's learned operation_type
# embedding (models/gnn.py, graph/node_features.py). Defined ONCE, here,
# alongside the type vocabulary itself -- not re-derived ad-hoc wherever an
# index is needed. This module's own docstring already explains why a
# second, independently-computed source of truth for this vocabulary is
# exactly the class of bug (taxonomy-matching drift) this file exists to
# prevent; the index mapping gets the same discipline.
#
# Index order is arbitrary (dict insertion order of
# MACHINE_TYPE_TO_OPERATION_TYPE's values) but fixed. UNKNOWN_OPERATION_TYPE_INDEX
# is a reserved extra slot so a 6th type showing up before this dict is
# extended maps to a real, stable index instead of crashing -- an embedding
# table can absorb an "unknown" bucket cheaply; growing it correctly when a
# real 6th type is added is still a deliberate edit here, not automatic.
# ---------------------------------------------------------------------------
OPERATION_TYPE_TO_INDEX = {
    op_type: idx for idx, op_type in enumerate(dict.fromkeys(MACHINE_TYPE_TO_OPERATION_TYPE.values()))
}
UNKNOWN_OPERATION_TYPE_INDEX = len(OPERATION_TYPE_TO_INDEX)
NUM_OPERATION_TYPES = len(OPERATION_TYPE_TO_INDEX) + 1  # +1 for the unknown bucket


def operation_type_index(operation_type: str | None) -> int:
    """
    Integer index for a canonical operation_type string (already resolved
    via machine_type_to_operation_type), for the GNN's operation_type
    embedding lookup. None, or any string not in the known vocabulary,
    maps to UNKNOWN_OPERATION_TYPE_INDEX rather than raising -- the
    embedding table always has room for it.
    """
    if operation_type is None:
        return UNKNOWN_OPERATION_TYPE_INDEX
    return OPERATION_TYPE_TO_INDEX.get(operation_type, UNKNOWN_OPERATION_TYPE_INDEX)
