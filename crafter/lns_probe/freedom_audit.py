"""Deterministic E1.3 audit of the freedom that exists around an incumbent.

No optimization model is built here.  Exact incumbent resource identities and
all frozen outside reservations are used for machine/worker screens.  Line and
machine-count estimates are necessary-condition opportunity estimates, not
claims of a feasible repaired schedule.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta
from math import comb, log1p
from types import SimpleNamespace

from crafter.authoritative_semantics import (
    authoritative_operation_work_minutes, authoritative_productive_seconds,
    authoritative_setup_seconds,
)
from ga.worker_allocation import allocate_crew
from .analysis import seconds
from .anchored_model import (
    _candidate_starts, _crew_progress, machine_interval_feasible,
    outside_machine_reservations, outside_worker_reservations,
)
from .transaction import _machine_accepts


def _global_machine_reservations(problem, index):
    result = defaultdict(list)
    for row in index.rows:
        key = str(row["operation_key"])
        for machine_id in row["machine_ids"]:
            result[int(machine_id)].append((
                seconds(row["start_minutes"]), seconds(row["end_minutes"]),
                str(row["production_order_id"]), key))
    for fixed in problem.resources.fixed_machine_commitments:
        result[int(fixed.machine_id)].append((
            int(fixed.start_seconds), int(fixed.end_seconds),
            str(fixed.production_order_id),
            f"fixed:{fixed.production_order_id}"))
    for values in result.values():
        values.sort()
    return result


def _without_operation(values, operation_key):
    return tuple(value for value in values if str(value[3]) != operation_key)


def _machines(problem, operation, line_id):
    factory_id = problem.order_by_id[operation.order_id].factory_id
    return tuple(sorted(machine.machine_id for machine in problem.machines
        if machine.is_usable and machine.factory_id == factory_id and
        machine.line_id == int(line_id) and
        machine.supports(operation.required_capability)))


def _worker_counts(operator_index, factory_id, line_id, skill):
    skill = str(skill).strip().lower()
    line = tuple(sorted({str(value) for value in
        operator_index.get("by_line", {}).get((int(line_id), skill), ())}))
    factory = tuple(sorted({str(value) for value in
        operator_index.get("by_factory", {}).get((int(factory_id), skill), ())}))
    return line, factory


def _route_ready(problem, index, operation_key):
    operation = problem.operation_by_key[operation_key]
    predecessor = index.route_predecessor.get(operation_key)
    if predecessor:
        return seconds(index.by_key[predecessor]["end_minutes"])
    return max(0, problem.order_by_id[operation.order_id].release_seconds)


def estimate_line_k(problem, index, op_details, operator_index,
                    machine_reservations, operation_key, line_id, k):
    """Machine-exact/frozen, labour-aggregate opportunity estimate.

    The hold duration is setup plus authoritative productive seconds on a
    continuous proxy. Exact calendar pauses and worker identities deliberately
    remain outside this estimator.
    """
    operation = problem.operation_by_key[operation_key]
    factory_id = problem.order_by_id[operation.order_id].factory_id
    line_id, k = int(line_id), int(k)
    machines = _machines(problem, operation, line_id)
    line_workers, factory_workers = _worker_counts(
        operator_index, factory_id, line_id, operation.required_capability)
    setup = authoritative_setup_seconds(op_details[operation_key])
    productive = authoritative_productive_seconds(op_details[operation_key], k)
    duration = setup + productive
    base = {
        "line_id": line_id, "k": k,
        "setup_seconds": setup, "productive_seconds": productive,
        "continuous_hold_proxy_seconds": duration,
        "usable_machine_count": len(machines),
        "line_qualified_worker_count": len(line_workers),
        "factory_qualified_worker_count": len(factory_workers),
        "worker_screen": "AGGREGATE_NECESSARY_CONDITION_ONLY",
    }
    if k < 1 or len(machines) < k or len(factory_workers) < k:
        return {**base, "screen_feasible": False,
                "earliest_start_seconds": None, "estimated_finish_seconds": None,
                "screen_machine_ids": [],
                "reason": "insufficient machine or aggregate worker capacity"}
    ready = _route_ready(problem, index, operation_key)
    reservations = {
        machine_id: _without_operation(
            machine_reservations.get(machine_id, ()), operation_key)
        for machine_id in machines}
    candidate_starts = {ready}
    order_id = operation.order_id
    from crafter.authoritative_semantics import authoritative_changeover_seconds
    for machine_id in machines:
        for _left, right, previous_order, _key in reservations[machine_id]:
            candidate_starts.add(right + authoritative_changeover_seconds(
                previous_order, order_id))
    for start in sorted(value for value in candidate_starts
                        if ready <= value <= problem.horizon_seconds):
        end = start + duration
        available = tuple(machine_id for machine_id in machines
            if _machine_accepts(reservations[machine_id], start, end, order_id))
        if len(available) >= k:
            return {**base, "screen_feasible": True,
                    "earliest_start_seconds": start,
                    "estimated_finish_seconds": end,
                    "screen_machine_ids": list(available[:k]), "reason": None}
    return {**base, "screen_feasible": False,
            "earliest_start_seconds": None, "estimated_finish_seconds": None,
            "screen_machine_ids": [],
            "reason": "no frozen-machine window in problem horizon"}


def audit_machine_identity(problem, index, machine_reservations):
    rows = []
    for operation in problem.operations:
        key, incumbent = operation.operation_key, index.by_key[operation.operation_key]
        line_id = int(incumbent["line_id"])
        selected = tuple(sorted({int(value) for value in incumbent["machine_ids"]}))
        k = len(selected)
        candidates = _machines(problem, operation, line_id)
        start, end = (seconds(incumbent["start_minutes"]),
                      seconds(incumbent["end_minutes"]))
        available = tuple(machine_id for machine_id in candidates
            if _machine_accepts(_without_operation(
                machine_reservations.get(machine_id, ()), key),
                start, end, operation.order_id))
        combination_count = comb(len(available), k) if len(available) >= k else 0
        incumbent_represented = set(selected) <= set(available)
        different = max(0, combination_count-(1 if incumbent_represented else 0))
        alternatives = tuple(value for value in available if value not in selected)
        rows.append({
            "operation_key": key, "order_id": operation.order_id,
            "factory_id": problem.order_by_id[operation.order_id].factory_id,
            "incumbent_line_id": line_id, "incumbent_k": k,
            "incumbent_machine_ids": list(selected),
            "total_usable_eligible_on_incumbent_line": len(candidates),
            "usable_eligible_machine_ids": list(candidates),
            "physically_realizable_machine_ids_at_incumbent_envelope": list(available),
            "usable_alternative_machine_ids": list(alternatives),
            "exact_machine_combination_count": combination_count,
            "different_machine_combination_count": different,
            "machine_id_freedom": different > 0,
            "screen_semantics": (
                "Exact incumbent envelope; all other schedule occupation, fixed "
                "commitments and authoritative incoming/outgoing changeovers frozen."),
        })
    return rows


def audit_timing(problem, index, op_details, operator_index):
    rows = []
    for operation in problem.operations:
        key, row = operation.operation_key, index.by_key[operation.operation_key]
        neighborhood = SimpleNamespace(operation_keys=(key,))
        machine_res = outside_machine_reservations(problem, index, {key})
        worker_res = outside_worker_reservations(problem, index, {key})
        incumbent_start, incumbent_end = (seconds(row["start_minutes"]),
                                          seconds(row["end_minutes"]))
        machines = tuple(sorted({int(value) for value in row["machine_ids"]}))
        workers = tuple(sorted(str(value) for value in row.get("employee_ids", ())))
        setup = authoritative_setup_seconds(op_details[key])
        _setup_minutes, productive_minutes = authoritative_operation_work_minutes(
            op_details[key], len(machines))
        pred, succ = index.route_predecessor.get(key), index.route_successor.get(key)
        earliest = (seconds(index.by_key[pred]["end_minutes"])
                    if pred else max(0, problem.order_by_id[operation.order_id].release_seconds))
        latest = (seconds(index.by_key[succ]["start_minutes"])
                  if succ else problem.horizon_seconds)
        feasible = []
        for start in _candidate_starts(
                problem, index, neighborhood, key, machine_res, worker_res):
            if start == incumbent_start:
                feasible.append((start, incumbent_end)); continue
            try:
                end, _segments = _crew_progress(
                    problem, operator_index, workers, worker_res,
                    start+setup, productive_minutes)
            except Exception:
                continue
            if start < earliest or end > latest:
                continue
            if all(machine_interval_feasible(
                    machine_res[machine_id], start, end, operation.order_id)
                   for machine_id in machines):
                feasible.append((start, end))
        alternatives = sorted({pair for pair in feasible
                               if pair != (incumbent_start, incumbent_end)})
        rows.append({
            "operation_key": key,
            "incumbent_start_seconds": incumbent_start,
            "incumbent_end_seconds": incumbent_end,
            "incumbent_option_present": (incumbent_start, incumbent_end) in feasible,
            "timing_free_possible": bool(alternatives),
            "alternative_timing_option_count": len(alternatives),
            "best_start_gain_seconds": max(
                0,
                max((incumbent_start-start for start, _end in alternatives), default=0),
            ),
            "screen": "EXACT_ANCHORED_CREW_AND_MACHINE_EVENT_SCREEN",
        })
    return rows


def audit_lines_and_k(problem, index, op_details, operator_index,
                      machine_reservations, scheduler=None):
    line_rows, k_rows, combination_rows = [], [], []
    for operation in problem.operations:
        key, row = operation.operation_key, index.by_key[operation.operation_key]
        incumbent_line = int(row["line_id"])
        incumbent_k = len({int(value) for value in row["machine_ids"]})
        factory_id = problem.order_by_id[operation.order_id].factory_id
        line_estimates = []
        all_combinations = []
        for line_id in sorted(set(operation.eligible_line_ids)):
            if problem.line_by_id[line_id].factory_id != factory_id:
                raise AssertionError(f"cross-factory eligible line for {key}")
            machines = _machines(problem, operation, line_id)
            _line_workers, factory_workers = _worker_counts(
                operator_index, factory_id, line_id, operation.required_capability)
            max_count = min(operation.max_machine_count, len(machines),
                            len(factory_workers))
            if scheduler is not None:
                max_count = min(max_count, int(
                    scheduler.factory._max_machines_for(key, line_id)))
            for candidate_k in range(operation.min_machine_count, max_count+1):
                estimate = estimate_line_k(
                    problem, index, op_details, operator_index,
                    machine_reservations, key, line_id, candidate_k)
                if estimate["screen_feasible"]:
                    all_combinations.append(estimate)
            same_k = next((item for item in all_combinations
                           if item["line_id"] == line_id and
                           item["k"] == incumbent_k), None)
            line_estimates.append({
                "line_id": line_id,
                "is_incumbent": line_id == incumbent_line,
                "usable_machine_count": len(machines),
                "nominal_legal_k_values": list(range(
                    operation.min_machine_count, max_count + 1)),
                "same_k_screen": same_k,
            })
        incumbent_estimate = next((item for item in all_combinations
            if item["line_id"] == incumbent_line and item["k"] == incumbent_k), None)
        alternative_lines = [item for item in line_estimates
            if not item["is_incumbent"] and item["same_k_screen"] is not None]
        best_line = min(alternative_lines,
            key=lambda item: (item["same_k_screen"]["estimated_finish_seconds"],
                              item["line_id"]), default=None)
        incumbent_start = (incumbent_estimate["earliest_start_seconds"]
                           if incumbent_estimate else None)
        incumbent_finish = (incumbent_estimate["estimated_finish_seconds"]
                            if incumbent_estimate else None)
        best_alt_start = (best_line["same_k_screen"]["earliest_start_seconds"]
                          if best_line else None)
        best_alt_finish = (best_line["same_k_screen"]["estimated_finish_seconds"]
                           if best_line else None)
        line_rows.append({
            "operation_key": key, "order_id": operation.order_id,
            "factory_id": factory_id, "incumbent_line_id": incumbent_line,
            "incumbent_k": incumbent_k,
            "legal_eligible_line_ids": list(sorted(set(operation.eligible_line_ids))),
            "alternative_legal_line_ids": [value for value in
                sorted(set(operation.eligible_line_ids)) if value != incumbent_line],
            "line_estimates": line_estimates,
            "useful_alternative_line_count": len(alternative_lines),
            "useful_alternative_line_ids": [item["line_id"] for item in alternative_lines],
            "line_freedom": bool(alternative_lines),
            "best_alternative_line_id": best_line["line_id"] if best_line else None,
            "incumbent_earliest_realizable_start_seconds": incumbent_start,
            "best_alternative_earliest_realizable_start_seconds": best_alt_start,
            "potential_start_gain_seconds": max(
                0, (incumbent_start or 0)-(best_alt_start or incumbent_start or 0)),
            "potential_finish_gain_seconds": max(
                0, (incumbent_finish or 0)-(best_alt_finish or incumbent_finish or 0)),
            "screen": "FROZEN_MACHINE_EXACT_AGGREGATE_LABOUR_NECESSARY",
        })

        incumbent_line_options = [item for item in all_combinations
                                  if item["line_id"] == incumbent_line]
        incumbent_line_row = next(item for item in line_estimates
                                  if item["line_id"] == incumbent_line)
        nominal_legal_k = incumbent_line_row["nominal_legal_k_values"]
        legal_k = [item["k"] for item in incumbent_line_options]
        alternate_k = [item for item in incumbent_line_options
                       if item["k"] != incumbent_k]
        best_k = min(alternate_k,
            key=lambda item: (item["estimated_finish_seconds"], item["k"]),
            default=None)
        k_rows.append({
            "operation_key": key, "order_id": operation.order_id,
            "factory_id": factory_id, "incumbent_line_id": incumbent_line,
            "incumbent_k": incumbent_k,
            "nominal_legal_k_values": nominal_legal_k,
            "legal_screened_k_values": legal_k,
            "smaller_feasible_k_values": [value for value in legal_k
                                           if value < incumbent_k],
            "larger_feasible_k_values": [value for value in legal_k
                                          if value > incumbent_k],
            "k_estimates": incumbent_line_options,
            "k_freedom": bool(alternate_k),
            "best_alternative_k": best_k["k"] if best_k else None,
            "incumbent_estimated_finish_seconds": incumbent_finish,
            "best_alternative_estimated_finish_seconds": (
                best_k["estimated_finish_seconds"] if best_k else None),
            "potential_finish_gain_seconds": max(0,
                (incumbent_finish or 0)-(
                    best_k["estimated_finish_seconds"] if best_k else
                    incumbent_finish or 0)),
            "screen": "AUTHORITATIVE_DURATION_FROZEN_MACHINE_AGGREGATE_LABOUR",
        })

        interaction = [item for item in all_combinations
            if item["line_id"] != incumbent_line and item["k"] != incumbent_k]
        interaction.sort(key=lambda item: (
            item["estimated_finish_seconds"], item["earliest_start_seconds"],
            abs(item["k"]-incumbent_k), item["line_id"], item["k"]))
        low_churn = min(interaction, key=lambda item: (
            abs(item["k"]-incumbent_k), item["estimated_finish_seconds"],
            item["line_id"]), default=None)
        promising = []
        for item in [
                (interaction[0] if interaction else None),
                (min(interaction, key=lambda value: (
                    value["earliest_start_seconds"], value["estimated_finish_seconds"]))
                 if interaction else None), low_churn]:
            if item is not None and item not in promising:
                promising.append(item)
        best_combination = interaction[0] if interaction else None
        combination_rows.append({
            "operation_key": key, "order_id": operation.order_id,
            "factory_id": factory_id,
            "incumbent": incumbent_estimate,
            "screened_feasible_combination_count": len(all_combinations),
            "line_k_interaction_count": len(interaction),
            "combined_line_k_possible": bool(interaction),
            "top_promising_combinations": promising[:3],
            "best_combined_estimated_finish_seconds": (
                best_combination["estimated_finish_seconds"]
                if best_combination else None),
            "potential_finish_gain_seconds": max(0,
                (incumbent_finish or 0)-(
                    best_combination["estimated_finish_seconds"]
                    if best_combination else incumbent_finish or 0)),
            "screen": "TOP_COMBINATIONS_ONLY; NO CARTESIAN OPTIONS EXPORTED",
        })
    return line_rows, k_rows, combination_rows


def _index_without_employee(operator_index, line_id, factory_id, skill, employee):
    result = dict(operator_index)
    result.pop("_calendar_pool_cache", None)
    result["by_line"] = dict(operator_index.get("by_line", {}))
    result["by_factory"] = dict(operator_index.get("by_factory", {}))
    line_key, factory_key = ((int(line_id), skill), (int(factory_id), skill))
    result["by_line"][line_key] = [value for value in
        result["by_line"].get(line_key, ()) if str(value) != str(employee)]
    result["by_factory"][factory_key] = [value for value in
        result["by_factory"].get(factory_key, ()) if str(value) != str(employee)]
    return result


def audit_workers(problem, index, op_details, operator_index):
    rows = []
    for operation in problem.operations:
        key, row = operation.operation_key, index.by_key[operation.operation_key]
        line_id = int(row["line_id"])
        factory_id = problem.order_by_id[operation.order_id].factory_id
        skill = operation.required_capability.strip().lower()
        incumbent = tuple(sorted(str(value) for value in row.get("employee_ids", ())))
        k = len(incumbent)
        line_pool, factory_pool = _worker_counts(
            operator_index, factory_id, line_id, skill)
        alternatives = tuple(value for value in factory_pool if value not in incumbent)
        reservations = outside_worker_reservations(problem, index, {key})
        _setup, productive_minutes = authoritative_operation_work_minutes(
            op_details[key], k)
        ready = problem.origin + timedelta(
            seconds=seconds(row["productive_start_minutes"]))
        best = None
        # Removing one incumbent identity forces any success to use a
        # genuinely different complete crew while preserving authoritative
        # calendars and frozen outside reservations.
        if alternatives and len(factory_pool) >= k:
            for excluded in incumbent:
                candidate_index = _index_without_employee(
                    operator_index, line_id, factory_id, skill, excluded)
                result = allocate_crew(
                    candidate_index, skill, line_id, k, ready,
                    productive_minutes, reservations, priority=0.5)
                if result["shortage"] is None and set(map(str, result["employees"])) != set(incumbent):
                    best = result; break
        completion = (int(round((best["end"]-problem.origin).total_seconds()))
                      if best else None)
        incumbent_end = seconds(row["end_minutes"])
        useful = best is not None and completion <= incumbent_end+86_400
        rows.append({
            "operation_key": key, "order_id": operation.order_id,
            "factory_id": factory_id, "line_id": line_id,
            "required_skill": skill, "crew_size": k,
            "incumbent_crew_ids": list(incumbent),
            "qualified_line_worker_count": len(line_pool),
            "qualified_factory_worker_count": len(factory_pool),
            "qualified_alternative_worker_count": len(alternatives),
            "qualified_alternative_worker_ids": list(alternatives),
            "alternative_crew_ids": list(map(str, best["employees"])) if best else [],
            "alternative_sourcing_tier": best["tier"] if best else None,
            "alternative_completion_seconds": completion,
            "incumbent_completion_seconds": incumbent_end,
            "common_calendar_opportunity": useful,
            "worker_freedom": useful,
            "potential_completion_gain_seconds": max(
                0, incumbent_end-(completion or incumbent_end)),
            "status": "WORKER_FREEDOM_SCREEN_ONLY",
            "disclosure": (
                "Authoritative allocator check at incumbent productive start "
                "with one incumbent identity excluded; not a simultaneous "
                "global reassignment proof."),
        })
    return rows


def build_masks_and_recommendations(problem, index, causal_rows, timing_rows,
                                    machine_rows, line_rows, k_rows,
                                    worker_rows, combination_rows):
    maps = [{row["operation_key"]: row for row in values} for values in (
        causal_rows, timing_rows, machine_rows, line_rows, k_rows,
        worker_rows, combination_rows)]
    causal, timing, machine, line, kval, worker, combined = maps
    masks, classes, alignment, recommendations = [], [], [], []
    dimension_candidates = defaultdict(list)
    for operation in problem.operations:
        key = operation.operation_key
        c, t, m, l, k, w, lk = (value[key] for value in maps)
        flags = {
            "timing_free_possible": t["timing_free_possible"],
            "machine_id_free_possible": m["machine_id_freedom"],
            "line_free_possible": l["line_freedom"],
            "k_free_possible": k["k_freedom"],
            "worker_free_possible": w["worker_freedom"],
            "combined_line_k_possible": lk["combined_line_k_possible"],
        }
        magnitudes = {
            "timing_potential_gain_seconds": t["best_start_gain_seconds"],
            "machine_potential_alternative_sets": m["different_machine_combination_count"],
            "line_potential_gain_seconds": max(
                l["potential_start_gain_seconds"], l["potential_finish_gain_seconds"]),
            "k_potential_gain_seconds": k["potential_finish_gain_seconds"],
            "worker_potential_gain_seconds": w["potential_completion_gain_seconds"],
            "combined_line_k_potential_gain_seconds": lk["potential_finish_gain_seconds"],
        }
        mask = {"operation_key": key, "order_id": operation.order_id,
                "factory_id": problem.order_by_id[operation.order_id].factory_id,
                **flags, **magnitudes}
        masks.append(mask)
        active = [name for name, value in (
            ("M", flags["machine_id_free_possible"]),
            ("L", flags["line_free_possible"]),
            ("K", flags["k_free_possible"]),
            ("W", flags["worker_free_possible"])) if value]
        if len(active) >= 3:
            label = "MULTI_RESOURCE"
        elif set(active) == {"L", "K"} and flags["combined_line_k_possible"]:
            label = "LK"
        elif set(active) == {"M", "K"}:
            label = "MK"
        elif set(active) == {"L", "W"}:
            label = "LW"
        elif len(active) == 1:
            label = active[0]
        elif not active and flags["timing_free_possible"]:
            label = "T_ONLY"
        elif not active:
            label = "NO_USEFUL_FREEDOM"
        else:
            label = "AMBIGUOUS"
        classes.append({"operation_key": key, "freedom_class": label,
                        "active_dimensions": active})

        downstream_seconds = int(round(
            index.operation_features[key]["downstream_work_minutes"]*60))
        delivery_pressure = (
            index.operation_features[key]["order_tardiness_seconds"] +
            downstream_seconds + c["machine_wait_seconds"] +
            c["worker_wait_seconds"] + c["calendar_pause_seconds"] +
            c["changeover_delay_seconds"])
        raw = {
            "MACHINE_ID": (c["machine_wait_seconds"]+c["changeover_delay_seconds"])*
                log1p(magnitudes["machine_potential_alternative_sets"])
                if flags["machine_id_free_possible"] else 0.0,
            "LINE": (delivery_pressure+c["machine_wait_seconds"])*
                log1p(1+magnitudes["line_potential_gain_seconds"])
                if flags["line_free_possible"] else 0.0,
            "K": delivery_pressure*log1p(
                1+magnitudes["k_potential_gain_seconds"])
                if flags["k_free_possible"] else 0.0,
            "WORKER": 0.5*(delivery_pressure+c["worker_wait_seconds"]+
                c["calendar_pause_seconds"])*log1p(
                    1+magnitudes["worker_potential_gain_seconds"])
                if flags["worker_free_possible"] else 0.0,
            "LINE_K": (delivery_pressure+c["machine_wait_seconds"])*log1p(
                1+magnitudes["combined_line_k_potential_gain_seconds"])
                if flags["combined_line_k_possible"] else 0.0,
            "TIMING_ONLY": delivery_pressure*log1p(
                1+magnitudes["timing_potential_gain_seconds"])
                if flags["timing_free_possible"] else 0.0,
        }
        recommendation = max(raw, key=lambda name: (raw[name], name))
        if raw[recommendation] <= 0:
            recommendation = "NONE"
        alignment_row = {
            "operation_key": key, "order_id": operation.order_id,
            "order_tardiness_seconds": index.operation_features[key]["order_tardiness_seconds"],
            "delivery_pressure_score": delivery_pressure,
            "realized_delay": c,
            "freedom_mask": flags,
            "dimension_scores": raw,
            "recommended_dimension": recommendation,
            "disclosure": "Deterministic opportunity ranking, not predicted improvement.",
        }
        alignment.append(alignment_row)
        recommendations.append({
            "operation_key": key, "order_id": operation.order_id,
            "recommended_dimension": recommendation,
            "score": raw.get(recommendation, 0.0),
            "delivery_pressure_score": delivery_pressure,
        })
        for dimension in ("LINE", "K", "LINE_K", "WORKER", "MACHINE_ID"):
            if raw[dimension] > 0:
                dimension_candidates[dimension].append({
                    "operation_key": key, "order_id": operation.order_id,
                    "score": raw[dimension], "delivery_pressure": delivery_pressure,
                    "opportunity": magnitudes,
                    "confidence": "SCREEN_ONLY" if dimension == "WORKER" else
                                  "DETERMINISTIC_NECESSARY_SCREEN",
                })
    for values in dimension_candidates.values():
        values.sort(key=lambda row: (-row["score"], row["operation_key"]))
    recommendations.sort(key=lambda row: (-row["score"], row["operation_key"]))
    return masks, classes, alignment, recommendations, dimension_candidates


def freedom_distribution(problem, index, masks, classes):
    class_by_key = {row["operation_key"]: row["freedom_class"] for row in classes}
    total = len(masks)
    fields = ["timing_free_possible", "machine_id_free_possible",
              "line_free_possible", "k_free_possible",
              "worker_free_possible", "combined_line_k_possible"]

    def summarize(rows):
        count = len(rows)
        values = {field: {"count": sum(bool(row[field]) for row in rows),
                          "percentage": (100*sum(bool(row[field]) for row in rows)/count
                                         if count else 0.0)} for field in fields}
        values["timing_only"] = {
            "count": sum(class_by_key[row["operation_key"]] == "T_ONLY" for row in rows),
            "percentage": (100*sum(class_by_key[row["operation_key"]] == "T_ONLY"
                                    for row in rows)/count if count else 0.0)}
        values["no_useful_freedom"] = {
            "count": sum(class_by_key[row["operation_key"]] == "NO_USEFUL_FREEDOM"
                         for row in rows),
            "percentage": (100*sum(class_by_key[row["operation_key"]] ==
                                    "NO_USEFUL_FREEDOM" for row in rows)/count
                           if count else 0.0)}
        values["multi_resource"] = {
            "count": sum(class_by_key[row["operation_key"]] == "MULTI_RESOURCE"
                         for row in rows),
            "percentage": (100*sum(class_by_key[row["operation_key"]] ==
                                    "MULTI_RESOURCE" for row in rows)/count
                           if count else 0.0)}
        return {"operations": count, "freedoms": values}

    p75 = sorted(index.operation_scores.values())[int(0.75*(total-1))]
    by_factory, by_line = {}, {}
    for factory_id in sorted({row["factory_id"] for row in masks}):
        by_factory[str(factory_id)] = summarize(
            [row for row in masks if row["factory_id"] == factory_id])
    for line_id in sorted({int(index.by_key[row["operation_key"]]["line_id"])
                           for row in masks}):
        by_line[str(line_id)] = summarize([row for row in masks
            if int(index.by_key[row["operation_key"]]["line_id"]) == line_id])
    late = [row for row in masks if
            index.operation_features[row["operation_key"]]["order_tardiness_seconds"] > 0]
    nonlate = [row for row in masks if row not in late]
    high = [row for row in masks if index.operation_scores[row["operation_key"]] >= p75]
    low = [row for row in masks if row not in high]
    return {
        "overall": summarize(masks),
        "class_counts": dict(sorted(Counter(class_by_key.values()).items())),
        "by_factory": by_factory, "by_incumbent_line": by_line,
        "late_order_operations": summarize(late),
        "non_late_order_operations": summarize(nonlate),
        "high_tardiness_impact_operations": summarize(high),
        "lower_tardiness_impact_operations": summarize(low),
        "high_impact_threshold": p75,
    }
