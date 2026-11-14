"""Circuit breaker around a storage adapter (slice G).

A storage that keeps failing is "opened" for `cooldown` seconds: calls fail at once with
StorageError instead of each waiting for a connect/read timeout. That keeps request
slots and connections free for everything else while the storage is down. After the
cooldown one trial call goes through ("half-open"); success closes the breaker.
A missing object (ObjectNotFound) is an answer from a healthy storage, not a failure.
"""

import logging
import time
from typing import AsyncIterator

from storage.base import ObjectNotFound, StorageError

log = logging.getLogger("filegrid.storage.breaker")


class BreakerStorage:
    def __init__(self, inner, threshold: int = 3, cooldown: float = 5.0) -> None:
        self._inner = inner
        self.name = inner.name
        self._threshold = threshold
        self._cooldown = cooldown
        self._failures = 0
        self._opened_at: float | None = None
        self._trial_in_flight = False

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if time.monotonic() - self._opened_at < self._cooldown:
            return "open"
        return "half_open"

    def _enter(self) -> None:
        state = self.state
        if state == "open" or (state == "half_open" and self._trial_in_flight):
            raise StorageError(f"storage '{self.name}' unavailable (circuit open)")
        if state == "half_open":
            self._trial_in_flight = True

    def _success(self) -> None:
        if self._opened_at is not None:
            log.info("circuit closed", extra={"storage": self.name})
        self._failures, self._opened_at, self._trial_in_flight = 0, None, False

    def _failure(self) -> None:
        self._failures += 1
        was_trial = self._trial_in_flight
        self._trial_in_flight = False
        if was_trial or self._failures >= self._threshold:
            if self._opened_at is None or was_trial:
                log.warning("circuit open", extra={"storage": self.name,
                                                   "failures": self._failures})
            self._opened_at = time.monotonic()

    def _done(self, ok: bool) -> None:
        self._success() if ok else self._failure()

    async def put_stream(self, key: str, chunks: AsyncIterator[bytes]) -> int:
        self._enter()
        try:
            size = await self._inner.put_stream(key, chunks)
        except ObjectNotFound:
            self._success()
            raise
        except StorageError:
            self._failure()
            raise
        except BaseException:
            self._trial_in_flight = False  # not the storage's fault (e.g. client sent too much)
            raise
        self._success()
        return size

    async def get_stream(self, key: str) -> AsyncIterator[bytes]:
        self._enter()
        try:
            async for piece in self._inner.get_stream(key):
                yield piece
        except ObjectNotFound:
            self._success()
            raise
        except StorageError:
            self._failure()
            raise
        except BaseException:
            self._trial_in_flight = False
            raise
        self._success()

    async def delete(self, key: str) -> None:
        self._enter()
        try:
            await self._inner.delete(key)
        except StorageError:
            self._failure()
            raise
        except BaseException:
            self._trial_in_flight = False
            raise
        self._success()

    async def healthy(self) -> bool:
        return await self._inner.healthy()  # the truth, not the breaker's opinion
