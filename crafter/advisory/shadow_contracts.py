"""Versioned, teacher-label-free state and trace contracts for Laya shadowing."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Mapping


CRAFT_SHADOW_STATE_V1 = "CRAFT_SHADOW_STATE_V1"
SHADOW_TRACE_VERSION = "CRAFT_LAYA_SHADOW_TRACE_V1"
LAYA_SHADOW_DECISION_VERSION = "LayaShadowDecisionV1"
LAYA_DECISION_OPTIONS = (
    "SOLVE_READY_WORK",
    "HORIZON_EXTENSION_REQUIRED",
    "NEIGHBORHOOD_EXPANSION_REQUIRED",
    "FAST_FORWARD_TO_EVENT",
    "NO_EXECUTABLE_WORK",
    "UNRESOLVED_ROLLING_STATE",
)

# Keep this list aligned with the Step-3B authoritative-event contract.  The
# shadow layer may observe these events, but it must never infer resource
# availability or calendar openings.
_AUTHORITATIVE_EVENT_TYPES = frozenset({
    "COMMITTED_PREDECESSOR_COMPLETION",
    "COMPLETED_PREDECESSOR_COMPLETION",
    "IN_PROGRESS_PREDECESSOR_COMPLETION",
    "AUTHORITATIVE_PREDECESSOR_COMPLETION",
    "ORDER_RELEASE",
    "HARD_RESERVATION_END",
    "DISRUPTION_RECOVERY",
})


def _normalize(value):
    if isinstance(value, Mapping):
        return {str(key): _normalize(value[key]) for key in sorted(value, key=lambda x: str(x))}
    if isinstance(value, (set, frozenset)):
        return [_normalize(item) for item in sorted(value, key=lambda x: repr(x))]
    if isinstance(value, (tuple, list)):
        return [_normalize(item) for item in value]
    if isinstance(value, bool) or value is None or isinstance(value, (str, int, float)):
        return value
    if hasattr(value, "item"):
        return _normalize(value.item())
    raise TypeError(f"shadow state contains unsupported value {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Canonical JSON used for both reproducibility and hashing."""
    return json.dumps(_normalize(value), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def state_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _readiness_scan(decision) -> Mapping[str, Any]:
    return (getattr(decision, "provenance", {}) or {}).get("readiness_scan", {}) or {}


def _blocker_counts(scan: Mapping[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in scan.get("results", ()) or ():
        for blocker in row.get("blockers", ()) or ():
            kind = str(blocker.get("type", "UNKNOWN"))
            counts[kind] = counts.get(kind, 0) + 1
    return dict(sorted(counts.items()))


def _authoritative_event_summary(decision, *, window_start_seconds: int,
                                 authoritative_future_events=()):
    """Return the earliest explicit Step-3B event, independently of the label.

    A SOLVE_READY_WORK decision can coexist with a deferred operation whose
    committed predecessor has a known completion event.  That event remains a
    truthful state feature even though it is not the current orchestration
    action.
    """
    candidates = []
    provenance = getattr(decision, "provenance", {}) or {}
    prior = provenance.get("next_relevant_event")
    if isinstance(prior, Mapping):
        candidates.append(prior)
    scan = _readiness_scan(decision)
    for row in scan.get("results", ()) or ():
        result_key = row.get("operation_key", "")
        for blocker in row.get("blockers", ()) or ():
            if not isinstance(blocker, Mapping):
                continue
            candidates.append({
                "event_type": blocker.get("event_type", blocker.get("type", "")),
                "event_time_seconds": blocker.get(
                    "event_time_seconds", blocker.get("authoritative_end_seconds")),
                "operation_key": blocker.get("operation_key", result_key),
                "source": blocker.get("source"),
            })
    candidates.extend(authoritative_future_events or ())
    events = []
    for row in candidates:
        if not isinstance(row, Mapping):
            continue
        kind = str(row.get("event_type", row.get("type", "")))
        value = row.get("event_time_seconds", row.get("authoritative_end_seconds"))
        if kind not in _AUTHORITATIVE_EVENT_TYPES or value is None:
            continue
        try:
            event_time = int(value)
        except (TypeError, ValueError):
            continue
        if event_time <= int(window_start_seconds):
            continue
        event = {
            "event_type": kind,
            "event_time_seconds": event_time,
            "operation_key": str(row.get("operation_key", row.get("event_id", ""))),
            "source": row.get("source"),
        }
        events.append(event)
    if not events:
        return None
    return min(events, key=lambda row: (
        int(row["event_time_seconds"]), str(row["event_type"]),
        str(row["operation_key"]), str(row.get("source")),
    ))


def build_shadow_state(decision, *, lookahead_seconds: int | None = None,
                       commit_step_seconds: int | None = None,
                       current_operation_cap: int | None = None,
                       committed_boundary_count: int | None = None,
                       urgent_deferred_count: int = 0,
                       required_target_count: int = 0,
                       repeated_no_progress_state: bool = False,
                       has_required_repair_target: bool | None = None,
                       remaining_operation_count: int | None = None,
                       authoritative_future_events=()) -> "ShadowStateV1":
    """Build only normalized facts; no authoritative teacher label is included."""
    scan = _readiness_scan(decision)
    results = tuple(scan.get("results", ()) or ())
    deferred = [row for row in results if row.get("decision") == "BOUNDARY_DEFERRED"]
    event = _authoritative_event_summary(
        decision, window_start_seconds=int(getattr(decision, "window_start_seconds")),
        authoritative_future_events=authoritative_future_events)
    deferred_gaps = [
        int(row["earliest_ready_seconds"]) - int(getattr(decision, "window_end_seconds"))
        for row in deferred if row.get("earliest_ready_seconds") is not None
    ]
    closure_sizes = [
        int(row.get("required_minimum_closure_size"))
        for row in (getattr(decision, "trigger_blockers", ()) or ())
        if row.get("required_minimum_closure_size") is not None
    ]
    window_start = int(getattr(decision, "window_start_seconds"))
    window_end = int(getattr(decision, "window_end_seconds"))
    window_width = window_end - window_start
    state = {
        "contract_version": CRAFT_SHADOW_STATE_V1,
        "window": {
            "window_start_seconds": window_start,
            "window_end_seconds": window_end,
            "commit_end_seconds": int(getattr(decision, "commit_end_seconds")),
            "lookahead_seconds": None if lookahead_seconds is None else int(lookahead_seconds),
            "commit_step_seconds": None if commit_step_seconds is None else int(commit_step_seconds),
            "window_width_seconds": window_width,
        },
        "factory_state": {
            "remaining_operation_count": (
                None if remaining_operation_count is None else int(remaining_operation_count)),
            "candidate_pool_count": int(getattr(decision, "candidate_pool_count")),
            "ready_candidate_count": int(getattr(decision, "ready_candidate_count")),
            "admitted_count": len(getattr(decision, "admitted_operation_keys", ()) or ()),
            "deferred_candidate_count": int(getattr(decision, "deferred_candidate_count")),
            "unresolved_candidate_count": int(getattr(decision, "unresolved_candidate_count")),
            "committed_boundary_count": committed_boundary_count,
        },
        "readiness_flags": {
            "has_ready_work": bool(getattr(decision, "ready_candidate_count")),
            "has_deferred_work": bool(getattr(decision, "deferred_candidate_count")),
            "has_unresolved_truth": bool(getattr(decision, "unresolved_candidate_count")),
            "has_finite_authoritative_event": event is not None,
            "has_urgent_deferred_work": int(urgent_deferred_count) > 0,
            "has_required_repair_target": has_required_repair_target,
            "repeated_no_progress_state": bool(repeated_no_progress_state),
        },
        "blockers": {"counts_by_type": _blocker_counts(scan)},
        "temporal": {
            "earliest_authoritative_event_type": None if event is None else event.get("event_type"),
            "seconds_until_earliest_authoritative_event": (
                None if event is None else int(event["event_time_seconds"]) - window_start),
            "maximum_deferred_readiness_gap": max(deferred_gaps) if deferred_gaps else None,
            "minimum_deferred_readiness_gap": min(deferred_gaps) if deferred_gaps else None,
        },
        "neighborhood": {
            "current_operation_cap": current_operation_cap,
            "largest_required_movable_closure": max(closure_sizes) if closure_sizes else None,
            "movable_closure_exceeds_cap": (
                None if current_operation_cap is None or not closure_sizes
                else max(closure_sizes) > int(current_operation_cap)),
        },
        "urgency": {
            "urgent_deferred_count": int(urgent_deferred_count),
            "required_target_count": int(required_target_count),
        },
    }
    normalized = _normalize(state)
    return ShadowStateV1(normalized, canonical_json(normalized), state_hash(normalized))


@dataclass(frozen=True)
class ShadowStateV1:
    canonical_state: Mapping[str, Any]
    canonical_json: str
    state_hash: str

    def as_dict(self):
        return {
            "state_contract_version": CRAFT_SHADOW_STATE_V1,
            "state_hash": self.state_hash,
            "canonical_state": dict(self.canonical_state),
        }


@dataclass(frozen=True)
class LayaShadowDecisionV1:
    shadow_contract_version: str
    state_contract_version: str
    state_hash: str
    authoritative_craft_decision: str
    laya_status: str
    laya_predicted_decision: str | None
    probabilities: Mapping[str, float] | None
    confidence: float | None
    answer_confidence: float | None
    agreement: bool | None
    disagreement_type: str | None
    model_id: str | None
    model_subfolder: str | None
    package_version: str | None
    device: str | None
    latency_ms: float | None
    error_type: str | None
    error_message: str | None
    provenance: Mapping[str, Any]

    def as_dict(self):
        return {"shadow_contract_version": self.shadow_contract_version, **asdict(self)}


def classify_shadow_disagreement(craft_decision: str, laya_decision: str | None) -> tuple[bool | None, str | None]:
    if laya_decision is None:
        return None, "LAYA_UNAVAILABLE_OR_ERROR"
    if craft_decision == laya_decision:
        return True, "MATCH"
    pairs = {
        ("SOLVE_READY_WORK", "FAST_FORWARD_TO_EVENT"): "LAYA_FAST_FORWARD_WHILE_CRAFT_HAS_READY_WORK",
        ("FAST_FORWARD_TO_EVENT", "SOLVE_READY_WORK"): "LAYA_SOLVE_WHILE_CRAFT_HAS_NO_READY_WORK",
        ("UNRESOLVED_ROLLING_STATE", "SOLVE_READY_WORK"): "LAYA_SOLVE_WHILE_CRAFT_IS_UNRESOLVED",
        ("SOLVE_READY_WORK", "HORIZON_EXTENSION_REQUIRED"): "LAYA_EXTENSION_WHILE_CRAFT_SOLVES_READY_WORK",
        ("SOLVE_READY_WORK", "NEIGHBORHOOD_EXPANSION_REQUIRED"): "LAYA_EXPANSION_WHILE_CRAFT_SOLVES_READY_WORK",
    }
    return False, pairs.get((craft_decision, laya_decision), "OTHER_DISAGREEMENT")


def make_laya_shadow_decision(*, shadow_state: ShadowStateV1, craft_decision: str,
                              provider, provenance: Mapping[str, Any] | None = None) -> LayaShadowDecisionV1:
    """Invoke an isolated provider, then attach the teacher label afterwards."""
    raw = provider.predict(shadow_state.canonical_state)
    result = dict(raw)
    predicted = result.get("laya_predicted_decision", result.get("decision"))
    if predicted is not None and predicted not in LAYA_DECISION_OPTIONS:
        return LayaShadowDecisionV1(
            shadow_contract_version=LAYA_SHADOW_DECISION_VERSION,
            state_contract_version=CRAFT_SHADOW_STATE_V1,
            state_hash=shadow_state.state_hash,
            authoritative_craft_decision=str(craft_decision),
            laya_status="LAYA_INVALID_DECISION",
            laya_predicted_decision=None, probabilities=None, confidence=None,
            answer_confidence=None, agreement=None,
            disagreement_type="OTHER_DISAGREEMENT", model_id=result.get("model_id"),
            model_subfolder=result.get("subfolder"),
            package_version=result.get("package_version"), device=result.get("device"),
            latency_ms=result.get("latency_ms"), error_type="INVALID_LAYA_DECISION",
            error_message=f"invalid Laya shadow decision: {predicted!r}",
            provenance=dict(provenance or {}),
        )
    agreement, disagreement = classify_shadow_disagreement(craft_decision, predicted)
    probabilities = result.get("probabilities")
    if probabilities is not None:
        probabilities = {str(key): float(probabilities[key]) for key in sorted(probabilities)}
    return LayaShadowDecisionV1(
        shadow_contract_version=LAYA_SHADOW_DECISION_VERSION,
        state_contract_version=CRAFT_SHADOW_STATE_V1,
        state_hash=shadow_state.state_hash,
        authoritative_craft_decision=str(craft_decision),
        laya_status=str(result.get("laya_status", "LAYA_INFERENCE_COMPLETE")),
        laya_predicted_decision=predicted,
        probabilities=probabilities,
        confidence=None if result.get("confidence") is None else float(result["confidence"]),
        answer_confidence=None if result.get("answer_confidence") is None else float(result["answer_confidence"]),
        agreement=agreement,
        disagreement_type=disagreement,
        model_id=result.get("model_id"), model_subfolder=result.get("subfolder"),
        package_version=result.get("package_version"),
        device=result.get("device"), latency_ms=result.get("latency_ms"),
        error_type=result.get("error_type"), error_message=result.get("error_message"),
        provenance=dict(provenance or {}),
    )


def build_shadow_trace_row(*, run_id: str, factory_id: int, window_id: str,
                           shadow_state: ShadowStateV1, craft_decision: str,
                           craft_provenance: Mapping[str, Any], shadow_decision: LayaShadowDecisionV1):
    """Append-only JSONL-compatible trace row; label is added after inference."""
    return {
        "trace_version": SHADOW_TRACE_VERSION,
        "run_id": str(run_id), "factory_id": int(factory_id), "window_id": str(window_id),
        "state_hash": shadow_state.state_hash, "state": shadow_state.canonical_state,
        "craft": {"decision": str(craft_decision), "provenance": dict(craft_provenance or {})},
        "laya": shadow_decision.as_dict(),
        "comparison": {
            "agreement": shadow_decision.agreement,
            "disagreement_type": shadow_decision.disagreement_type,
        },
        "database_writes": 0,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }
