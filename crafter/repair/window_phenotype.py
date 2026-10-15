"""Scoped planning phenotype assembly; never backfills decoder future work."""
from __future__ import annotations
from dataclasses import dataclass

from .validation_scope import scope_completeness


@dataclass(frozen=True)
class WindowPhenotypeV1:
    selected_assignments: tuple[dict, ...]
    boundary_facts: tuple[dict, ...]
    future_outside_operation_keys: tuple[str, ...]
    validation_scope: object

    @property
    def assignment_keys(self):
        return tuple(str(x['operation_key']) for x in self.selected_assignments)


def build_window_phenotype(selected_schedule, *, boundary_facts=(),
                           future_outside_operation_keys=(), validation_scope):
    """Build a bounded view from canonical materialized selections only.

    No baseline argument exists deliberately: omitted future work cannot be
    backfilled by construction.
    """
    rows = tuple(dict(row) for row in selected_schedule)
    boundary = tuple(dict(row) for row in boundary_facts)
    result = WindowPhenotypeV1(rows, boundary,
                               tuple(sorted(str(x) for x in future_outside_operation_keys)),
                               validation_scope)
    accounting = scope_completeness(validation_scope, result.assignment_keys)
    return result, accounting


def validate_window_scope_accounting(window):
    accounting = scope_completeness(window.validation_scope, window.assignment_keys)
    return {
        **accounting,
        'scope_status': 'SCOPE_VALID' if accounting['complete_for_scope'] else 'SCOPE_INVALID',
        'physical_status': 'NOT_RUN',
        'boundary_operation_count': len(window.boundary_facts),
        'historical_future_assignments_backfilled': 0,
    }


# Compatibility alias.  It intentionally returns SCOPE_VALID rather than a
# physical VALID result; callers must run the scoped replay/validator adapter.
validate_window_phenotype = validate_window_scope_accounting
