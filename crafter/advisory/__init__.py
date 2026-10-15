"""Optional advisory/shadow integrations for CRAFT.

Nothing in this package is imported by the deterministic scheduling kernel at
application import time.  Shadow providers are explicitly invoked by a
caller and never receive control authority.
"""

from .shadow_contracts import (
    CRAFT_SHADOW_STATE_V1,
    LAYA_DECISION_OPTIONS,
    LayaShadowDecisionV1,
    ShadowStateV1,
    build_shadow_state,
    build_shadow_trace_row,
    canonical_json,
    classify_shadow_disagreement,
    make_laya_shadow_decision,
    state_hash,
)
from .laya_shadow import LayaShadowProvider, LayaShadowUnavailable
from .laya_runtime_bridge import (
    LayaRuntimeBridge, LayaRuntimeProvider,
    PersistentLayaRuntimeBridge, PersistentLayaRuntimeProvider,
)

__all__ = [
    "CRAFT_SHADOW_STATE_V1", "LAYA_DECISION_OPTIONS", "ShadowStateV1",
    "LayaShadowDecisionV1", "LayaShadowProvider", "LayaShadowUnavailable",
    "LayaRuntimeBridge", "LayaRuntimeProvider",
    "PersistentLayaRuntimeBridge", "PersistentLayaRuntimeProvider",
    "build_shadow_state", "build_shadow_trace_row", "canonical_json",
    "classify_shadow_disagreement", "make_laya_shadow_decision", "state_hash",
]
