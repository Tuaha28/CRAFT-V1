"""Final CRAFT vs dispatch-rule experiments on DATA v2, run one job at a time.

Usage:
  venv/Scripts/python.exe scratch/run_final_experiments.py <out_root> --factory 4
      --loads 30 60 [--release-days 7] [--lns 1800]

Per load level (capture located by its source database name) it runs, in order:
  1. CRAFT final + EDD with the same features (lanes, adaptive crews):
     due-date windows, lazy EDD guard, lookahead, contention ALNS, long limits;
  2. plain FIFO / EDD / SPT as factories use them (no lanes, one worker).
Common physics for every job: residual routes, same-factory crew fallback,
the same validators. Jobs never overlap, so solver time limits stay fair.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
CAPTURES = ROOT / "web_v1/audits/factory_source_integration_v1"


def capture_for(load):
    for report in sorted(CAPTURES.glob("capture_*/report.json")):
        database = json.loads(report.read_text(encoding="utf-8")).get("dataset", {}).get("database", "")
        if f"_load{load}_" in database:
            return report.parent.relative_to(ROOT).as_posix()
    raise SystemExit(f"NO_CAPTURE_FOR_LOAD_{load}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("out_root")
    p.add_argument("--factory", type=int, default=4)
    p.add_argument("--loads", type=int, nargs="+", default=[30, 60, 90])
    p.add_argument("--release-days", type=int, default=7)
    p.add_argument("--lns", type=float, default=1800)
    a = p.parse_args()
    out_root = Path(a.out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "CRAFT_RESIDUAL_ROUTES": "1", "CRAFT_WORKER_LINE_POLICY": "BATCH_LINE_THEN_FACTORY",
           "CRAFT_STAGE_SECONDS": "10,20,30", "CRAFT_OBJECTIVE_SECONDS": "30", "PYTHONUNBUFFERED": "1"}
    script = str(ROOT / "scratch/run_lane_experiment.py")
    base = [sys.executable, script, str(a.factory)]
    common = ["--release-days", str(a.release_days)]
    for load in a.loads:
        env["CRAFT_CAPTURE_DIR"] = capture_for(load)
        jobs = [
            ("featured", common + ["--crew", "4", "--window-order", "DUE_DATE", "--guard",
                                   "--lookahead", "10", "--lns", str(a.lns), "--rules", "EDD"]),
            ("plain", common + ["--no-lanes", "--skip-craft", "--rules", "FIFO", "EDD", "SPT"]),
        ]
        if load <= 30:
            # Spare capacity: FAST mode finishes work early with idle workers.
            jobs.append(("fast", common + ["--crew", "4", "--window-order", "DUE_DATE", "--guard",
                                           "--lookahead", "10", "--lns", str(a.lns), "--rules"]))
        for name, args in jobs:
            env["CRAFT_OBJECTIVE_MODE"] = "FAST" if name == "fast" else "ECONOMY"
            out = out_root / f"f{a.factory}_load{load}_{name}"
            if (out / "report.json").exists():
                continue
            started = time.time()
            log = (out_root / f"f{a.factory}_load{load}_{name}.log").open("w", encoding="utf-8")
            code = subprocess.call(base + [str(out)] + args, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
            print(json.dumps({"job": out.name, "exit": code, "capture": env["CRAFT_CAPTURE_DIR"],
                              "hours": round((time.time() - started) / 3600, 2)}), flush=True)


if __name__ == "__main__":
    main()
