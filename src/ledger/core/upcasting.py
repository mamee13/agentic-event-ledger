"""UpcasterRegistry — decorator-based, chained upcasting by version.

Usage:
    registry = UpcasterRegistry()

    @registry.register("CreditAnalysisCompleted", from_version=1, to_version=2)
    def upcast_credit_v1_v2(payload: dict, recorded_at: datetime) -> dict:
        ...

EventStore calls registry.upcast(event) transparently on every loaded event.
The registry chains upcasters automatically: v1→v2→v3 if both are registered.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Any

# Type alias for an upcaster function
UpcasterFn = Callable[[dict[str, Any], datetime], dict[str, Any]]


class UpcasterRegistry:
    """Holds all registered upcasters and applies them in version order."""

    def __init__(self) -> None:
        # { event_type: { from_version: (to_version, fn) } }
        self._registry: dict[str, dict[int, tuple[int, UpcasterFn]]] = {}

    def register(
        self, event_type: str, from_version: int, to_version: int
    ) -> Callable[[UpcasterFn], UpcasterFn]:
        """Decorator that registers an upcaster for event_type from_version → to_version."""

        def decorator(fn: UpcasterFn) -> UpcasterFn:
            self._registry.setdefault(event_type, {})[from_version] = (to_version, fn)
            return fn

        return decorator

    def upcast(
        self,
        event_type: str,
        event_version: int,
        payload: dict[str, Any],
        recorded_at: datetime,
    ) -> tuple[int, dict[str, Any]]:
        """Chains upcasters until no further upcaster exists for the current version.

        Returns (final_version, final_payload). If no upcaster is registered the
        inputs are returned unchanged.
        """
        version = event_version
        current_payload = dict(payload)

        type_registry = self._registry.get(event_type, {})
        while version in type_registry:
            next_version, fn = type_registry[version]
            current_payload = fn(current_payload, recorded_at)
            version = next_version

        return version, current_payload

    def has_upcaster(self, event_type: str, from_version: int) -> bool:
        return from_version in self._registry.get(event_type, {})
