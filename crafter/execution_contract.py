"""Versioned physical execution requirements for one operation.

Contracts keep plant evidence and controlled TEST assumptions explicit.  The
duration rule intentionally requires a declared crew productivity and machine
parallelism factor; it never infers linear speed-up from machine count.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_CEILING
from typing import Mapping


CONTRACT_VERSION = "CRAFT_OPERATION_EXECUTION_V1"
AUTHORITATIVE = "AUTHORITATIVE"
TEST_ASSUMPTION = "TEST_ASSUMPTION"
KNOWN_EVIDENCE_CLASSES = {AUTHORITATIVE, TEST_ASSUMPTION}
PARALLEL_SPLIT = "PARALLEL_SPLIT_SUM_OF_MEMBER_RATES"
CREW_EFFICIENCY_POLICIES = {PARALLEL_SPLIT}


@dataclass(frozen=True)
class OperationExecutionContract:
    operation_key: str
    factory_id: int
    required_worker_count: int
    required_machine_count: int
    required_skills: tuple[str, ...]
    eligible_worker_ids: tuple[str, ...]
    eligible_machine_ids: tuple[int, ...]
    eligible_line_ids: tuple[int, ...]
    quantity: int
    target_quantity: int
    completed_quantity: int
    in_flight_quantity: int
    quantity_basis: str
    smv_minutes_per_unit: str
    setup_seconds: int
    evidence_class: str
    source_fingerprint: str
    machine_speedup_factor: str
    pairing_policy: str
    worker_calendar_policy: str
    machine_calendar_policy: str
    snapshot_fingerprint: str = ""
    scenario_fingerprint: str = ""
    worker_efficiency_percent: tuple[tuple[str, str], ...] = ()
    crew_efficiency_percent: tuple[tuple[str, str], ...] = ()
    allowed_worker_machine_pairs: tuple[tuple[str, int], ...] = ()
    contract_version: str = CONTRACT_VERSION
    # Adaptive crew sizing (opt-in). Empty keeps the single declared crew
    # size. Otherwise the scheduler may choose any listed size k: k workers,
    # each on their own allowed machine, split the quantity in parallel. The
    # duration rule for k > 1 must be declared by crew_efficiency_policy.
    allowed_crew_sizes: tuple[int, ...] = ()
    crew_efficiency_policy: str = ""

    def __post_init__(self):
        if self.contract_version != CONTRACT_VERSION:
            raise ValueError("UNSUPPORTED_EXECUTION_CONTRACT_VERSION")
        if self.evidence_class not in KNOWN_EVIDENCE_CLASSES:
            raise ValueError("INVALID_EXECUTION_CONTRACT_EVIDENCE_CLASS")
        if not self.operation_key or int(self.factory_id) < 0:
            raise ValueError("INVALID_EXECUTION_CONTRACT_IDENTITY")
        if int(self.required_worker_count) < 1 or int(self.required_machine_count) < 1:
            raise ValueError("INVALID_EXECUTION_CONTRACT_RESOURCE_COUNT")
        if len(self.required_skills) != int(self.required_worker_count):
            raise ValueError("REQUIRED_SKILLS_MUST_MATCH_CREW_SLOTS")
        if (int(self.quantity) <= 0 or int(self.target_quantity) <= 0
                or int(self.completed_quantity) < 0 or int(self.in_flight_quantity) < 0
                or int(self.completed_quantity) + int(self.in_flight_quantity) + int(self.quantity)
                   != int(self.target_quantity)
                or Decimal(str(self.smv_minutes_per_unit)) <= 0
                or self.quantity_basis not in {"ORDER_QUANTITY", "UNSTARTED_REMAINING_UNITS",
                                               "UNSTARTED_RESIDUAL_UNITS"}):
            raise ValueError("INVALID_EXECUTION_CONTRACT_WORK")
        if int(self.setup_seconds) < 0 or Decimal(str(self.machine_speedup_factor)) <= 0:
            raise ValueError("INVALID_EXECUTION_CONTRACT_DURATION_POLICY")
        if not self.source_fingerprint:
            raise ValueError("MISSING_EXECUTION_CONTRACT_PROVENANCE")
        if self.pairing_policy not in {"ELIGIBLE_CARTESIAN_BUNDLE", "EXPLICIT_PAIRS"}:
            raise ValueError("UNSUPPORTED_RESOURCE_PAIRING_POLICY")
        if self.pairing_policy == "EXPLICIT_PAIRS" and not self.allowed_worker_machine_pairs:
            raise ValueError("EXPLICIT_RESOURCE_PAIRS_REQUIRED")
        if self.worker_calendar_policy == "UNSPECIFIED" or self.machine_calendar_policy == "UNSPECIFIED":
            raise ValueError("EXECUTION_CONTRACT_CALENDAR_POLICY_REQUIRED")
        if self.allowed_crew_sizes:
            sizes = tuple(int(x) for x in self.allowed_crew_sizes)
            if (sizes != tuple(sorted(set(sizes))) or sizes[0] != int(self.required_worker_count)
                    or int(self.required_worker_count) != int(self.required_machine_count)
                    or self.pairing_policy != "EXPLICIT_PAIRS"
                    or (len(sizes) > 1 and self.crew_efficiency_policy not in CREW_EFFICIENCY_POLICIES)):
                raise ValueError("INVALID_ADAPTIVE_CREW_POLICY")

    @property
    def crew_sizes(self) -> tuple[int, ...]:
        return tuple(int(x) for x in self.allowed_crew_sizes) or (int(self.required_worker_count),)

    def skills_for(self, crew_size: int) -> tuple[str, ...]:
        """Required skill slots for a chosen crew size (same skill per slot)."""
        if int(crew_size) == int(self.required_worker_count):
            return tuple(self.required_skills)
        return tuple(self.required_skills[0] for _ in range(int(crew_size)))

    @property
    def worker_efficiencies(self) -> dict[str, Decimal]:
        return {str(worker): Decimal(str(value))
                for worker, value in self.worker_efficiency_percent}

    @property
    def crew_efficiencies(self) -> dict[tuple[str, ...], Decimal]:
        result = {}
        for signature, value in self.crew_efficiency_percent:
            workers = tuple(sorted(str(x) for x in str(signature).split("|")))
            result[workers] = Decimal(str(value))
        return result

    def expected_productive_seconds(self, worker_ids, machine_ids) -> int:
        workers = tuple(sorted(str(x) for x in worker_ids))
        machines = tuple(sorted(int(x) for x in machine_ids))
        if self.allowed_crew_sizes:
            if len(workers) not in self.crew_sizes:
                raise ValueError("REQUIRED_WORKER_COUNT_MISMATCH")
            if len(machines) != len(workers):
                raise ValueError("REQUIRED_MACHINE_COUNT_MISMATCH")
        else:
            if len(workers) != self.required_worker_count:
                raise ValueError("REQUIRED_WORKER_COUNT_MISMATCH")
            if len(machines) != self.required_machine_count:
                raise ValueError("REQUIRED_MACHINE_COUNT_MISMATCH")
        if not set(workers).issubset(set(self.eligible_worker_ids)):
            raise ValueError("INELIGIBLE_WORKER")
        if not set(machines).issubset(set(self.eligible_machine_ids)):
            raise ValueError("INELIGIBLE_MACHINE")
        if self.pairing_policy == "EXPLICIT_PAIRS":
            if len(workers) != len(machines):
                raise ValueError("RESOURCE_PAIR_NOT_ALLOWED")
            allowed = {(str(worker), int(machine))
                       for worker, machine in self.allowed_worker_machine_pairs}
            matched = {}
            def assign(worker, seen):
                for candidate_worker, machine in sorted(allowed):
                    if candidate_worker != worker or machine in seen or machine not in machines:
                        continue
                    seen.add(machine)
                    previous = matched.get(machine)
                    if previous is None or assign(previous, seen):
                        matched[machine] = worker
                        return True
                return False
            if not all(assign(worker, set()) for worker in workers):
                raise ValueError("RESOURCE_PAIR_NOT_ALLOWED")
        efficiencies = self.worker_efficiencies
        if len(workers) == 1:
            efficiency = efficiencies.get(workers[0])
        elif self.allowed_crew_sizes and self.crew_efficiency_policy == PARALLEL_SPLIT:
            # Each member sews a share on their own machine, balanced by
            # efficiency, so the crew's rate is the sum of member rates.
            members = [efficiencies.get(worker) for worker in workers]
            efficiency = None if any(v is None for v in members) else sum(members)
        else:
            efficiency = self.crew_efficiencies.get(workers)
        if efficiency is None or efficiency <= 0:
            raise ValueError("MISSING_AUTHORIZED_CREW_EFFICIENCY_RULE")
        smv = Decimal(str(self.smv_minutes_per_unit))
        speedup = Decimal(str(self.machine_speedup_factor))
        work_seconds = Decimal(int(self.quantity)) * smv * Decimal(60) / (efficiency / Decimal(100)) / speedup
        return int(work_seconds.to_integral_value(rounding=ROUND_CEILING))

    def as_mapping(self) -> dict:
        return asdict(self)

    @classmethod
    def from_mapping(cls, value: Mapping):
        """Parse serialized contract data without accepting implicit defaults."""
        fields = dict(value)
        for name in ("required_skills", "eligible_worker_ids", "eligible_machine_ids",
                     "eligible_line_ids", "worker_efficiency_percent",
                     "crew_efficiency_percent", "allowed_worker_machine_pairs",
                     "allowed_crew_sizes"):
            if name in fields:
                fields[name] = tuple(tuple(x) if isinstance(x, (list, tuple)) else x
                                     for x in fields[name])
        return cls(**fields)
