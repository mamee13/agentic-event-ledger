"""In-process rate limiter for MCP tools.

run_integrity_check is limited to 1 call per minute per entity.
"""

import time
from collections import defaultdict


class RateLimiter:
    """Sliding-window rate limiter keyed by an arbitrary string."""

    def __init__(self, calls: int = 1, period_seconds: float = 60.0) -> None:
        self._calls = calls
        self._period = period_seconds
        self._history: dict[str, list[float]] = defaultdict(list)

    def is_allowed(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self._period
        self._history[key] = [t for t in self._history[key] if t > cutoff]
        if len(self._history[key]) < self._calls:
            self._history[key].append(now)
            return True
        return False

    def seconds_until_allowed(self, key: str) -> float:
        if not self._history[key]:
            return 0.0
        return max(0.0, self._period - (time.monotonic() - min(self._history[key])))


# 1 integrity check per entity per 60 s
integrity_check_limiter = RateLimiter(calls=1, period_seconds=60.0)
