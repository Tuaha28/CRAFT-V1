"""Frozen controlled-sensitivity configurations for Phase-B operating regimes.

Stress reproduces the pre-existing Phase-B semantics exactly. Nominal and Busy
are monotone synthetic pressure reductions, frozen before their outcomes exist.
They are not estimates of observed factory rates.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json


REGIME_CONFIG_VERSION = "operating-pressure-v1"


@dataclass(frozen=True)
class OperatingRegime:
    name: str
    execution_variance_mean: float
    execution_variance_sd: float
    execution_variance_min: float
    execution_variance_max: float
    breakdown_effect_cap: float
    breakdown_effect_per_count: float
    queue_sd_widen_cap: float
    queue_sd_widen_scale_mins: float
    dynamic_breakdown_runtime_threshold_mins: float
    dynamic_breakdown_probability: float
    repair_min_minutes: int
    repair_max_minutes: int
    calibration_statement: str

    def manifest(self) -> dict:
        payload = {"version": REGIME_CONFIG_VERSION, **asdict(self)}
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return {**payload, "sha256": sha256(canonical.encode("utf-8")).hexdigest()}


# Exact legacy STRESS values. Do not alter without superseding the frozen Stress
# dataset; only new staging databases may use the other profiles.
STRESS = OperatingRegime(
    name="stress", execution_variance_mean=1.0, execution_variance_sd=0.15,
    execution_variance_min=0.65, execution_variance_max=1.60,
    breakdown_effect_cap=0.08, breakdown_effect_per_count=0.01,
    queue_sd_widen_cap=0.50, queue_sd_widen_scale_mins=10000.0,
    dynamic_breakdown_runtime_threshold_mins=1200.0,
    dynamic_breakdown_probability=0.15, repair_min_minutes=120,
    repair_max_minutes=360,
    calibration_statement="Existing frozen synthetic STRESS semantics; not an empirical factory outcome rate.",
)

# Controlled sensitivity profiles. Planning inputs, order universe, resources,
# seeds, calendar and label definition are held fixed; only Phase-B severity
# below changes. Multipliers are monotone relative to STRESS and intentionally
# were frozen before either dataset's labels were calculated.
NOMINAL = OperatingRegime(
    name="nominal", execution_variance_mean=1.0, execution_variance_sd=0.08,
    execution_variance_min=0.80, execution_variance_max=1.30,
    breakdown_effect_cap=0.03, breakdown_effect_per_count=0.005,
    queue_sd_widen_cap=0.20, queue_sd_widen_scale_mins=10000.0,
    dynamic_breakdown_runtime_threshold_mins=1200.0,
    dynamic_breakdown_probability=0.05, repair_min_minutes=60,
    repair_max_minutes=180,
    calibration_statement="Controlled synthetic lower-pressure sensitivity regime; not calibrated to observed factory disruption rates.",
)
BUSY = OperatingRegime(
    name="busy", execution_variance_mean=1.0, execution_variance_sd=0.12,
    execution_variance_min=0.70, execution_variance_max=1.45,
    breakdown_effect_cap=0.06, breakdown_effect_per_count=0.0075,
    queue_sd_widen_cap=0.35, queue_sd_widen_scale_mins=10000.0,
    dynamic_breakdown_runtime_threshold_mins=1200.0,
    dynamic_breakdown_probability=0.10, repair_min_minutes=90,
    repair_max_minutes=270,
    calibration_statement="Controlled synthetic intermediate-pressure sensitivity regime; not calibrated to observed factory disruption rates.",
)

REGIMES = {profile.name: profile for profile in (NOMINAL, BUSY, STRESS)}


def get_operating_regime(name: str | OperatingRegime = "stress") -> OperatingRegime:
    if isinstance(name, OperatingRegime):
        return name
    try:
        return REGIMES[str(name).strip().lower()]
    except KeyError as exc:
        raise ValueError(f"Unknown operating regime: {name!r}; expected one of {sorted(REGIMES)}") from exc


def assert_monotonic_pressure() -> None:
    ordered = (NOMINAL, BUSY, STRESS)
    for field in (
        "execution_variance_sd", "breakdown_effect_cap",
        "breakdown_effect_per_count", "queue_sd_widen_cap",
        "dynamic_breakdown_probability", "repair_min_minutes",
        "repair_max_minutes",
    ):
        values = [getattr(profile, field) for profile in ordered]
        if values != sorted(values) or len(set(values)) != len(values):
            raise AssertionError(f"Regime pressure is not strictly monotone for {field}: {values}")


assert_monotonic_pressure()
