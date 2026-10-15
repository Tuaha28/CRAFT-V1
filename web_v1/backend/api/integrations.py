"""Future service boundary only. No CRAFT imports are permitted in this demo project."""
from typing import Protocol

class CraftSchedulingService(Protocol):
    def request_repair(self, factory_id: int, trigger: str) -> dict: ...

class NotConfiguredCraftService:
    """Safe placeholder for a future adapter; deliberately cannot schedule."""
    def request_repair(self, factory_id: int, trigger: str) -> dict:
        raise NotImplementedError("Live CRAFT integration is not configured in the demo application.")
