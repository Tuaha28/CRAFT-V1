"""Reconstruct saved authoritative objective components and classify control."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ga.fitness import ALPHA, GAMMA, DELTA, EPSILON, ZETA, SERVICE_LEVEL_WEIGHT
from utils.config import IDLE_LABOR_WEIGHT

SOURCE = Path("outputs/authoritative_comparison_report.json")
OUT = Path("outputs/integration_audit/objective_reconstruction.json")


def main():
    report = json.loads(SOURCE.read_text(encoding="utf-8"))
    rows = []
    max_residual = 0.0
    for arm, values in report["per_window"].items():
        for row in values:
            components = row["fitness_components"]
            residual = sum(components.values()) - row["fitness"]
            max_residual = max(max_residual, abs(residual))
            rows.append({
                "arm": arm,
                "window": row["window"],
                "fitness": row["fitness"],
                "component_sum": sum(components.values()),
                "sum_residual": residual,
                "components": components,
            })

    component_names = list(rows[0]["components"])
    ranges = {
        name: {
            "min": min(r["components"][name] for r in rows),
            "max": max(r["components"][name] for r in rows),
        }
        for name in component_names
    }
    per_window_delay = {}
    for window in sorted({r["window"] for r in rows}):
        values = [r["components"]["delay_penalty"] for r in rows if r["window"] == window]
        per_window_delay[str(window)] = {
            "values": values,
            "range": max(values) - min(values),
            "constant_across_arms": max(values) == min(values),
        }

    result = {
        "source_artifact": str(SOURCE),
        "source_snapshot": report.get("snapshot"),
        "source_seed": report.get("seed"),
        "weights": {
            "ALPHA_makespan": ALPHA,
            "GAMMA_delay": GAMMA,
            "DELTA_priority": DELTA,
            "EPSILON_load_balance": EPSILON,
            "ZETA_avg_lateness": ZETA,
            "SERVICE_LEVEL_WEIGHT": SERVICE_LEVEL_WEIGHT,
            "IDLE_LABOR_WEIGHT": IDLE_LABOR_WEIGHT,
        },
        "formula": "makespan_hours + mean(delay_prob*50) - mean(priority*position_weight*100) + line_busy_stdev_hours + 3*avg_tardiness_hours_all_deadlined_orders + 24*fraction_late + idle_labour_penalty",
        "component_sum": {
            "rows_checked": len(rows),
            "max_abs_residual": max_residual,
            "all_match_1e_9": max_residual <= 1e-9,
        },
        "observed_component_ranges_saved_authoritative_run": ranges,
        "delay_by_window": per_window_delay,
        "candidate_control": {
            "makespan": "OS/LA/MC through decoded machine timing",
            "delay_penalty": "none; predictions fixed per operation and averaged once each",
            "priority_reward": "OS position only; fixed predictions",
            "load_balance": "LA/MC directly and OS indirectly through resource use",
            "lateness": "OS/LA/MC through order completion",
            "idle_labour": "MC and makespan/resource timing",
        },
        "qualification": "Saved report predates IA-003 machine-pool filtering and IA-007 prior-corrected scheduling probabilities. Sum identity is valid; exact values are historical/qualified.",
        "rows": rows,
    }
    OUT.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({k: result[k] for k in (
        "source_snapshot", "source_seed", "weights", "component_sum",
        "observed_component_ranges_saved_authoritative_run", "delay_by_window",
        "candidate_control", "qualification")}, indent=2))


if __name__ == "__main__":
    main()
