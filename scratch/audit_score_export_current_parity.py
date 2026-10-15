"""Compare normal production fitness decode with export decode on current data."""
import json
import random
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent.decision_engine as decision_module
import ga.fitness as fitness_module
from agent.decision_engine import AIDecisionEngine
from database.db import get_engine
from database.queries import (
    fetch_active_orders_and_routings, fetch_committed_machine_state,
    fetch_concurrent_operator_capacity, fetch_machine_state,
)
from ga.chromosome import creator
from ga.heuristics import baseline_chromosomes
from ga.scheduler import GAScheduler
from utils.run_artifacts import current_run_id

OUT=Path("outputs/integration_audit/score_export_current_parity.json")
EXPORT_DIR=Path("outputs/integration_audit/current_export_probe")


def main():
    random.seed(42)
    origin=datetime.now()
    routings=fetch_active_orders_and_routings()
    orders=(routings.groupby("production_order_id",as_index=False)["release_date"].min()
            .sort_values(["release_date","production_order_id"])
            ["production_order_id"].head(12))
    order_ids=set(orders)
    window=routings[routings["production_order_id"].isin(order_ids)].copy()
    machines=fetch_machine_state()
    lines=pd.read_sql(text(
        "SELECT line_id, department_id, line_number, target_efficiency_percentage, "
        "is_active FROM Production_Lines WHERE is_active=1"),get_engine())
    free,last=fetch_committed_machine_state(origin)
    preds={f"{r.production_order_id}_{r.operation_id}":{"delay":0.30,"priority":0.50}
           for r in window.itertuples(index=False)}
    scheduler=GAScheduler(
        window,machines,lines,preds,current_time=origin,
        present_operator_count=fetch_concurrent_operator_capacity(),
        initial_machine_free=free,initial_machine_last_order=last,
        scored_order_ids={str(x) for x in order_ids})
    chromosome=creator.Individual(baseline_chromosomes(
        scheduler.factory,scheduler.op_details,scheduler.order_deadlines_minutes,
        scheduler.seq_lookup)["EDF"])
    n=scheduler.num_ops

    calls=[]
    real_select=fitness_module.select_machines_and_start
    def traced(pool_ids,machine_free,machine_last_order,order_id,machine_count,
               ready,changeover,**kwargs):
        start,chosen=real_select(pool_ids,machine_free,machine_last_order,order_id,
                                 machine_count,ready,changeover,**kwargs)
        calls.append({"start_minutes":float(start),
                      "machines":[int(x) for x in chosen]})
        return start,chosen
    fitness_module.select_machines_and_start=traced
    try:
        _fit,details=fitness_module.evaluate_schedule(
            chromosome,return_details=True,**scheduler._eval_ctx)
    finally:
        fitness_module.select_machines_and_start=real_select
    for i,c in enumerate(calls):
        c["operation_key"]=chromosome[i]
        c["line"]=int(chromosome[n+i])

    engine=object.__new__(AIDecisionEngine)
    engine.run_id=current_run_id()
    old_output=decision_module.OUTPUT_DIR
    decision_module.OUTPUT_DIR=str(EXPORT_DIR)
    try:
        summary=engine._export_proposed_schedule(
            chromosome,n,scheduler.op_details,preds,lines,
            scheduler.factory.eligibility_map,scheduler.order_deadline_abs,
            window,machines,label="current_parity",
            machine_pools=scheduler.factory.machine_ids_per_line_map,
            operator_index={"by_line":{},"by_factory":{},"by_skill":{},
                            "line_factory":{},"efficiency_of":{}},
            present_operator_count=scheduler.present_operator_count,
            usable_machine_count=scheduler.usable_machine_count,
            op_optype=scheduler.factory.optype_for_op,
            protected_machine_type=scheduler.protected_machine_type,
            initial_machine_free=free,initial_machine_last_order=last,
            initial_operator_reservations={},base_time=origin)
    finally:
        decision_module.OUTPUT_DIR=old_output

    exported=json.loads((EXPORT_DIR/"proposed_schedule_current_parity.json").read_text())
    exported_completions=json.loads(
        (EXPORT_DIR/"order_completions_current_parity.json").read_text())
    by_op={f"{r['production_order_id']}_{r['operation_id']}":r for r in exported}
    completion_comparisons=[]
    for row in exported_completions:
        oid=str(row["production_order_id"])
        export_minutes=(
            datetime.fromisoformat(row["predicted_completion"])-origin
        ).total_seconds()/60.0
        completion_comparisons.append({
            "order_id":oid,
            "completion_delta_seconds":60.0*(
                export_minutes-details["order_completion_minutes"][oid]),
            "tardiness_delta_hours":float(row["schedule_added_late_hours"])-
                details["order_lateness_hours"][oid],
        })
    comparisons=[]
    for c in calls:
        e=by_op[c["operation_key"]]
        export_start=(datetime.fromisoformat(e["proposed_start_time"])-origin).total_seconds()/60
        comparisons.append({
            "operation_key":c["operation_key"],
            "same_line":c["line"]==int(e["assigned_line_id"]),
            "same_machines":c["machines"]==e["machine_ids_used"],
            "start_delta_seconds":60.0*(export_start-c["start_minutes"]),
        })
    export_completion={
        str(r["production_order_id"]):
        (datetime.fromisoformat(r["cushioned_end_time"])-origin).total_seconds()/60
        for r in exported
    }
    # The last row encountered for an order is its routing-final operation
    # because OS precedence is valid; compare to evaluator rollup.
    result={
        "scope":{"orders":len(order_ids),"operations":n,
                 "committed_machines":len(free),"delay_probability":0.30,
                 "risk_override_triggered":summary["high_risk_reroutes"]},
        "normal_machine_decode":{
            "line_matches":sum(x["same_line"] for x in comparisons),
            "machine_identity_matches":sum(x["same_machines"] for x in comparisons),
            "max_abs_start_delta_seconds":max(abs(x["start_delta_seconds"]) for x in comparisons),
            "all_lines_match":all(x["same_line"] for x in comparisons),
            "all_machines_match":all(x["same_machines"] for x in comparisons),
        },
        "completion_deadline_parity":{
            "orders_compared":len(completion_comparisons),
            "max_abs_completion_delta_seconds":max(
                abs(x["completion_delta_seconds"]) for x in completion_comparisons),
            "max_abs_tardiness_delta_hours":max(
                abs(x["tardiness_delta_hours"]) for x in completion_comparisons),
            "tardiness_note":"export JSON rounds schedule_added_late_hours to one decimal",
        },
        "known_separate_export_state":{
            "understaffed_operations":summary["understaffed_operations"],
            "execution_feasible":summary["execution_feasible"],
            "reason":"operator index deliberately empty; worker identity is export-only and outside machine parity"
        },
        "comparisons":comparisons,
    }
    OUT.write_text(json.dumps(result,indent=2),encoding="utf-8")
    print(json.dumps({k:result[k] for k in ("scope","normal_machine_decode","known_separate_export_state")},indent=2))


if __name__=="__main__":
    main()
