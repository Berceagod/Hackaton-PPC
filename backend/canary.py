"""Storage canary (improvements.md 2.8): the check that does not wait for a user.

Metrics only describe traffic that happens. At 3 a.m. with no uploads, a storage can be
broken and nothing shows it. Once a minute (per worker) a tiny random object is written to
EVERY storage, read back, compared byte for byte, and deleted. The result is counted in
`filegrid_canary_runs_total{storage,outcome}` (alert: StorageCanaryFailing) and its duration
in `filegrid_canary_duration_seconds`.

It goes through the same adapters, circuit breakers and (when enabled) chaos wrapper as real
traffic, but touches no user data: keys live under `_canary/`, are never in the database, and
are removed again. A leftover from a crash is harmless and overwritten by nothing.
"""

import asyncio
import logging
import os
import time
import uuid

import metrics
from storage.base import StorageError

log = logging.getLogger("filegrid.canary")
PAYLOAD_BYTES = 2048


async def _one_chunk(data: bytes):
    yield data


async def check_storage(name: str, storage) -> bool:
    """One round trip on one storage. Never raises."""
    key = f"_canary/{uuid.uuid4()}"
    payload = os.urandom(PAYLOAD_BYTES)
    started = time.monotonic()
    try:
        await storage.put_stream(key, _one_chunk(payload))
        got = b"".join([c async for c in storage.get_stream(key)])
        if got != payload:
            raise StorageError("canary read back different bytes")
    except Exception as exc:  # any failure of the round trip is the answer, not a crash
        metrics.canary(name, "failed")
        log.warning("storage canary failed", extra={"storage": name, "error": str(exc)})
        try:
            await storage.delete(key)
        except Exception:
            pass
        return False
    metrics.canary(name, "ok", time.monotonic() - started)
    try:
        await storage.delete(key)
    except Exception:  # the check passed; a leftover 2 KB object is not an incident
        log.warning("canary object not removed", extra={"storage": name, "key": key})
    return True


async def canary_once(deps) -> dict[str, bool]:
    names = list(deps.storages)
    results = await asyncio.gather(*(check_storage(n, deps.storages[n]) for n in names))
    return dict(zip(names, results))


async def run_canary(deps, stop: asyncio.Event) -> None:
    s = deps.settings
    if not s.canary_enabled:
        log.info("canary disabled")
        return
    while not stop.is_set():
        try:
            await canary_once(deps)
        except Exception:
            log.exception("canary pass failed")
        try:
            await asyncio.wait_for(stop.wait(), max(s.canary_interval_seconds, 1.0))
        except (asyncio.TimeoutError, TimeoutError):
            pass
