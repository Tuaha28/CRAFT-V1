"""Offline, pinned Factory-1 cache-equivalence check; no legacy DB access."""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import timedelta
from hashlib import sha256
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
import pymysql
pymysql.connect = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("LEGACY_DB_FORBIDDEN"))

from crafter.repair.calendar_regimes import CalendarRegimeCache, generate_calendar_regimes
from web_v1.audits.factory1_realistic_neighborhood_v1.analyze_scope import SOURCE
from web_v1.audits.factory1_realistic_neighborhood_v1.run_bounded import construct


def digest(regimes):
    rows = {key: [{"config": asdict(row["config"]), "lo": row["lo"],
                   "hi": row["hi"], "formula": row["formula"],
                   "segments": row["segments"]} for row in values]
            for key, values in regimes.items()}
    return sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()


def main():
    started = time.perf_counter()
    key = "790_972"
    _source, normalized, scenario, state, configs, _ledger, _domain = construct(
        (key,), domain_width=2, resource_policy="stage_b_diverse")
    cache = CalendarRegimeCache()
    first = generate_calendar_regimes(state, configs, cache=cache)
    warmed = generate_calendar_regimes(state, configs, cache=cache)
    fresh = generate_calendar_regimes(state, configs, cache=CalendarRegimeCache())
    cases = {"unchanged": (warmed, fresh)}
    changed = {key: tuple(replace(config,
                                  productive_seconds=config.productive_seconds + 600,
                                  productive_minutes=config.productive_minutes + 10)
                           for config in configs[key])}
    cases["duration_changed"] = (
        generate_calendar_regimes(state, changed, cache=cache),
        generate_calendar_regimes(state, changed, cache=CalendarRegimeCache()))
    worker = configs[key][0].worker_ids[0]
    reserved = {**state, "_worker_res": {**state["_worker_res"],
                str(worker): [*state["_worker_res"].get(str(worker), ()),
                              (state["problem"].origin,
                               state["problem"].origin + timedelta(hours=2))]}}
    cases["reservation_changed"] = (
        generate_calendar_regimes(reserved, configs, cache=cache),
        generate_calendar_regimes(reserved, configs, cache=CalendarRegimeCache()))
    first_regime = first[0][key][0]
    machine = first_regime["config"].machine_ids[0]
    occupied_start = int(first_regime["lo"])
    committed = {**state, "_machine_res": {**state["_machine_res"],
                 int(machine): [*state["_machine_res"].get(int(machine), ()),
                                (occupied_start, occupied_start + 3600,
                                 "outside-order", "approved-test") ]}}
    cases["machine_commitment_changed"] = (
        generate_calendar_regimes(committed, configs, cache=cache),
        generate_calendar_regimes(committed, configs, cache=CalendarRegimeCache()))
    results = {name: {"cached_sha256": digest(pair[0][0]),
                      "uncached_sha256": digest(pair[1][0]),
                      "cached_regimes": pair[0][2], "uncached_regimes": pair[1][2],
                      "equivalent": digest(pair[0][0]) == digest(pair[1][0])
                                    and pair[0][2] == pair[1][2]}
               for name, pair in cases.items()}
    results["unchanged"]["initial_sha256"] = digest(first[0])
    results["machine_commitment_changed"]["different_from_uncommitted"] = (
        results["machine_commitment_changed"]["cached_sha256"] != digest(first[0]))
    report = {"classification": "SIMULATION_ONLY_NOT_PRODUCTION",
              "source_sha256": sha256(SOURCE.read_bytes()).hexdigest(),
              "snapshot_hash": normalized["snapshot_hash"],
              "scenario_hash": scenario["scenario_hash"],
              "operation_key": key, "configurations": len(configs[key]),
              "cases": results, "cache_profile": cache.snapshot(),
              "elapsed_seconds": time.perf_counter() - started,
              "status": "PASS" if all(row["equivalent"] for row in results.values())
                        and results["machine_commitment_changed"]["different_from_uncommitted"]
                        and results["unchanged"]["initial_sha256"] == results["unchanged"]["cached_sha256"]
                        else "FAIL"}
    target = Path(__file__).with_name("cache_equivalence_v1.json")
    target.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "cases": results,
                      "elapsed_seconds": report["elapsed_seconds"], "path": str(target)}))
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
