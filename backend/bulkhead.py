"""Per-category bulkheads (slice G).

Each data category gets its own concurrency limit per backend instance, sized by the
`priority` in its policy (high > normal > low). A spike in one category queues behind
its own limit only; a request that cannot get a slot within `wait_seconds` gets
503 + Retry-After instead of piling up, and the other categories keep their capacity.
"""

import asyncio
from contextlib import asynccontextmanager

from fastapi import HTTPException

import metrics

DEFAULT_LIMITS = {"high": 64, "normal": 32, "low": 8}


class Bulkheads:
    def __init__(self, limits: dict[str, int] | None = None, wait_seconds: float = 10.0) -> None:
        self._limits = {**DEFAULT_LIMITS, **(limits or {})}
        self._wait = wait_seconds
        self._sems: dict[tuple[str, str], asyncio.Semaphore] = {}

    def limit(self, priority: str) -> int:
        return self._limits.get(priority, self._limits["normal"])

    def utilisation(self, category: str, priority: str) -> float:
        """Share of this category's slots in use right now on this instance (0..1)."""
        sem = self._sems.get((category, priority))
        limit = self.limit(priority)
        return 0.0 if sem is None else max(0.0, (limit - sem._value) / limit)

    @asynccontextmanager
    async def slot(self, category: str, priority: str):
        key = (category, priority)  # a policy priority change gets a fresh-sized semaphore
        sem = self._sems.get(key)
        if sem is None:
            sem = self._sems[key] = asyncio.Semaphore(self.limit(priority))
        try:
            await asyncio.wait_for(sem.acquire(), self._wait)
        except (asyncio.TimeoutError, TimeoutError):
            metrics.bulkhead_rejected(category)
            raise HTTPException(
                503, f"Category '{category}' is at capacity; retry",
                headers={"Retry-After": "1"},
            ) from None
        try:
            yield
        finally:
            sem.release()
