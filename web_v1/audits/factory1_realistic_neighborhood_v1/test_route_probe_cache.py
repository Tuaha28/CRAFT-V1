"""Source-derived exact regime parity for iterative route-ready probes."""
from __future__ import annotations

import json
import unittest

from crafter.repair.calendar_regimes import CalendarRegimeCache, generate_calendar_regimes
from .run_bounded import HERE, construct, next_earliest_order_keys


class RouteProbeCacheTests(unittest.TestCase):
    def test_cold_and_warmed_final_regimes_are_identical(self):
        keys = next_earliest_order_keys()
        _source, _snapshot, _scenario, state, configs, _ledger, _domain = construct(keys)
        saved = json.loads((HERE / "bounded_25_full_evidence_v1.json").read_text(encoding="utf-8"))
        window = saved["windows"][0]
        selected = tuple(window["keys"])
        state = {**state, "keys": selected}
        chosen = {key: configs[key] for key in selected}
        final_probes = window["route_ready_breakpoint_probes"]
        cold, _, cold_count, cold_checks = generate_calendar_regimes(
            state, chosen, additional_breakpoints_by_operation=final_probes)
        cache = CalendarRegimeCache()
        initial = {key: tuple(probes[:1]) for key, probes in final_probes.items()}
        generate_calendar_regimes(state, chosen, cache=cache,
                                  additional_breakpoints_by_operation=initial)
        warmed, _, warm_count, warm_checks = generate_calendar_regimes(
            state, chosen, cache=cache,
            additional_breakpoints_by_operation=final_probes)
        self.assertEqual(cold, warmed)
        self.assertEqual(cold_count, warm_count)
        self.assertEqual(cold_checks["mismatches"], 0)
        self.assertEqual(warm_checks["mismatches"], 0)
        self.assertGreater(cache.snapshot()["execution_hits"], 0)


if __name__ == "__main__":
    unittest.main()
