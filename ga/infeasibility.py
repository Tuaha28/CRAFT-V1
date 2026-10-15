"""Structured failures for scheduling without physical machine capacity."""


def missing_machine_pool(order_id, operation_key, capability, line_id=None):
    return {
        "code": "missing_machine_capacity",
        "production_order_id": str(order_id),
        "operation_key": str(operation_key),
        "required_capability": capability,
        "line_id": line_id,
    }


class ScheduleInfeasibleError(ValueError):
    """An operation in the planning instance has no eligible machine pool."""

    def __init__(self, result):
        self.result = result
        super().__init__(
            f"Order {result['production_order_id']}, operation "
            f"{result['operation_key']}: no eligible machines for "
            f"{result['required_capability']!r}"
        )
