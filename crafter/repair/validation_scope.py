"""Explicit completeness scopes layered over the existing validator primitives."""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class ValidationScopeV1:
    mode: str
    required_operation_keys: tuple[str, ...]
    future_outside_operation_keys: tuple[str, ...] = ()

    def __post_init__(self):
        if self.mode not in {'WINDOW', 'ASSEMBLED_FACTORY', 'FULL_GLOBAL'}:
            raise ValueError('unknown validation scope')
        if set(self.required_operation_keys) & set(self.future_outside_operation_keys):
            raise ValueError('an operation cannot be both required and future-outside')


def scope_completeness(scope: ValidationScopeV1, operation_keys):
    """Completeness accounting used before physical validation is invoked."""
    seen = [str(x) for x in operation_keys]
    required = set(scope.required_operation_keys)
    duplicates = sorted({x for x in seen if seen.count(x) > 1})
    missing = sorted(required - set(seen))
    unexpected = sorted(set(seen) - required)
    return {
        'validation_scope': scope.mode,
        'required_operations': len(required),
        'future_outside_operations': len(scope.future_outside_operation_keys),
        'missing_required_operations': missing,
        'duplicate_operations': duplicates,
        'unexpected_operations': unexpected,
        'factory_complete_claimed': scope.mode in {'ASSEMBLED_FACTORY', 'FULL_GLOBAL'},
        'complete_for_scope': not missing and not duplicates and not unexpected,
    }
