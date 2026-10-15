"""Fresh Factory-2 run for the shallow CRAFT production controller."""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
OUT = os.environ.get("CRAFT_PRODUCTION_V1_OUT",
                    os.path.join(ROOT, "outputs", "crafter", "factory2_172_production_v1"))
os.environ.update({
    "CRAFT_STEP4D_OUT": OUT,
    "CRAFT_PRODUCTION_V1": "1",
    "CRAFT_FAST_MODE": "0",
    "CRAFT_FAST_EVIDENCE": "1",
    "CRAFT_ONE_FACTORY_ID": "2",
    "CRAFT_ONE_FACTORY_NORMAL_CAP": "22",
    "CRAFT_ONE_FACTORY_MAX_MACHINE_SETS": "1",
    # Production starts from the compact K3 matching domain.  The historical
    # K1/K2/K3 solve ladder remains available to research callers, but the
    # accepted five-window control proved each smaller solve redundant.
    "CRAFT_ONE_FACTORY_MAX_WORKER_CREWS": "3",
    "CRAFT_ONE_FACTORY_RESOURCE_DOMAIN_V3": "0",
    "CRAFT_ONE_FACTORY_ADAPTIVE_CREW_RECOVERY": "0",
    "CRAFT_ONE_FACTORY_STEP4D": "1",
    # UNKNOWN-only CP budget escalation reuses the exact prepared model.
    "CRAFT_ADAPTIVE_CP_BUDGETS": "1",
    "CRAFT_ADAPTIVE_CP_RETRY_SECONDS": "120,240",
})

from scratch.run_factory2_172_step4d import main

if __name__ == "__main__":
    raise SystemExit(main())
