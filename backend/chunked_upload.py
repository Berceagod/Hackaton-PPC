"""Chunked, resumable uploads (ADR-0010).

Every chunk is staged durably on S3 under `_staging/<upload_id>/<index>` and recorded in
`upload_chunks`, so a client can resend any chunk any number of times (same key, same
row) and resume after a dropped connection by asking which chunks the server has.
When the last missing chunk arrives, one request wins the `open -> assembling` status
change and streams the staged chunks, in order, into the storage the category's policy
names, hashing on the way. Memory stays bounded by the chunk/part size whatever the file
size is, and any backend replica can take any chunk (no local disk, no sticky sessions).
"""

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass

from fastapi import HTTPException, UploadFile
from fastapi.responses import JSONResponse

import metrics
from crypto_envelope import DecryptionError, decrypt_stream, encrypt_stream
from integrity import HashingStream, upload_chunks
from policies import CATEGORIES
from repo import FileRecord
from storage.base import StorageError

log = logging.getLogger("filegrid.upload")

MAX_CHUNK_BYTES = 16 * 1024 * 1024  # the UI sends 5 MB; anything above 16 MB is refused
MAX_TOTAL_CHUNKS = 100_000
ASSEMBLY_STALE_SECONDS = 600  # an 'assembling' upload older than this was abandoned by a crash
STAGING_STORAGE = "s3"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class ChunkTooLarge(Exception):
    pass


@dataclass
class ChunkRequest:
    upload_id: str
    chunk_index: int
    total_chunks: int
    filename: str
    category: str
    metadata: str
    sha256: str | None
    size_bytes: int | None
    chunk_sha256: str | None


def staging_key(upload_id: str, index: int) -> str:
    return f"_staging/{upload_id}/{index:06d}"


def staging_object_id(upload_id: str, index: int) -> str:
    # binds each staged chunk's ciphertext to its upload and position (ADR-0011)
    return f"staging/{upload_id}/{index}"


def _bad(error: str, message: str, status: int = 422) -> HTTPException:
    return HTTPException(status, {"error": error, "message": message})


def _hex64(value: str | None, field: str) -> str | None:
    if value is None or value == "":
        return None
    value = value.strip().lower()
    if not _HEX64.match(value):
        raise _bad("invalid_field", f"{field} must be 64 hex characters")
    return value


def _validate(req: ChunkRequest) -> tuple[str, dict]:
    try:
        upload_id = str(uuid.UUID(req.upload_id))
    except ValueError:
        raise _bad("invalid_field", "upload_id must be a UUID") from None
    if req.category not in CATEGORIES:
        raise _bad("invalid_field", f"Unknown category '{req.category}'")
    if req.category == "meter_readings":
        raise _bad("invalid_field", "meter_readings are sent to /ingest (idempotent path)")
    if not 1 <= req.total_chunks <= MAX_TOTAL_CHUNKS:
        raise _bad("invalid_field", f"total_chunks must be 1..{MAX_TOTAL_CHUNKS}")
    if not 0 <= req.chunk_index < req.total_chunks:
        raise _bad("invalid_field", "chunk_index must be 0..total_chunks-1")
    req.filename = req.filename.strip()
    if not req.filename or len(req.filename) > 255:
        raise _bad("invalid_field", "filename is required (max 255 characters)")
    if req.size_bytes is not None and req.size_bytes < 0:
        raise _bad("invalid_field", "size_bytes must be >= 0")
    req.sha256 = _hex64(req.sha256, "sha256")
    req.chunk_sha256 = _hex64(req.chunk_sha256, "chunk_sha256")
    try:
        meta = json.loads(req.metadata or "{}")
        if not isinstance(meta, dict):
            raise ValueError
    except ValueError:
        raise _bad("invalid_field", "metadata must be a JSON object") from None
    return upload_id, meta


async def _limited(source, limit: int):
    total = 0
    async for piece in source:
        total += len(piece)
        if total > limit:
            raise ChunkTooLarge()
        yield piece


async def _delete(storage, key: str) -> None:
    try:
        await storage.delete(key)
    except StorageError:
        log.warning("could not delete object", extra={"key": key})


async def _delete_staging(staging, upload_id: str, total: int) -> None:
    for i in range(total):
        await _delete(staging, staging_key(upload_id, i))


def _owned(session: dict | None, principal) -> dict:
    # someone else's upload looks exactly like a missing one
    if session is None or session["owner"] != principal.email:
        raise HTTPException(404, "Upload not found")
    return session


async def _complete_body(deps, session: dict) -> dict:
    rec = await deps.repo.get_file(session["file_id"])
    return {
        "upload_id": session["id"],
        "received_chunks": session["total_chunks"],
        "total_chunks": session["total_chunks"],
        "status": "complete",
        "file_id": session["file_id"],
        "trace_id": rec.trace_id if rec else None,
        "sha256": rec.sha256 if rec else None,
        "storage": rec.storage if rec else None,
    }


def _retry_later(message: str) -> HTTPException:
    return HTTPException(503, message, headers={"Retry-After": "2"})


async def receive_chunk(deps, principal, trace_id: str, req: ChunkRequest,
                        chunk: UploadFile, storage_for):
    upload_id, meta = _validate(req)
    policy, target = await storage_for(deps, req.category)
    staging = deps.storages[STAGING_STORAGE]

    session = _owned(await deps.repo.open_upload({
        "id": upload_id,
        "owner": principal.email,
        "category": req.category,
        "filename": req.filename,
        "total_chunks": req.total_chunks,
        "size_bytes": req.size_bytes,
        "sha256": req.sha256,
        "metadata": meta,
        "trace_id": trace_id,
        # decided once, at the first chunk: staged pieces of personal data are encrypted too
        "encrypted": policy.encryption,
    }), principal)
    if (session["category"], session["filename"], session["total_chunks"]) != (
        req.category, req.filename, req.total_chunks
    ):
        raise _bad("upload_id_conflict",
                   "upload_id already used for a different file; start a new upload", 409)
    if session["status"] == "complete":
        return await _complete_body(deps, session)  # replayed chunk after success
    if session["status"] == "failed":
        raise _bad("upload_failed", f"Upload failed ({session['reason']}); start a new one", 409)
    if session["status"] == "assembling":
        raise _retry_later("Upload is being assembled; retry")

    key = staging_key(upload_id, req.chunk_index)
    stream = HashingStream(_limited(upload_chunks(chunk), MAX_CHUNK_BYTES))
    body = (
        encrypt_stream(stream, deps.settings.master_key,
                       staging_object_id(upload_id, req.chunk_index))
        if session["encrypted"] else stream
    )
    try:
        await staging.put_stream(key, body)
        size = stream.size
        metrics.add_bytes(size)
    except ChunkTooLarge:
        await _delete(staging, key)
        raise _bad("chunk_too_large", f"Chunk larger than {MAX_CHUNK_BYTES} bytes", 413) from None
    except StorageError as exc:
        raise _retry_later(f"Staging storage unavailable: {exc}") from exc
    if req.chunk_sha256 and stream.hexdigest != req.chunk_sha256:
        metrics.integrity_failure(req.category, STAGING_STORAGE, "chunk_checksum_mismatch")
        await _delete(staging, key)
        raise _bad("chunk_checksum_mismatch", "Chunk corrupted in transit; resend it")

    received = await deps.repo.record_chunk(upload_id, req.chunk_index, size, stream.hexdigest)
    if received < req.total_chunks:
        return {"upload_id": upload_id, "received_chunks": received,
                "total_chunks": req.total_chunks, "status": "open"}
    return await _assemble(deps, principal, trace_id, session, policy, target, staging)


async def _assemble(deps, principal, trace_id, session, policy, target, staging):
    upload_id, total = session["id"], session["total_chunks"]
    started = time.perf_counter()

    def done(outcome: str, nbytes: int = 0) -> None:
        # The finished file is one "ingest" on the storage the policy chose (slice H); the
        # chunks that built it were recorded one by one as "chunk" on the staging storage.
        metrics.record(session["category"], target.name, "ingest", outcome,
                       time.perf_counter() - started, nbytes)

    if not await deps.repo.start_assembly(upload_id, ASSEMBLY_STALE_SECONDS):
        current = await deps.repo.get_upload(upload_id)
        if current and current["status"] == "complete":
            return await _complete_body(deps, current)
        raise _retry_later("Upload is being assembled; retry")

    file_id = str(uuid.uuid4())
    final_key = f"{session['category']}/{file_id}"

    master_key = deps.settings.master_key
    encrypted = bool(session.get("encrypted"))

    async def joined():
        for i in range(total):
            source = staging.get_stream(staging_key(upload_id, i))
            if encrypted:
                source = decrypt_stream(source, master_key, staging_object_id(upload_id, i))
            async for piece in source:
                yield piece

    stream = HashingStream(joined())
    body = encrypt_stream(stream, master_key, file_id) if encrypted else stream
    try:
        await target.put_stream(final_key, body)
        size = stream.size
    except DecryptionError:
        # a staged chunk was altered at rest: it will not repair itself on retry
        await _delete(target, final_key)
        await _delete_staging(staging, upload_id, total)
        await deps.repo.fail_upload(upload_id, "staging_corrupted")
        metrics.integrity_failure(session["category"], STAGING_STORAGE, "staging_corrupted")
        done("integrity_error")
        log.error("staged chunk failed authentication", extra={"upload_id": upload_id})
        return JSONResponse(status_code=409, content={"detail": {
            "error": "upload_failed", "message": "A staged chunk was altered; start a new upload"}})
    except StorageError as exc:
        # chunks stay staged: resending the last chunk retries the assembly
        await deps.repo.reopen_upload(upload_id)
        done("server_error")
        log.error("assembly failed", extra={"upload_id": upload_id, "storage": target.name,
                                            "error": str(exc)})
        raise _retry_later(f"Storage '{target.name}' unavailable") from exc

    async def fail(error: str, message: str, status: int = 422, extra: dict | None = None):
        await _delete(target, final_key)
        await _delete_staging(staging, upload_id, total)
        await deps.repo.fail_upload(upload_id, error)
        if error == "file_checksum_mismatch":  # damaged between the client and us
            metrics.integrity_failure(session["category"], target.name, error)
        done({"file_checksum_mismatch": "integrity_error", "duplicate_file": "duplicate"}
             .get(error, "client_error"), size)
        log.error("upload rejected", extra={"upload_id": upload_id, "reason": error})
        detail = {"error": error, "message": message, **(extra or {})}
        return JSONResponse(status_code=status, content={"detail": detail})

    if session["size_bytes"] is not None and size != session["size_bytes"]:
        return await fail("file_size_mismatch",
                          f"Received {size} bytes, client declared {session['size_bytes']}")
    if session["sha256"] and stream.hexdigest != session["sha256"]:
        return await fail("file_checksum_mismatch",
                          "Assembled file does not match the client's SHA-256")
    if policy.deduplicate:
        existing = await deps.repo.find_by_sha(session["category"], stream.hexdigest)
        if existing is not None:
            return await fail("duplicate_file", "Identical file already stored", 409,
                              {"file_id": existing.id, "sha256": existing.sha256,
                               "storage": existing.storage})

    meta = session["metadata"] or {}
    rec = FileRecord(
        id=file_id,
        filename=session["filename"],
        category=session["category"],
        storage=target.name,
        storage_key=final_key,
        size_bytes=size,
        sha256=stream.hexdigest,
        encrypted=encrypted,
        trace_id=trace_id,
        zone=meta.get("zone"),
        station=meta.get("station"),
        description=meta.get("description"),
        uploaded_by=principal.email,
    )
    try:
        await deps.repo.complete_upload(upload_id, rec)
    except Exception:
        await _delete(target, final_key)
        await deps.repo.reopen_upload(upload_id)
        done("server_error", size)
        raise
    done("ok", size)
    await deps.repo.add_audit(
        file_id, "upload", principal.email, trace_id,
        f"Upload pe bucăți ({total} bucăți) {session['category']} → {target.name}",
    )
    await _delete_staging(staging, upload_id, total)
    log.info("chunked upload stored", extra={"upload_id": upload_id, "file_id": file_id,
                                             "storage": target.name, "size": size})
    return await _complete_body(deps, {**session, "file_id": file_id})


async def upload_status(deps, principal, upload_id: str) -> dict:
    try:
        upload_id = str(uuid.UUID(upload_id))
    except ValueError:
        raise HTTPException(404, "Upload not found") from None
    session = _owned(await deps.repo.get_upload(upload_id), principal)
    received = await deps.repo.received_chunks(upload_id)
    have = set(received)
    body = {
        "upload_id": upload_id,
        "status": session["status"],
        "filename": session["filename"],
        "category": session["category"],
        "total_chunks": session["total_chunks"],
        "received_chunks": len(received),
        "received": received,
        "missing": [] if session["status"] == "complete"
        else [i for i in range(session["total_chunks"]) if i not in have],
    }
    if session["status"] == "complete":
        body.update({k: v for k, v in (await _complete_body(deps, session)).items()
                     if k in ("file_id", "trace_id", "sha256", "storage")})
    if session["status"] == "failed":
        body["reason"] = session["reason"]
    return body


async def purge_stale_uploads(deps, older_than_seconds: float) -> int:
    """Delete uploads abandoned for longer than `older_than_seconds` (open or failed),
    with their staged chunks. Activity (a received chunk) keeps an upload alive."""
    staging = deps.storages[STAGING_STORAGE]
    purged = 0
    for upload_id, total in await deps.repo.list_stale_uploads(older_than_seconds):
        await _delete_staging(staging, upload_id, total)
        await deps.repo.delete_upload(upload_id)
        purged += 1
    if purged:
        log.info("abandoned uploads purged", extra={"count": purged})
    return purged
