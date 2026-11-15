"""Kaggle runs of Nov. 2026 (Set 1 at each load, three settings) -> sensitivity.json.

A  the paper's settings: 8 solver workers, windows commit 8 h, 1,800 s improvement
B  4 solver workers (= vCPUs), windows commit 24 h, 1,800 s improvement
C  as B with 3,600 s improvement
Run A repeats the paper's Set 1 cases a fourth time; B and C test the settings.

Usage: python sensitivity.py <dir with load30/60/90_summary.json> <out sensitivity.json>"""
import json
import sys
from pathlib import Path

src, out = Path(sys.argv[1]), Path(sys.argv[2])
NAMES = {"A_today": "A", "B_tuned": "B", "C_tuned_more_time": "C"}
METHODS = {"EDD_CREW": "EDD-Crew", "SAVABLE_FIRST_CREW": "Savable-First-Crew", "CRAFT_TW": "CP-SAT plan",
           "CRAFT_V3": "CRAFT V3"}
cases = []
for load in (30, 60, 90):
    for v in json.loads((src / f"load{load}_summary.json").read_text(encoding="utf-8")):
        assert v["exit"] == 0, (load, v["variant"])
        cases.append({"label": f"{load}%-S1", "setting": NAMES[v["variant"]], "minutes": v["minutes"],
                      "methods": {name: {"complete": v[k]["orders_complete"], "late": 10 - v[k]["on_time_orders"],
                                         "tard": v[k]["total_tardiness_days"]} for k, name in METHODS.items()}})
out.write_text(json.dumps({"cases": cases}, indent=1), encoding="utf-8")
for c in cases:
    print(c["label"], c["setting"], c["minutes"], {m: (x["late"], x["tard"]) for m, x in c["methods"].items()})
