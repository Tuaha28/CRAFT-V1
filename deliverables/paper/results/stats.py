"""results.json (+ optional V3.1 rerun) -> paper_data.json: per-case metrics,
win/tie/loss and Wilcoxon signed-rank tests of CRAFT against every method.

Late orders = 10 - on-time (an unfinished order is not on time). Tardiness
is compared only where both methods finished all 10 orders: an unfinished
order's lateness is not in the total, so a partial plan looks better."""
import json
import sys
from pathlib import Path

from scipy.stats import wilcoxon

runs = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
override = Path(sys.argv[2]) if len(sys.argv) > 3 else None  # V3.1 rerun of 90%-S3, when available
out = Path(sys.argv[-1])
# Operations per case = the most any plan placed in the original run (a plan may leave work unplaced).
OPS = {k: max(s["operations"] for s in r["starts"]) for k, r in runs.items()}
if override and override.exists():
    v31 = json.loads(override.read_text(encoding="utf-8"))
    runs["load90_set3"] = {**v31["load90_set3_v31"], "v31": True}

CASES = [(f"load{l}_set{s}", f"{l}%-S{s}") for l in (30, 60, 90) for s in (1, 2, 3)]
METHODS = {"FIFO": "FIFO", "EDD": "EDD", "SPT": "SPT", "EDD_CREW": "EDD-Crew",
           "SAVABLE_FIRST_CREW": "Savable-First-Crew", "CRAFT_TW": "CP-SAT plan", "CRAFT_V3": "CRAFT V3"}


def row(m):
    return {"complete": m["complete"], "late": 10 - m["on_time"], "mean_tard": round(m["tardiness_d"] / 10, 2),
            "makespan": m["makespan_d"], "flow": m["flow_d"], "workers": m["workers"], "util": m["util_pct"]}


# Orders already due at the reference time (overdue.py): late under every method.
overdue_path = Path(sys.argv[1]).with_name("overdue.json")
OVERDUE = json.loads(overdue_path.read_text(encoding="utf-8")) if overdue_path.exists() else {}

cases = []
for key, label in CASES:
    r = runs[key]
    chosen = next((s["start"] for s in r["starts"] if s["chosen"]), None)
    cases.append({"key": key, "label": label, "ops": max(OPS[key], *(s["operations"] for s in r["starts"])),
                  "seconds": r["seconds"], "start": METHODS.get(chosen, chosen), "v31": bool(r.get("v31")),
                  "methods": {METHODS[k]: row(v) for k, v in r["methods"].items()},
                  "overdue": OVERDUE.get(key)})
    if OVERDUE:  # sanity: an order due before the plan starts is late under every method
        assert all(m["late"] >= OVERDUE[key] for m in cases[-1]["methods"].values()), key


def compare(other, metric):
    a, b = [], []
    for c in cases:
        if c["key"] == "load90_set3" and not c["v31"]:
            continue  # V3 result superseded by the V3.1 rerun; left out until it lands
        x, y = c["methods"]["CRAFT V3"], c["methods"][other]
        if metric == "mean_tard" and (x["complete"] < 10 or y["complete"] < 10):
            continue
        a.append(x[metric]); b.append(y[metric])
    win = sum(p < q - 1e-9 for p, q in zip(a, b)); loss = sum(p > q + 1e-9 for p, q in zip(a, b))
    diffs = [p - q for p, q in zip(a, b) if abs(p - q) > 1e-9]
    pval = float(wilcoxon(diffs).pvalue) if diffs else None
    return {"n": len(a), "win": win, "tie": len(a) - win - loss, "loss": loss, "p": pval,
            "mean_craft": sum(a) / len(a) if a else None, "mean_other": sum(b) / len(b) if b else None}


comparisons = {m: {"late": compare(m, "late"), "mean_tard": compare(m, "mean_tard")}
               for m in METHODS.values() if m != "CRAFT V3"}
repeats = []
for l in (30, 60, 90):
    reps = [runs[f"load{l}_set1"], runs[f"load{l}_set1_rep2"], runs[f"load{l}_set1_rep3"]]
    repeats.append({"label": f"{l}%-S1", "tard": [r["methods"]["CRAFT_V3"]["tardiness_d"] for r in reps],
                    "late": [10 - r["methods"]["CRAFT_V3"]["on_time"] for r in reps],
                    "workers": [r["methods"]["CRAFT_V3"]["workers"] for r in reps]})
data = {"cases": cases, "comparisons": comparisons, "repeats": repeats,
        "seconds": [r["seconds"] for r in runs.values()], "v31_done": any(c["v31"] for c in cases)}
out.write_text(json.dumps(data, indent=1), encoding="utf-8")
for m, v in comparisons.items():
    print(f"{m:20s} late {v['late']}  tard {v['mean_tard']}")
