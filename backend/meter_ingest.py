"""Meter-reading ingest: never lost, never duplicated.

Identity of a reading = (meter_id, timestamp). The UNIQUE constraint on
meter_readings(meter_id, reading_ts) is the single arbiter, so concurrent retries on
any number of backend replicas cannot create two rows.

  1. claim   INSERT ... ON CONFLICT DO NOTHING, status 'pending'
  2. store   write the file to the storage chosen by policy (deterministic key, so
             repeating the write is harmless)
  3. commit  files_metadata row + status 'stored' in ONE transaction -> 202

  already stored or queued  -> 409 (what the simulator counts as "duplicate rejected")
  storage write failed      -> exact bytes kept in the metadata DB as 'queued', 202
                               (meter_outbox.py writes them out later, slice G);
                               only if that also fails: claim released, 503
  duplicate while in flight -> 503 + Retry-After (never say "stored" before it is)
  claim orphaned by a crash -> taken over after STALE_CLAIM_SECONDS
"""

import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from crypto_envelope import encrypt_stream
from repo import FileRecord
from storage.base import StorageError

log = logging.getLogger("filegrid.meter")

CATEGORY = "meter_readings"
MAX_READING_BYTES = 1024 * 1024  # readings are 1-50 KB; anything bigger is not a reading
STALE_CLAIM_SECONDS = 5.0
_METER_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")  # also keeps storage keys path-safe


@dataclass(frozen=True)
class ParsedReading:
    meter_id: str
    timestamp_raw: str
    reading_ts: datetime
    zone: str | None
    voltage_v: float | None
    sim_label: str | None
    payload_json: str


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def parse_reading(raw: bytes) -> ParsedReading:
    try:
        doc = json.loads(raw)
    except ValueError:
        raise HTTPException(422, "meter reading must be a JSON document") from None
    if not isinstance(doc, dict):
        raise HTTPException(422, "meter reading must be a JSON object")
    meter_id, ts_raw = doc.get("meter_id"), doc.get("timestamp")
    if not isinstance(meter_id, str) or not _METER_ID.match(meter_id) or ".." in meter_id:
        raise HTTPException(422, "meter_id missing or invalid")
    if not isinstance(ts_raw, str):
        raise HTTPException(422, "timestamp missing")
    try:
        ts = datetime.fromisoformat(ts_raw)
    except ValueError:
        raise HTTPException(422, "timestamp must be ISO-8601") from None
    ts = (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)

    instant = doc.get("instant") if isinstance(doc.get("instant"), dict) else {}
    voltage = instant.get("voltage_v")
    zone, label = doc.get("zone"), doc.get("sim_label")
    return ParsedReading(
        meter_id=meter_id,
        timestamp_raw=ts_raw,
        reading_ts=ts,
        zone=zone if isinstance(zone, str) else None,
        voltage_v=float(voltage) if _is_number(voltage) else None,
        sim_label=label if isinstance(label, str) else None,
        payload_json=json.dumps(doc, separators=(",", ":")),
    )


async def _one_chunk(data: bytes):
    yield data


def reading_key(meter_id: str, reading_ts: datetime) -> str:
    """Deterministic storage key: a retried or drained write overwrites the same object."""
    return f"{CATEGORY}/{meter_id}/{reading_ts:%Y%m%dT%H%M%S%f}Z.json"


async def ingest_meter_reading(deps, file, idempotency_key, principal, trace_id) -> JSONResponse:
    raw = await file.read(MAX_READING_BYTES + 1)
    if len(raw) > MAX_READING_BYTES:
        raise HTTPException(413, "meter reading larger than 1 MB")
    reading = parse_reading(raw)
    expected_key = f"{reading.meter_id}:{reading.timestamp_raw}"
    if idempotency_key and idempotency_key != expected_key:
        raise HTTPException(422, "Idempotency-Key does not match meter_id:timestamp of the reading")

    policy = await deps.policies.get(CATEGORY)
    storage = deps.storages.get(policy.storage)
    if storage is None:
        raise HTTPException(501, f"Storage '{policy.storage}' not implemented")

    repo = deps.meters()  # own connection pool (bulkhead, slice G)
    state, file_id = await repo.claim_reading(
        {
            "meter_id": reading.meter_id,
            "reading_ts": reading.reading_ts,
            "zone": reading.zone,
            "voltage_v": reading.voltage_v,
            "sim_label": reading.sim_label,
            "payload": reading.payload_json,
            "file_id": str(uuid.uuid4()),
            "trace_id": trace_id,
        },
        STALE_CLAIM_SECONDS,
    )
    ident = {"meter_id": reading.meter_id, "timestamp": reading.timestamp_raw}
    if state == "duplicate":
        log.info("duplicate reading rejected", extra={"category": CATEGORY, **ident})
        return JSONResponse(
            status_code=409,
            content={"error": "duplicate_reading", "file_id": file_id, "trace_id": trace_id, **ident},
        )
    if state == "in_flight":
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": "1"},
            content={"error": "reading_in_flight", "trace_id": trace_id, **ident},
        )

    key = reading_key(reading.meter_id, reading.reading_ts)
    filename = file.filename or f"{reading.meter_id}_{reading.reading_ts:%Y%m%dT%H%M%S}.json"
    try:
        # Encrypted under a key derived from file_id: a takeover that rewrites the same
        # object for the same file_id produces another valid ciphertext of the same bytes.
        body = (
            encrypt_stream(_one_chunk(raw), deps.settings.master_key, file_id)
            if policy.encryption else _one_chunk(raw)
        )
        await storage.put_stream(key, body)
    except StorageError as exc:
        log.error(
            "meter storage write failed",
            extra={"category": CATEGORY, "storage": storage.name, "error": str(exc), **ident},
        )
        return await _queue(deps, reading, raw, file_id, filename, principal, trace_id,
                            storage.name, ident)

    record = FileRecord(
        id=file_id,
        filename=filename,
        category=CATEGORY,
        storage=storage.name,
        storage_key=key,
        size_bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        encrypted=policy.encryption,
        trace_id=trace_id,
        zone=reading.zone,
        uploaded_by=principal.email,
    )
    try:
        await repo.complete_reading(reading.meter_id, reading.reading_ts, record)
    except Exception as exc:
        log.error("meter commit failed", extra={"category": CATEGORY, "error": str(exc), **ident})
        await _release(deps, reading, file_id)
        raise HTTPException(503, "Metadata store unavailable") from exc

    log.info(
        "reading stored",
        extra={"category": CATEGORY, "storage": storage.name, "file_id": file_id, **ident},
    )
    return JSONResponse(
        status_code=202,
        content={
            "file_id": file_id,
            "trace_id": trace_id,
            "sha256": record.sha256,
            "storage": storage.name,
        },
    )


async def _queue(deps, reading, raw, file_id, filename, principal, trace_id, storage_name,
                 ident) -> JSONResponse:
    """The storage is down: keep the exact reading in the metadata DB (outbox). It is
    durably accepted, so the sender gets 202 and stops retrying; the drainer writes it
    to the storage later. If even that fails, release the claim and ask for a retry."""
    try:
        queued = await deps.meters().queue_reading(
            reading.meter_id, reading.reading_ts, raw, filename, principal.email)
    except Exception as exc:
        log.error("could not queue reading", extra={"category": CATEGORY, "error": str(exc), **ident})
        queued = False
    if not queued:
        await _release(deps, reading, file_id)
        raise HTTPException(503, f"Storage '{storage_name}' unavailable",
                            headers={"Retry-After": "2"})
    log.warning("reading queued: storage unavailable",
                extra={"category": CATEGORY, "storage": storage_name, "file_id": file_id, **ident})
    return JSONResponse(
        status_code=202,
        content={
            "file_id": file_id,
            "trace_id": trace_id,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "storage": storage_name,
            "status": "queued",
        },
    )


async def _release(deps, reading: ParsedReading, file_id: str) -> None:
    """Drop our pending claim so the sender's retry is treated as new. If this fails
    too, the claim simply goes stale and is taken over by the retry."""
    try:
        await deps.meters().release_reading(reading.meter_id, reading.reading_ts, file_id)
    except Exception:
        log.warning("could not release claim; it will go stale", exc_info=True)
