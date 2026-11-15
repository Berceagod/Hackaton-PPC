"""Fault injection below the circuit breaker (improvements.md 2.1, only when CHAOS_ENABLED).

An administrator can make one storage "down" (every call fails) or "slow" (every call waits)
for a limited time. The wrapper sits BETWEEN the real adapter and the breaker, so the rest of
the system reacts exactly as it would to a real outage: the breaker opens, meter readings go
to the outbox, replicas are served, metrics move and alerts fire.

It is an application-level simulation: the storage container keeps running. The scripts
`kill-storage.sh` (stops the real container) remain the proof of a true outage.
"""

import asyncio
import time
from typing import AsyncIterator

from storage.base import StorageError

REFRESH_SECONDS = 1.0  # a fault set by any replica reaches this one within a second


class ChaosStorage:
    def __init__(self, inner, repo) -> None:
        self._inner = inner
        self._repo = repo
        self.name = inner.name
        self._fault: dict | None = None
        self._read_at = float("-inf")

    async def _current(self) -> dict | None:
        now = time.monotonic()
        if now - self._read_at >= REFRESH_SECONDS:
            self._read_at = now
            try:
                faults = await self._repo.active_faults()
            except Exception:  # cannot read the fault table: behave normally
                faults = []
            self._fault = next((f for f in faults if f["target"] == self.name), None)
        return self._fault

    async def _apply(self) -> None:
        fault = await self._current()
        if fault is None:
            return
        if fault["kind"] == "down":
            raise StorageError(f"chaos: storage '{self.name}' is down (injected)")
        if fault["kind"] == "slow":
            await asyncio.sleep(fault["seconds"])

    async def put_stream(self, key: str, chunks: AsyncIterator[bytes]) -> int:
        await self._apply()
        return await self._inner.put_stream(key, chunks)

    async def get_stream(self, key: str) -> AsyncIterator[bytes]:
        await self._apply()
        async for chunk in self._inner.get_stream(key):
            yield chunk

    async def delete(self, key: str) -> None:
        await self._apply()
        await self._inner.delete(key)

    async def healthy(self) -> bool:
        fault = await self._current()
        if fault is not None and fault["kind"] == "down":
            return False
        return await self._inner.healthy()

    def __getattr__(self, item):  # ensure_ready, close, ... of the real adapter
        return getattr(self._inner, item)
