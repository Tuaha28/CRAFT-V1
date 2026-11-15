"""All Kaggle CRAFT V3 runs -> results.json (one record per run).

Reads the notebook logs (single-run notebooks and two-run batch notebooks),
keeps the per-method KPI lines, the start-plan scores and per-run seconds."""
import json
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
KPI = re.compile(r"^(\w+) complete (\d+) partial (\d+) makespan_d ([\d.]+) tardiness_d ([\d.]+) on_time (\d+) "
                 r"flow_d ([\d.]+) workers_used (\d+) worker_util_used% ([\d.]+)")
runs = {}
for f in sorted(root.rglob("*.log")):
    if f.parent.name == "one":
        continue
    s = f.read_text(encoding="utf-8")
    rows = json.loads(s[s.index("["):])
    name = f.stem.replace("-", "_") if not f.stem.startswith("batch") else None
    start_t = 0.0
    lines = []
    for r in rows:
        if r["stream_name"] != "stdout":
            continue
        for l in r["data"].splitlines():
            lines.append((r["time"], l))
    cur = None
    for t, l in lines:
        if l.startswith("START "):
            name, start_t = l.split()[1], t
        if name is None:
            continue
        rec = runs.setdefault(name, {"methods": {}, "notebook": f.stem})
        if l.startswith("EXIT "):
            rec["exit"] = int(l.split()[-1])
            rec["seconds"] = round(t - start_t, 1)
        if l.startswith('{"pace'):
            d = json.loads(l)
            rec["starts"] = [{k: st[k] for k in ("start", "operations", "validation", "score", "chosen")} for st in d["starts"]]
            rec["final_score"] = d["final_score"]
            rec["improve_seconds"] = d.get("seconds")
        m = KPI.match(l)
        if m:
            k = m.groups()
            rec["methods"][k[0]] = {"complete": int(k[1]), "partial": int(k[2]), "makespan_d": float(k[3]),
                                    "tardiness_d": float(k[4]), "on_time": int(k[5]), "flow_d": float(k[6]),
                                    "workers": int(k[7]), "util_pct": float(k[8])}
    if not f.stem.startswith("batch"):  # single notebooks: whole log is one run
        runs[name]["seconds"] = round(max(t for t, _ in lines), 1)
for n, r in sorted(runs.items()):
    assert len(r["methods"]) == 7, (n, list(r["methods"]))
    print(n, r["notebook"], r.get("seconds"), r["methods"]["CRAFT_V3"]["tardiness_d"])
Path(sys.argv[2]).write_text(json.dumps(runs, indent=1), encoding="utf-8")
