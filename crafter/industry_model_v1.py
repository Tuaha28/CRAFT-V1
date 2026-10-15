"""Machine-readable CRAFT Industry Operating Model V1.

This module is deliberately a documentation/audit artifact generator.  It
does not alter scheduling semantics, invoke CP-SAT, or write to the database.
The policy records distinguish frozen evidence from industry-derived policy
and questions requiring Needle Craft confirmation.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


ROOT = Path("outputs/crafter/industry_operating_model_v1")
SOURCE = "7386844498503f37cdfa08877248c51966699e31b6762f6e0c3effc572cd78ee"
CR_HASH = "56cc2b12f69f242a380f6678385c06a70341a560ebac3619408d5700b7938e92"
SNAPSHOT = "parity_fix_20260916_s42"
CANONICAL = Path("outputs/crafter/e1_7_final_eligibility_provenance")
DIAGNOSTICS = Path("outputs/crafter/disruption_repair_v2_diagnostics_final3")


def _write(out: Path, name: str, value) -> None:
    path = out / name
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _policy(name, policy_type, confidence, confirmation, source, **body):
    return {
        "policy": name,
        "policy_type": policy_type,
        "confidence": confidence,
        "factory_confirmation_required": confirmation,
        "source": source,
        **body,
    }


def _state(name, meaning, cp, resources, timing, quantity, transitions, events):
    return {
        "state": name,
        "meaning": meaning,
        "cp_sat_may_move": cp,
        "resources_may_change": resources,
        "start_time_may_change": timing,
        "quantity_may_change": quantity,
        "line_may_change": resources,
        "entry_events": events.get("in", []),
        "exit_events": events.get("out", []),
        "transition_notes": transitions,
    }


def build_artifacts():
    evidence = {
        "snapshot": SNAPSHOT,
        "source_fingerprint": SOURCE,
        "cr_phenotype_hash": CR_HASH,
        "semantic_freeze": str(CANONICAL),
        "disruption_diagnostics": str(DIAGNOSTICS),
        "database_writes": 0,
    }
    common_industry = {
        "policy_type": "INDUSTRY_DERIVED_POLICY",
        "confidence": "HIGH",
        "factory_confirmation_required": True,
        "source": "garment/apparel dynamic scheduling practice; exact Needle Craft SOP not publicly confirmed",
    }

    recovery_levels = [
        {"level": 0, "name": "ABSORB_NO_ACTION", "description": "Keep the validated plan when feasibility and delivery are unaffected."},
        {"level": 1, "name": "TEMPORAL_SHIFT", "description": "Wait or right-shift work while preserving resources and sequence."},
        {"level": 2, "name": "SAME_LINE_RESOURCE_SUBSTITUTION", "description": "Use a compatible machine or qualified worker on the incumbent line."},
        {"level": 3, "name": "SAME_LINE_CAPACITY_ADJUSTMENT", "description": "Change legal k or rebalance the same line with authoritative duration recalculation."},
        {"level": 4, "name": "LOCAL_RESEQUENCING", "description": "Change local order only within an explicitly released neighborhood."},
        {"level": 5, "name": "CROSS_LINE_RESOURCE_BORROWING", "description": "Borrow a qualified worker/resource while keeping operation line when legal."},
        {"level": 6, "name": "CROSS_LINE_OPERATION_MOVE", "description": "Move only affected work to another explicitly eligible line."},
        {"level": 7, "name": "BOUNDED_CRAFT_REPAIR", "description": "Generate fresh executable modes and solve a bounded local CP model."},
        {"level": 8, "name": "BROADER_REBUILD", "description": "Escalate neighborhood/context only when local repair cannot satisfy policy."},
        {"level": 9, "name": "MANAGER_INTERVENTION", "description": "Explain the boundary and request an accountable business decision."},
    ]

    lifecycle = {
        "states": [
            _state("NOT_RELEASED", "Demand exists but release/material/precedence conditions are not met.", True, True, True, True, "No execution resources are reserved.", {"in": ["order_created", "precedence_not_ready"], "out": ["release_approved"]}),
            _state("PLANNED", "Included in the predictive schedule but not yet released to the floor.", True, True, True, True, "Incumbent mode is a fallback, not a production commitment.", {"in": ["release_approved"], "out": ["release_time", "manager_release"]}),
            _state("RELEASED", "Eligible to start when resources and material are available.", True, True, True, True, "Fresh modes must respect current reservations/calendars.", {"in": ["release_time", "manager_release"], "out": ["start", "blocked", "quality_hold"]}),
            _state("FROZEN", "Protected schedule commitment in the near-term stability zone.", False, False, False, False, "Only explicit event policy or manager authorization can release it.", {"in": ["freeze_window"], "out": ["event_release", "completion"]}),
            _state("IN_PROGRESS", "Execution has begun and remaining quantity exists.", False, "extremely restricted", False, "remaining quantity only", "Partial migration is unsupported until the WIP contract is implemented.", {"in": ["start"], "out": ["pause", "quality_hold", "completion", "supported_interruption"]}),
            _state("PAUSED", "Execution is temporarily stopped while WIP remains in process.", False, "same crew/resource continuity required", False, "remaining quantity only", "Current worker pause/resume semantics preserve the same crew; machine hold is continuous.", {"in": ["worker_pause", "material_wait", "utility_outage"], "out": ["resume", "quality_hold", "manager_intervention"]}),
            _state("BLOCKED", "Cannot proceed because material, predecessor, resource, or policy condition is unavailable.", True, True, True, True, "Do not invent a line/resource assignment; release safe resources and monitor.", {"in": ["material_shortage", "predecessor_delay", "resource_failure"], "out": ["condition_cleared", "fallback", "manager_intervention"]}),
            _state("QUALITY_HOLD", "Production is held pending inspection/disposition.", False, "case-specific", False, "affected quantity only", "Quality disposition must identify good, reject, scrap, and rework quantity.", {"in": ["qc_failure", "inspection_delay"], "out": ["pass", "rework", "scrap", "manager_intervention"]}),
            _state("REWORK", "A separately accountable rework demand is required.", True, True, True, True, "Rework is not silently folded into original productive duration.", {"in": ["quality_hold"], "out": ["reinspection", "completion", "manager_intervention"]}),
            _state("COMPLETED", "Required quantity has completed and is immutable.", False, False, False, False, "Completed work is never rescheduled.", {"in": ["completion"], "out": []}),
            _state("CANCELLED", "Future demand is cancelled; completed/in-progress WIP needs business disposition.", False, False, False, False, "Release future capacity only after cancellation policy is applied.", {"in": ["order_cancelled"], "out": ["reinstated"]}),
        ],
        "transition_model": [
            ["NOT_RELEASED", "PLANNED", "release_approved"], ["PLANNED", "RELEASED", "release_time_or_manager_release"],
            ["RELEASED", "IN_PROGRESS", "start"], ["IN_PROGRESS", "PAUSED", "supported_pause"],
            ["PAUSED", "IN_PROGRESS", "resume_same_crew"], ["IN_PROGRESS", "QUALITY_HOLD", "qc_event"],
            ["QUALITY_HOLD", "REWORK", "rework_disposition"], ["IN_PROGRESS", "COMPLETED", "remaining_quantity_zero"],
            ["RELEASED", "BLOCKED", "resource_material_or_precedence_failure"], ["BLOCKED", "RELEASED", "condition_cleared"],
        ],
        "unsupported_contracts": ["in_progress_machine_migration", "arbitrary_partial_lot_split"],
    }

    artifacts = {
        "industry_operating_model.json": {
            "model_version": "CRAFT_INDUSTRY_OPERATING_MODEL_V1",
            "status": "DESIGN_AND_AUDIT_FREEZE",
            "system_description": "A WIP-aware, event-driven, predictive-reactive dynamic scheduling and selective schedule-repair system for garment manufacturing.",
            "philosophy": "CRAFT performs the minimum operationally disruptive recovery that restores feasibility and protects delivery performance.",
            "terms": {
                "predictive_schedule": "A forward plan generated from the authoritative factory snapshot before deviations occur.",
                "reactive_repair": "A bounded correction after a material event, using fresh executable modes and exact validation.",
                "event_driven_rescheduling": "Rescheduling triggered by a structured event rather than periodic indiscriminate rebuilding.",
                "dynamic_scheduling": "Repeatedly reconciling plan, execution, WIP, resources, and constraints as state changes.",
                "wip_aware": "Using completed/good/rejected/remaining quantity and execution state rather than treating all work as untouched jobs.",
                "selective_local_repair": "Releasing only an impacted/contextual neighborhood while freezing the rest of the schedule.",
                "frozen_schedule": "Validated commitments protected from movement unless a documented policy releases them.",
                "rolling_horizon": "A moving decision horizon where completed work is immutable and distant work remains flexible.",
                "schedule_stability": "Preference for fewer, smaller, and more explainable changes.",
                "schedule_nervousness": "Operational harm caused by repeatedly changing near-term commitments.",
                "match_up_recovery": "Returning to the incumbent plan at the earliest sensible stable boundary after temporary repair.",
                "right_shift_recovery": "Delaying affected future work while preserving line, resource identity, and sequence when feasible.",
            },
            "architecture": ["authoritative_factory_state", "global_resource_state_graph", "event_update", "impact_analysis", "industry_recovery_policy", "bounded_neighborhood", "fresh_joint_executable_modes", "local_cp_sat", "exact_replay", "independent_validator", "accept_escalate_rollback"],
            "evidence": evidence,
        },
        "operation_lifecycle.json": lifecycle,
        "wip_contract.json": {
            "status": "REQUIRED_CONTRACT_DEFERRED_FOR_IMPLEMENTATION",
            "fields": ["target_quantity", "completed_quantity", "good_quantity", "defective_quantity", "rejected_quantity", "remaining_quantity", "rework_quantity", "current_operation", "current_line", "current_machines", "current_workers", "actual_start", "productive_time_accumulated", "interruption_time", "expected_remaining_duration"],
            "future_operation": {"definition": "Not started.", "movement": "May be considered by policy and bounded repair."},
            "in_progress_operation": {"definition": "Started with remaining quantity.", "movement": "Extremely restricted; no unsupported migration or quantity fabrication."},
            "completed_operation": {"definition": "Required quantity completed.", "movement": "Immutable."},
            "source": "FACTORY_SUPPORTED_POLICY for current operation timing/segments; WIP quantities beyond phenotype are ASSUMPTION_REQUIRES_CONFIRMATION",
            "data_gap": "Current validated benchmark is operation-level and does not expose a complete bundle/WIP ledger.",
        },
        "bundle_lot_streaming_policy.json": {
            "lot_streaming_supported_conceptually": True,
            "bundle_level_implementation": "DEFERRED_UNLESS_DATA_AVAILABLE",
            "policy_type": "INDUSTRY_DERIVED_POLICY",
            "description": "Garment bundles/sublots may stream to downstream operations before the full order quantity completes, but this requires bundle identity, quantity, quality, and routing data.",
            "current_behavior": "CRAFT remains operation/order resolution; do not infer bundle movement from aggregate phenotype fields.",
            "future_extension": ["bundle_id", "sublot_quantity", "good/rework quantity by bundle", "bundle precedence", "partial completion transfer rules"],
        },
        "event_taxonomy.json": {
            "categories": {
                "machine_equipment": ["single_future_machine_breakdown", "in_progress_machine_breakdown", "multiple_machine_breakdown", "short_outage", "long_outage", "unknown_repair_duration", "known_repair_duration", "machine_recovery", "planned_maintenance", "unplanned_maintenance", "special_purpose_machine_shortage", "machine_efficiency_degradation"],
                "worker": ["single_worker_absence", "multiple_worker_absence", "late_arrival", "early_departure", "worker_return", "temporary_unavailability", "skill_shortage", "multi_skilled_reassignment", "worker_borrowed", "productivity_reduction"],
                "order": ["urgent_order", "manager_priority_override", "new_order", "rush_order", "due_date_change", "quantity_increase", "quantity_decrease", "order_cancellation", "partial_cancellation", "order_split", "customer_priority_change"],
                "production_flow": ["predecessor_delay", "processing_overrun", "line_imbalance", "bottleneck", "machine_congestion", "worker_congestion", "setup_delay", "changeover_delay", "unexpected_waiting", "downstream_starvation", "wip_blocking"],
                "quality": ["qc_rejection", "rework_required", "repeated_defect", "inspection_delay", "quality_hold", "scrap", "machine_quality_problem"],
                "material": ["fabric_shortage", "trim_shortage", "accessory_shortage", "delayed_material", "partial_material", "wrong_material", "shade_lot_mismatch", "material_received"],
                "factory_utility": ["power_outage", "section_shutdown", "line_shutdown", "calendar_change", "emergency_shutdown", "utility_failure"],
                "recovery": ["machine_repaired", "worker_returns", "material_arrives", "qc_hold_removed", "maintenance_completed", "urgent_priority_removed", "order_reinstated"],
                "combined": ["machine_breakdown_worker_absence", "urgent_machine_breakdown", "material_shortage_urgent_order", "predecessor_delay_resource_failure", "multiple_simultaneous_disruptions"],
            },
            "unknown_event": {"name": "UNKNOWN_OR_UNSUPPORTED_EVENT", "behavior": ["freeze_known_safe_state", "do_not_invent_recovery", "identify_impacted_scope", "manager_escalation"]},
        },
        "rescheduling_trigger_policy.json": {
            "decision_flow": ["event_occurs", "does_existing_plan_become_infeasible", "yes_repair", "no_does_delivery_priority_or_resource_risk_become_material", "yes_consider_repair", "no_monitor_only"],
            "hard_triggers": ["broken_machine_assigned_to_future_work", "required_worker_absent", "precedence_violation", "material_unavailable", "cancelled_order_occupies_capacity", "resource_conflict", "impossible_current_schedule"],
            "soft_triggers": ["small_processing_drift", "mild_bottleneck", "small_expected_lateness_change", "minor_efficiency_degradation"],
            "manager_triggers": ["urgent_order", "freeze_order", "force_local_repair", "request_broader_rebuild"],
            "thresholds": "Configurable; no unvalidated numeric thresholds are frozen in V1.",
            "source": "CRAFT_ENGINEERING_POLICY grounded in frozen validator/event contracts",
        },
        "freeze_zone_policy.json": {
            "zones": [
                {"name": "PAST", "meaning": "Completed work.", "movement": "none", "penalty": "infinite"},
                {"name": "IN_PROGRESS", "meaning": "Started WIP.", "movement": "extremely restricted", "penalty": "very_high"},
                {"name": "NEAR_FUTURE_FREEZE_ZONE", "meaning": "Near-term commitments likely visible to the floor.", "movement": "possible only with explicit event release", "penalty": "high"},
                {"name": "LOCAL_REPAIR_ZONE", "meaning": "Bounded context released by policy.", "movement": "allowed", "penalty": "measured"},
                {"name": "DISTANT_FUTURE", "meaning": "Greater planning flexibility.", "movement": "allowed under objective/policy", "penalty": "lower"},
            ],
            "durations": "Configurable and requires factory confirmation; no hard freeze minutes are asserted.",
            "reason": "Avoid schedule nervousness, floor confusion, unnecessary changeovers, and cascading resource churn.",
        },
        "matchup_recovery_policy.json": {
            "definition": "After local repair, return to the incumbent plan at the earliest operationally sensible stable boundary.",
            "targets": ["next_unaffected_operation", "next_shift", "next_order_boundary", "next_stable_resource_boundary"],
            "implementation": "DOCUMENTED_FUTURE_REQUIRED",
            "acceptance_signal": "time_to_match_up",
            "source": "INDUSTRY_DERIVED_POLICY",
        },
        "recovery_hierarchy.json": {"principle": "minimum operational disruption that restores feasibility and protects delivery", "levels": recovery_levels, "tie_break": ["hard_feasibility", "event_resolution", "urgent_priority", "total_tardiness", "recovery_severity", "stability_churn"]},
        "machine_recovery_policy.json": _policy("machine_breakdown", "INDUSTRY_DERIVED_POLICY", "HIGH", True, "CRAFT V2 policy diagnostics + garment maintenance practice", hierarchy=["absorb", "wait_right_shift", "same_line_compatible_spare", "same_line_k_reduction", "local_resequence", "another_machine_configuration", "eligible_line", "bounded_repair", "manager"], future_breakdown="Supported conceptually and through shadow event contracts.", in_progress="IN_PROGRESS_MACHINE_FAILURE_REQUIRES_WIP_CONTRACT", unknown_repair="Use conservative estimate, periodic re-evaluation, temporary repair, or manager escalation; never invent precise repair time."),
        "worker_recovery_policy.json": _policy("worker_absence", "INDUSTRY_DERIVED_POLICY", "HIGH", True, "CRAFT V2 policy diagnostics + garment staffing practice", hierarchy=["absorb", "same_line_replacement", "borrow_qualified_worker", "k_adjustment", "wait_return", "local_resequence", "operation_move", "bounded_repair", "manager"], borrowing="Worker borrowing does not imply line movement; donor-line impact must be checked.", multiple_absence=["same_line_replacement", "floating_worker", "cross_line_borrowing", "multi_skill_reassignment", "k_reduction", "delay", "operation_move", "broader_repair"]),
        "predecessor_delay_policy.json": _policy("predecessor_delay", "FACTORY_SUPPORTED_POLICY", "HIGH", True, "validated routing precedence and V2 causal propagation", flow=["update_actual_or_projected_end", "successor_earliest_start", "propagate_routing_dag", "detect_resource_conflicts", "release_affected_region", "local_repair"], causal_record=["predecessor", "old_earliest_start", "new_earliest_start", "inherited_delay", "resource_conflict", "neighborhood_reason"]),
        "production_overrun_policy.json": _policy("processing_time_overrun", "INDUSTRY_DERIVED_POLICY", "MEDIUM", True, "dynamic production control practice", flow=["update_progress", "estimate_remaining_duration", "check_downstream_feasibility", "monitor_if_safe", "repair_if_material_conflict"], no_trigger_for="Every small deviation; trigger thresholds remain configurable."),
        "quality_rework_policy.json": _policy("quality_and_rework", "INDUSTRY_DERIVED_POLICY", "MEDIUM", True, "garment QC/rework practice; source data not yet exposed", flow=["inspection", "pass_or_hold", "affected_quantity", "rework_or_scrap", "resource_time_demand", "reinspection", "return_to_flow"], implementation="DEFERRED_BY_DATA", distinctions=["partial_rework", "full_rework", "scrap", "repeated_failure"]),
        "material_shortage_policy.json": _policy("material_shortage", "INDUSTRY_DERIVED_POLICY", "HIGH", True, "garment material-control practice", flow=["block_affected_operation", "propagate_downstream", "release_safe_resources", "local_resequence", "material_arrives", "reassess_ready_region"], implementation="DEFERRED_BY_DATA", prohibition="Do not randomly change machines/workers for a blocked operation."),
        "utility_outage_policy.json": _policy("utility_outage", "INDUSTRY_DERIVED_POLICY", "MEDIUM", True, "factory utility continuity practice", scopes=["machine", "line", "section", "factory"], flow=["disable_affected_resources", "freeze_completed", "right_shift_future", "continue_unaffected_sections", "restore_resources", "reassess_future_region"], in_progress="Requires WIP contract for interruption; do not invent migration."),
        "urgent_order_policy.json": _policy("urgent_order", "FACTORY_SUPPORTED_POLICY", "HIGH", True, "validated V1 urgent-order regression", objective=["hard_feasibility", "disruption_resolution", "urgent_order_protection", "total_tardiness", "recovery_severity", "stability"], permissions="Urgency changes priority, never eligibility, precedence, calendars, commitments, or machine/worker semantics.", regression={"validated_transactions": 4, "replay_mismatches": 0}),
        "order_change_policy.json": _policy("order_lifecycle_changes", "INDUSTRY_DERIVED_POLICY", "MEDIUM", True, "production-control practice", cancellation=["remove_not_started_demand", "release_future_capacity", "business_disposition_for_in_progress"], quantity_increase=["update_remaining_quantity", "recompute_duration", "propagate_delivery_impact"], quantity_decrease=["reduce_remaining_demand", "release_excess_future_capacity", "preserve_produced_quantity"], new_order=["validate_ownership_routing_material", "insert_locally", "broaden_only_if_needed"]),
        "combined_event_policy.json": _policy("combined_events", "CRAFT_ENGINEERING_POLICY", "MEDIUM", True, "event composition contract", flow=["merge_event_constraints", "union_direct_impacts", "propagate_indirect_impacts", "generate_one_recovery_domain", "one_cp_transaction", "one_validator_pass"], priority="Hard feasibility and completed-work protection dominate event ordering."),
        "manager_intervention_policy.json": _policy("manager_intervention", "CRAFT_ENGINEERING_POLICY", "HIGH", True, "safe-failure contract", triggers=["no_physical_recovery", "unsupported_in_progress_migration", "unknown_event", "policy_conflict", "extreme_degradation", "cross_factory_movement", "capacity_shortage", "conflicting_urgent_orders", "no_safe_fallback"], explanation=["failure_reason", "affected_orders", "resources", "attempted_levels", "available_choices", "predicted_consequences"], outcome="MANAGER_REQUIRED is a valid controlled outcome, not silent scheduler failure."),
        "resource_graph_contract.json": {
            "purpose": "Global factory understanding and impact/context localization, not global optimization.",
            "nodes": {"currently_implemented": ["Order", "Operation", "Line", "Machine", "Worker", "Skill"], "partially_implemented": ["Disruption event"], "future_required": ["Material", "WIP/lot", "Quality/rework demand"]},
            "edges": {"currently_implemented": ["precedence", "order membership", "line/resource relation", "machine assignment", "worker assignment", "resource sharing", "timing impact"], "partially_implemented": ["event impact"], "future_required": ["material dependency", "WIP flow", "quality rework dependency"]},
            "role": "Build global graph, update shadow state, identify direct/indirect impact, and provide bounded context to CP.",
        },
        "cp_sat_contract.json": {
            "role": "Bounded local exact optimizer; not factory policy and not a global scheduler.",
            "input_contract": "Operationally valid JointExecutableModes with exact physical resources and timing.",
            "responsibilities": ["precedence", "machine conflicts", "worker conflicts", "timing", "frozen boundaries", "mode choice", "delivery objective", "urgency", "stability terms"],
            "prohibitions": ["global factory model", "invented worker/machine identity", "post-solve resource completion", "feasibility weakening"],
        },
        "joint_executable_mode_contract.json": {
            "immutable_after_cp": True,
            "fields": ["operation", "recovery_level", "recovery_reason", "factory", "line", "k", "machine_ids", "worker_ids", "setup", "productive_requirement", "worker_segments", "pause_intervals", "machine_occupancy", "start", "productive_start", "end", "source_event", "provenance"],
            "post_selection": "REPLAY_AND_VERIFY; never reallocate or mutate resources/timing.",
        },
        "schedule_stability_contract.json": {
            "metrics": ["operations_changed", "orders_changed", "start_time_shifts", "total_absolute_start_shift", "maximum_start_shift", "line_changes", "machine_changes", "worker_changes", "k_changes", "borrowed_worker_count", "cross_line_operation_moves", "repair_scope_ratio", "time_to_match_up"],
            "local_change_cost": {"status": "CONCEPTUAL_FROZEN_POLICY", "components": ["recovery_level", "operation_change", "resource_identity_change", "start_shift", "cross_line_move"], "use": "Secondary objective/acceptance evidence; must never override hard feasibility."},
            "completed_work": "immutable",
            "unaffected_work": "frozen unless explicit policy release",
        },
        "policy_provenance.json": {
            "types": ["FACTORY_SUPPORTED_POLICY", "INDUSTRY_DERIVED_POLICY", "CRAFT_ENGINEERING_POLICY", "ASSUMPTION_REQUIRES_CONFIRMATION"],
            "frozen_factory_evidence": ["E1.7 independent authoritative validation", "E1.7 exact replay", "Scalability V2 fresh-mode validation", "V2 diagnostic output"],
            "rule": "Unknown Needle Craft SOP is never presented as confirmed; configurable policy is recorded with confirmation requirement.",
        },
        "implementation_audit.json": {
            "capabilities": [
                {"capability": "normal_scheduling", "status": "IMPLEMENTED_AND_VALIDATED", "evidence": "E1.7 + Scalability V2"},
                {"capability": "global_resource_graph", "status": "IMPLEMENTED_AND_VALIDATED", "evidence": "Scalability V2"},
                {"capability": "bounded_neighborhood", "status": "IMPLEMENTED_AND_VALIDATED", "evidence": "E1.7 + Scalability V2"},
                {"capability": "bounded_cp_sat", "status": "IMPLEMENTED_AND_VALIDATED", "evidence": "E1.7 + Scalability V2"},
                {"capability": "exact_replay", "status": "IMPLEMENTED_AND_VALIDATED", "evidence": "180/180 incumbent parity; final E1.7"},
                {"capability": "authoritative_validator", "status": "IMPLEMENTED_AND_VALIDATED", "evidence": "6/6 final independent validation"},
                {"capability": "machine_breakdown_recovery", "status": "PARTIAL", "evidence": "V2 policy diagnostics; same-line spare boundary identified"},
                {"capability": "worker_absence_recovery", "status": "PARTIAL", "evidence": "V2 policy diagnostics; borrowing/temporal candidates"},
                {"capability": "urgent_order", "status": "IMPLEMENTED_AND_VALIDATED", "evidence": "V1 urgent regression 4/4"},
                {"capability": "predecessor_delay", "status": "PARTIAL", "evidence": "causal propagation diagnostic; full repair integration deferred"},
                {"capability": "k_adjustment", "status": "PARTIAL", "evidence": "policy screening, not full accepted event repair"},
                {"capability": "temporal_recovery", "status": "PARTIAL", "evidence": "policy candidates only"},
                {"capability": "cross_line_move", "status": "PARTIAL", "evidence": "policy candidates only"},
                {"capability": "worker_borrowing", "status": "PARTIAL", "evidence": "policy candidates only"},
                {"capability": "wip_partial_execution", "status": "DEFERRED_BY_DATA", "evidence": "bundle/quantity ledger unavailable"},
                {"capability": "quality_rework", "status": "DEFERRED_BY_DATA", "evidence": "quality disposition data unavailable"},
                {"capability": "materials_blocking", "status": "DEFERRED_BY_DATA", "evidence": "material event feed unavailable"},
                {"capability": "power_utility_outage", "status": "NOT_IMPLEMENTED", "evidence": "event contract not yet integrated"},
                {"capability": "order_cancellation_quantity_change_new_order", "status": "NOT_IMPLEMENTED", "evidence": "business event contract pending"},
                {"capability": "freeze_zones", "status": "NOT_IMPLEMENTED", "evidence": "conceptual policy only"},
                {"capability": "match_up_recovery", "status": "NOT_IMPLEMENTED", "evidence": "conceptual policy only"},
                {"capability": "schedule_stability_acceptance", "status": "PARTIAL", "evidence": "churn metrics exist; policy acceptance pending"},
                {"capability": "manager_escalation", "status": "PARTIAL", "evidence": "safe-failure diagnostics; production workflow pending"},
                {"capability": "combined_events", "status": "PARTIAL", "evidence": "event type exists; joint recovery benchmark pending"},
            ]
        },
        "gap_priority.json": {
            "P0": [
                {"gap": "execution_and_WIP_state_contract", "why": "Cannot safely classify completed/in-progress/future work or migrate partial WIP without it."},
                {"gap": "event_specific_machine_worker_recovery", "why": "Current diagnostics identify physical boundaries but do not yet produce accepted event repairs."},
                {"gap": "trigger_freeze_and_stability_acceptance", "why": "Industry-ready operation needs explicit criteria for when and how much to move."},
                {"gap": "safe_event_aware_fallback_and_manager_escalation", "why": "Invalid pre-event schedules cannot be used as fallback after a disruption."},
                {"gap": "predecessor_and_processing_delay_integration", "why": "Causal propagation must feed fresh modes and validator acceptance."},
                {"gap": "material_and_quality_blocking_contracts_if_in_scope", "why": "Blocked/rework work consumes real WIP/resources and cannot be represented as generic tardiness."},
            ],
            "P1": ["combined_disruption execution", "resource return events", "unknown repair-duration handling", "match-up recovery", "utility/section outage policy", "full disruption benchmark"],
            "P2": ["bundle/sublot streaming", "new-order insertion", "order split/cancellation edge cases", "fuzzy severity signal", "auxiliary learned neighborhood guidance"],
            "priority_rule": "P0 is required before live operational claims; P1 improves robustness; P2 requires data or later evidence.",
        },
        "factory_confirmation_register.json": {
            "questions": [
                "Are operators formally allowed to move between lines?",
                "Are machines physically moved between lines, or is work moved to machines?",
                "Is there a floating/multi-skilled worker pool?",
                "How are bundle/WIP quantities tracked?",
                "Can partially completed work move to another line?",
                "What is the QC rework routing and approval path?",
                "How are material shortages and partial availability recorded?",
                "How are machine repair estimates supplied and updated?",
                "What authority does a line manager have to release/freeze work?",
                "Who can mark an order urgent and what approval is required?",
                "Is overtime used and under what calendar rules?",
                "Can production orders be split across lines?",
                "What is the prolonged utility-failure response?",
            ],
            "policy_until_confirmed": "Use the documented industry-derived/configurable policy; never silently infer a Needle Craft SOP.",
            "does_not_block": "Normal semantic and scalability work may continue; live production integration must resolve P0 questions.",
        },
    }
    artifacts["source_identity.json"] = evidence
    return artifacts


def generate(output_dir: Path = ROOT):
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"refusing to overwrite non-empty {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts = build_artifacts()
    for name, value in artifacts.items():
        _write(output_dir, name, value)
    report = """# CRAFT Industry Operating Model & Recovery Playbook V1

## Decision

`CRAFT_INDUSTRY_MODEL_V1_FROZEN`

The operating contract is documented sufficiently to guide industrial-gap
implementation without changing frozen scheduling semantics. This is a design
and audit freeze, not a claim that every disruption class is implemented.

## Frozen architecture

Authoritative factory state and WIP → global Resource-State Graph → event
update and trigger policy → minimum-disruption recovery hierarchy → bounded
neighborhood → fresh immutable JointExecutableModes → local CP-SAT → exact
replay → independent authoritative validator → accept, escalate, rollback, or
manager intervention.

The graph provides whole-factory understanding; CP-SAT remains a bounded local
optimizer.

## Evidence and implementation boundary

Normal CRAFT scheduling, graph context, bounded local CP, exact replay, and
authoritative validation are implemented and validated. Urgent-order behavior
is a validated regression. Machine/worker event recovery, WIP-state handling,
freeze zones, match-up recovery, quality/material semantics, and manager
workflow remain implementation gaps or data-dependent contracts.

The V2 diagnostic evidence identifies a real physical boundary: the frozen
benchmark has no same-line spare-machine identity freedom for the tested
machine probe. It does not justify weakening machine semantics or treating
cross-line movement as the default.

## Provenance rule

Factory-supported facts, industry-derived policies, CRAFT engineering policy,
and assumptions requiring Needle Craft confirmation are explicitly separated in
the JSON artifacts. Unknown SOP details are configurable rather than invented.

## Next phase

Implement P0 gaps systematically, beginning with the execution/WIP state
contract, event-specific recovery, trigger/freeze/stability policy, safe
fallback, manager escalation, and causal delay integration. Do not start web
application integration or GNN/GA work before those contracts are executable.

Database writes: `0`.
"""
    (output_dir / "final_report.md").write_text(report, encoding="utf-8")
    files = {str(p.relative_to(output_dir)).replace("\\", "/"): _hash(p)
             for p in sorted(output_dir.rglob("*")) if p.is_file() and p.name != "artifact_hash_manifest.json"}
    manifest = {
        "files": files,
        "source_fingerprint": SOURCE,
        "cr_phenotype_hash": CR_HASH,
        "snapshot": SNAPSHOT,
        "database_writes": 0,
        "decision": "CRAFT_INDUSTRY_MODEL_V1_FROZEN",
        "artifact_count": len(files),
    }
    _write(output_dir, "artifact_hash_manifest.json", manifest)
    return {"decision": manifest["decision"], "artifact_count": len(files), "database_writes": 0}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT)
    args = parser.parse_args()
    print(json.dumps(generate(args.output_dir), indent=2))
