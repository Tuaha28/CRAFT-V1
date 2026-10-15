import copy
from datetime import datetime

import pandas as pd

from ga import rolling_horizon
from utils.factory_ownership import OwnershipSnapshot


T0 = datetime(2026, 3, 3, 5)
RESUMED = (datetime(2026, 3, 3, 6), datetime(2026, 3, 3, 8))
NEW_1 = (datetime(2026, 3, 3, 8), datetime(2026, 3, 3, 9))
NEW_2 = (datetime(2026, 3, 3, 9), datetime(2026, 3, 3, 10))


class Factory:
    eligibility_map = {}
    machine_ids_per_line_map = {"1_1": {1: [101]}, "2_1": {1: [102]}}
    optype_for_op = {"1_1": "body assembly", "2_1": "body assembly"}


class FakeScheduler:
    def __init__(self, sub, *args, **kwargs):
        oid = str(sub.iloc[0]["production_order_id"])
        self.oid = oid
        self.num_ops = 1
        self.factory = Factory()
        self.op_details = {
            f"{oid}_1": {
                "setup_mins": 0.0,
                "serial_processing_mins": 60.0,
            }
        }
        self.gnn_predictions = {}
        self.active_line_ids = [1]
        self.order_deadlines_minutes = {oid: 1000.0}
        self.order_deadline_abs = {}
        self.order_release_minutes = {oid: 0.0}
        self.machine_line_map = {101: 1, 102: 1}
        self.usable_machine_count = 2
        self.present_operator_count = 1
        self.seq_lookup = {f"{oid}_1": 1}
        self.protected_machine_type = None
        self.worker_availability = None
        self.now = T0


class FakeExporter:
    def __init__(self, eval_starts, eval_ends):
        self.eval_starts = eval_starts
        self.eval_ends = eval_ends
        self.calls = 0

    def _export_proposed_schedule(self, *args, **kwargs):
        index = self.calls
        self.calls += 1
        assert kwargs["initial_operator_reservations"] == self.eval_starts[index]
        return {
            "phenotype_hash": f"h{index}",
            "operator_reservations_out": self.eval_ends[index],
            "understaffed_operations": 0,
            "execution_feasible": True,
        }


def test_actual_rolling_runner_carries_segment_lists_without_collapsing(monkeypatch):
    starts, ends = [], []
    additions = (NEW_1, NEW_2)

    def fake_baselines(factory, details, deadlines, sequence):
        key = next(iter(details))
        return {"EDF": [key, 1, 1]}

    def fake_evaluate(chromosome, num_ops, *args, **kwargs):
        initial = copy.deepcopy(kwargs["initial_operator_reservations"])
        starts.append(initial)
        final = copy.deepcopy(initial)
        final.setdefault("7001", []).append(additions[len(starts) - 1])
        ends.append(final)
        oid = chromosome[0].split("_")[0]
        det = {
            "final_machine_free": {},
            "final_machine_last_order": {},
            "final_operator_reservations": final,
            "order_lateness_hours": {oid: 0.0},
            "order_completion_minutes": {oid: 60.0},
            "labor_used_minutes": 60.0,
            "fitness": 1.0,
            "fitness_components": {
                "makespan": 1.0, "delay_penalty": 0.0,
                "priority_reward": 0.0, "load_balance": 0.0,
                "lateness": 0.0, "idle_labour": 0.0,
            },
            "fitness_lateness_split": {},
            "max_tardiness_hours": 0.0,
            "pool_short_operations": 0,
            "phenotype_hash": f"h{len(starts) - 1}",
        }
        return (1.0,), det

    monkeypatch.setattr(rolling_horizon, "GAScheduler", FakeScheduler)
    monkeypatch.setattr(rolling_horizon, "baseline_chromosomes", fake_baselines)
    monkeypatch.setattr(rolling_horizon, "evaluate_schedule", fake_evaluate)

    routings = pd.DataFrame([
        {"production_order_id": 1, "operation_id": 1, "release_date": T0},
        {"production_order_id": 2, "operation_id": 1, "release_date": T0},
    ])
    initial = {"7001": [RESUMED]}
    exporter = FakeExporter(starts, ends)
    result = rolling_horizon.run_rolling_horizon_baseline(
        "EDF", routings, pd.DataFrame(), pd.DataFrame(), {},
        capacity=1, origin=T0, commit_size=1,
        ownership_snapshot=OwnershipSnapshot((("1", 1), ("2", 1))),
        initial_operator_reservations=initial,
        committed_machine_state={},
        committed_machine_last_order={},
        operator_index={"sentinel": True},
        export=True, decision_engine=exporter,
        log_fn=lambda *args, **kwargs: None,
    )

    assert starts[0] == {"7001": [RESUMED]}
    assert starts[1] == {"7001": [RESUMED, NEW_1]}
    assert result["export_summaries"][1]["operator_reservations_out"] == {
        "7001": [RESUMED, NEW_1, NEW_2]
    }
    assert all(isinstance(key, str) for state in starts for key in state)
