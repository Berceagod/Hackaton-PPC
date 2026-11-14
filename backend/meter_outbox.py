"""Meter-reading outbox drainer (slice G, ADR-0013).

When the storage for meter readings is down, `/ingest` keeps the reading's exact bytes in
the metadata database with status 'queued' and answers 202: the reading is durably
accepted and will never be lost. This worker writes queued readings to the storage the
policy names (encrypted if the policy says so) as soon as it is reachable again, then
records the file and marks the reading 'stored', exactly like the direct path.
Several backend replicas can drain at once (rows are taken with SKIP LOCKED).
"""

import asyncio
import hashlib
import logging

from crypto_envelope import encrypt_stream
from meter_ingest import CATEGORY, reading_key
from repo import FileRecord
from storage.base import StorageError

log = logging.getLogger("filegrid.meter.outbox")

BATCH = 100
RETRY_AFTER_SECONDS = 5.0  # a reading that failed is not retried sooner than this
IDLE_SECONDS = 2.0


async def _one(data: bytes):
    yield data


async def drain_once(deps, retry_after: float = RETRY_AFTER_SECONDS, batch: int = BATCH) -> int:
    """Write one batch of queued readings. Returns how many were stored."""
    repo = deps.meters()
    rows = await repo.claim_queued(batch, retry_after)
    stored = 0
    for row in rows:
        policy = await deps.policies.get(CATEGORY)
        storage = deps.storages.get(policy.storage)
        if storage is None:
            log.error("no storage for queued readings", extra={"storage": policy.storage})
            break
        raw, file_id = bytes(row["raw"]), row["file_id"]
        key = reading_key(row["meter_id"], row["reading_ts"])
        body = (encrypt_stream(_one(raw), deps.settings.master_key, file_id)
                if policy.encryption else _one(raw))
        try:
            await storage.put_stream(key, body)
        except StorageError as exc:
            # still down: this and the rest of the batch are retried after retry_after
            log.warning("storage still down; readings stay queued",
                        extra={"storage": storage.name, "error": str(exc)})
            break
        await repo.complete_reading(row["meter_id"], row["reading_ts"], FileRecord(
            id=file_id,
            filename=row["filename"] or f"{row['meter_id']}.json",
            category=CATEGORY,
            storage=storage.name,
            storage_key=key,
            size_bytes=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
            encrypted=policy.encryption,
            trace_id=row["trace_id"],  # the original request's trace: one story end to end
            zone=row["zone"],
            uploaded_by=row["uploaded_by"],
        ))
        stored += 1
    if stored:
        log.info("queued readings stored", extra={"count": stored})
    return stored


async def run_drainer(deps, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            n = await drain_once(deps)
        except Exception:  # the metadata DB may be down too: keep trying
            log.exception("outbox drain failed")
            n = 0
        if n == 0:
            try:
                await asyncio.wait_for(stop.wait(), IDLE_SECONDS)
            except (asyncio.TimeoutError, TimeoutError):
                pass
