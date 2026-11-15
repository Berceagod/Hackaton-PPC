"""Ingest, list, download (integrity-checked) and verify."""

import json
import logging
import unicodedata
import uuid
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Depends,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import JSONResponse, StreamingResponse

from auth import Principal, require_roles
from crypto_envelope import DecryptionError, decrypt_stream, encrypt_stream
from integrity import HashingStream, sha256_of_stream, upload_chunks
import metrics
from logging_setup import trace_id_var
from meter_ingest import ingest_meter_reading
import failover
import keystore
import replication
from keystore import FileErased
from policies import CATEGORIES, STORAGES
from repo import FileRecord
from storage.base import ObjectNotFound, StorageError

log = logging.getLogger("filegrid.files")
router = APIRouter()

INGEST_ROLES = require_roles("dispecer", "admin", "service")
USERS = require_roles("dispecer", "admin")  # people; the simulator token cannot read
# An auditor sees WHO did WHAT (file list, audit trails) but can never read a file's content
READERS = require_roles("dispecer", "admin", "auditor")

# The UI knows two audit actions. Every finer-grained event maps onto one of them
# (the exact event is still returned in `event`).
_UI_ACTION = {"upload": "upload"}


def _safe_filename(name: str) -> str:
    """Keep a client-supplied filename from breaking out of the Content-Disposition header."""
    cleaned = "".join(c for c in name if c.isprintable() and c not in '"\\/')
    return cleaned[:200] or "file"


def _content_disposition(name: str) -> str:
    """RFC 6266: an ASCII `filename` for old clients plus the exact UTF-8 name in
    `filename*`. HTTP headers are latin-1, so a raw "ș" in the header would crash the
    response (500) for every file with a Romanian name."""
    safe = _safe_filename(name)
    ascii_name = (
        unicodedata.normalize("NFKD", safe).encode("ascii", "ignore").decode() or "file"
    )
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(safe, safe='')}"


async def _storage_for(deps, category: str):
    policy = await deps.policies.get(category)
    storage = deps.storages.get(policy.storage)
    if storage is None:
        raise HTTPException(
            501, f"Storage '{policy.storage}' not implemented yet for {category}"
        )
    return policy, storage


@router.post("/ingest", status_code=202)
async def ingest(
    request: Request,
    file: UploadFile,
    category: str = Form(...),
    metadata: str = Form("{}"),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    principal: Principal = Depends(INGEST_ROLES),
) -> JSONResponse:
    # slice H: one observation per request (requests_total, duration, bytes)
    async with metrics.observe(category, "ingest") as obs:
        resp = await _ingest_guarded(request, file, category, metadata, idempotency_key, principal)
        obs.from_response(resp.status_code)
        return resp


async def _ingest_guarded(request, file, category, metadata, idempotency_key, principal) -> JSONResponse:
    deps = request.app.state.deps
    if deps.bulkheads is None or category not in CATEGORIES:
        return await _ingest(request, file, category, metadata, idempotency_key, principal)
    # bulkhead: this category's own concurrency limit, sized by its policy priority
    priority = (await deps.policies.get(category)).priority
    async with deps.bulkheads.slot(category, priority):
        return await _ingest(request, file, category, metadata, idempotency_key, principal)


async def _ingest(request, file, category, metadata, idempotency_key, principal) -> JSONResponse:
    deps = request.app.state.deps
    trace_id = trace_id_var.get()
    if category not in CATEGORIES:
        raise HTTPException(422, f"Unknown category '{category}'")
    if principal.role == "service" and category != "meter_readings":
        raise HTTPException(403, "The service token may only ingest meter_readings")
    try:
        meta = json.loads(metadata or "{}")
        if not isinstance(meta, dict):
            raise ValueError
    except ValueError:
        raise HTTPException(422, "metadata must be a JSON object") from None

    if category == "meter_readings":
        # exactly-once path keyed on (meter_id, timestamp)
        return await ingest_meter_reading(deps, file, idempotency_key, principal, trace_id)

    policy, storage = await _storage_for(deps, category)
    # automatic failover: primary down, or its category overloaded -> the fallback storage
    chosen, why = failover.choose(deps, category, policy)
    if why:
        storage = deps.storages[chosen]
        metrics.failover_file(category, why)
    await failover.note(deps, category, policy, chosen, why)
    metrics.note(storage=storage.name)
    file_id = str(uuid.uuid4())
    key = f"{category}/{file_id}"

    # Single streaming pass: hash the plaintext while uploading (encrypting on the way
    # when the policy says so). Nothing is acknowledged until the storage confirmed the
    # durable write.
    stream = HashingStream(upload_chunks(file))
    if policy.encryption:
        # a random key per file, stored only wrapped by the master key: the file can later be
        # made unreadable for good by destroying that key (POST /files/{id}/erase)
        body = encrypt_stream(stream, await keystore.new_file_key(deps, file_id), file_id)
    else:
        body = stream
    try:
        await storage.put_stream(key, body)
        size = stream.size  # plaintext size, what the user uploaded and will download
        metrics.add_bytes(size)
    except StorageError as exc:
        log.error(
            "storage write failed",
            extra={"category": category, "storage": storage.name, "error": str(exc)},
        )
        raise HTTPException(503, f"Storage '{storage.name}' unavailable") from exc

    if policy.deduplicate:
        # Content dedup (ADR-0004): same bytes already stored in this category -> 409
        # pointing at the existing file. Best effort (no unique constraint): two
        # identical uploads racing each other can both be kept.
        existing = await deps.repo.find_by_sha(category, stream.hexdigest)
        if existing is not None:
            await _cleanup(storage, key)
            return JSONResponse(
                status_code=409,
                content={
                    "error": "duplicate_file",
                    "file_id": existing.id,
                    "trace_id": trace_id,
                    "sha256": existing.sha256,
                    "storage": existing.storage,
                },
            )

    record = FileRecord(
        id=file_id,
        filename=file.filename or file_id,
        category=category,
        storage=storage.name,
        storage_key=key,
        size_bytes=size,
        sha256=stream.hexdigest,
        encrypted=policy.encryption,
        trace_id=trace_id,
        zone=meta.get("zone"),
        station=meta.get("station"),
        description=meta.get("description"),
        uploaded_by=principal.email,
    )
    try:
        await deps.repo.insert_file(record)
        await deps.repo.add_audit(
            file_id, "upload", principal.email, trace_id, f"Ingest {category} → {storage.name}"
        )
    except Exception:
        await _cleanup(storage, key)
        raise

    if policy.replicas:  # second copy on another storage; never fails the (already stored) upload
        try:
            await replication.replicate(deps, record, policy, principal.email)
        except Exception:
            log.exception("replication step failed", extra={"file_id": file_id})

    log.info(
        "file stored",
        extra={"category": category, "storage": storage.name, "file_id": file_id},
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


async def _cleanup(storage, key: str) -> None:
    try:
        await storage.delete(key)
    except StorageError:
        log.warning("orphan object left in storage", extra={"key": key})


@router.get("/files")
async def list_files(
    request: Request,
    category: str | None = Query(None),
    storage: str | None = Query(None),
    q: str | None = Query(None, max_length=200),
    limit: int = Query(500, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    _: Principal = Depends(READERS),
) -> list[dict]:
    """Newest first. With no `category` and no `q`, machine-generated meter_readings are
    left out (they arrive continuously and would bury the manually uploaded files);
    ask for `category=meter_readings`, or search with `q`, to see them."""
    if category is not None and category not in CATEGORIES:
        raise HTTPException(422, f"Unknown category '{category}'")
    if storage is not None and storage not in STORAGES:
        raise HTTPException(422, f"Unknown storage '{storage}'")
    q = (q or "").strip() or None
    exclude = None if (category or q) else "meter_readings"
    rows = await request.app.state.deps.repo.list_files(
        category, storage, q, limit, offset, exclude
    )
    return [r.to_api() for r in rows]


async def _load_or_404(deps, file_id: str) -> FileRecord:
    rec = await deps.repo.get_file(file_id)
    if rec is None:
        raise HTTPException(404, "File not found")
    return rec


async def plaintext_stream(deps, rec: FileRecord):
    """The file's original bytes: decrypted on the fly when it is stored encrypted.
    Raises FileErased once the file's key was destroyed."""
    async for piece in replication.plaintext_of(deps, rec, rec.storage, rec.storage_key):
        yield piece


async def _verify(deps, rec: FileRecord) -> tuple[bool, str, str]:
    """Re-hash the stored bytes (decrypted and authenticated first when encrypted).
    Returns (ok, actual_sha256, reason)."""
    storage = deps.storages.get(rec.storage)
    if storage is None:
        raise HTTPException(503, f"Storage '{rec.storage}' not available")
    try:
        actual, _ = await sha256_of_stream(plaintext_stream(deps, rec))
    except FileErased:
        return False, "", "erased"
    except ObjectNotFound:
        return False, "", "object_missing"
    except DecryptionError:
        return False, "", "decryption_failed"  # ciphertext altered, truncated or moved
    except StorageError as exc:
        raise HTTPException(503, f"Storage '{rec.storage}' unavailable") from exc
    return actual == rec.sha256, actual, "" if actual == rec.sha256 else "checksum_mismatch"


@router.get("/files/{file_id}/verify")
async def verify(
    file_id: str, request: Request, principal: Principal = Depends(USERS)
) -> dict:
    deps = request.app.state.deps
    rec = await _load_or_404(deps, file_id)
    async with metrics.observe(rec.category, "verify") as obs:
        obs.note(storage=rec.storage)
        ok, actual, reason = await _verify(deps, rec)
        if reason == "erased":
            raise HTTPException(410, "File erased: its encryption key was destroyed")
        if ok:
            obs.add_bytes(rec.size_bytes)
        else:
            metrics.integrity_failure(rec.category, rec.storage, reason)
            obs.note(outcome="integrity_error")
        await deps.repo.add_audit(
            file_id,
            "verify",
            principal.email,
            trace_id_var.get(),
            "Verificare integritate: OK (SHA-256)"
            if ok
            else f"Verificare integritate: EȘUATĂ ({reason})",
        )
        return {
            "file_id": file_id,
            "verified": ok,
            "expected_sha256": rec.sha256,
            "actual_sha256": actual,
            "reason": reason or None,
        }


@router.get("/files/{file_id}/download")
async def download(
    file_id: str, request: Request, principal: Principal = Depends(USERS)
):
    deps = request.app.state.deps
    rec = await _load_or_404(deps, file_id)
    trace_id = trace_id_var.get()

    async with metrics.observe(rec.category, "download") as obs:
        obs.note(storage=rec.storage)

        # Verify BEFORE delivering any byte: a corrupt file is never served.
        unavailable = None
        try:
            ok, actual, reason = await _verify(deps, rec)
        except HTTPException as exc:  # the primary's storage is down
            ok, actual, reason, unavailable = False, "", "storage_unavailable", exc
        if reason == "erased":
            raise HTTPException(410, "File erased: its encryption key was destroyed")
        extra_headers: dict[str, str] = {}
        source_storage, source_key = rec.storage, rec.storage_key
        if not ok:
            # Self-healing (improvements.md 2.2): a verified second copy repairs the primary
            # (or, if the primary cannot be written, is served as it is).
            healed = await replication.heal(deps, rec, principal.email)
            if healed == "repaired":
                ok = True
                extra_headers["X-Repaired"] = "true"
            elif healed == "replica":
                rep = await deps.repo.get_replica(rec.id)
                ok = True
                source_storage, source_key = rep["storage"], rep["storage_key"]
                extra_headers["X-Served-From"] = source_storage
        if not ok and unavailable is not None:
            raise unavailable
        if not ok:
            log.error(
                "integrity check failed",
                extra={
                    "category": rec.category,
                    "storage": rec.storage,
                    "file_id": file_id,
                    "reason": reason,
                },
            )
            metrics.integrity_failure(rec.category, rec.storage, reason)
            obs.note(outcome="integrity_error")
            await deps.repo.add_audit(
                file_id,
                "download_blocked",
                principal.email,
                trace_id,
                f"Descărcare BLOCATĂ: integritate eșuată ({reason})",
            )
            return JSONResponse(
                status_code=409,
                headers={"X-Integrity-Verified": "false", "X-File-Sha256": rec.sha256},
                content={
                    "error": "integrity_check_failed",
                    "reason": reason,
                    "file_id": file_id,
                    "expected_sha256": rec.sha256,
                    "actual_sha256": actual,
                },
            )

        detail = "Integritate verificată (SHA-256)"
        if "X-Repaired" in extra_headers:
            detail += "; copia principală a fost reparată din copia secundară"
        elif "X-Served-From" in extra_headers:
            detail += f"; servit din copia secundară ({source_storage})"
        await deps.repo.add_audit(file_id, "download", principal.email, trace_id, detail)
        obs.defer()  # a download ends when its last byte is sent, not when we return
        return StreamingResponse(
            metrics.counted(obs, replication.plaintext_of(deps, rec, source_storage, source_key)),
            media_type="application/octet-stream",
            headers={
                "X-Integrity-Verified": "true",
                "X-File-Sha256": rec.sha256,
                "Content-Length": str(rec.size_bytes),
                "Content-Disposition": _content_disposition(rec.filename),
                **extra_headers,
            },
        )


@router.get("/files/{file_id}/audit")
async def audit(
    file_id: str, request: Request, _: Principal = Depends(READERS)
) -> list[dict]:
    """Who did what with this file, newest first. `action` is upload|download (what the
    UI renders); `event` is the exact event: upload, download, verify, download_blocked."""
    deps = request.app.state.deps
    await _load_or_404(deps, file_id)
    entries = await deps.repo.list_audit(file_id)
    return [
        {**e, "event": e["action"], "action": _UI_ACTION.get(e["action"], "download")}
        for e in entries
    ]


ERASABLE = "connection_docs"  # personal data: the category erasure exists for


@router.post("/files/{file_id}/erase")
async def erase(
    file_id: str, request: Request, principal: Principal = Depends(require_roles("admin"))
) -> dict:
    """Right to erasure for a document (improvements.md 2.5). The file's encryption key is
    destroyed, so its ciphertext becomes unreadable everywhere it still exists (replica,
    storage snapshot, restored backup of the storage); the stored objects are deleted as
    well. The metadata row and the audit trail stay: the fact that it existed, who read it
    and who erased it must remain provable. Irreversible."""
    deps = request.app.state.deps
    rec = await _load_or_404(deps, file_id)
    if rec.category != ERASABLE:
        raise HTTPException(422, f"Only {ERASABLE} can be erased")
    row = await deps.repo.get_file_key(file_id)
    if row and row["status"] == "erased":
        raise HTTPException(410, "File already erased")
    destroyed = await keystore.destroy(deps, file_id, principal.email)
    removed = []
    copy = await deps.repo.get_replica(file_id)
    targets = [(rec.storage, rec.storage_key)] + ([(copy["storage"], copy["storage_key"])] if copy else [])
    for name, key in targets:
        try:
            await deps.storages[name].delete(key)
            removed.append(name)
        except StorageError:
            log.warning("object not deleted during erasure", extra={"file_id": file_id, "storage": name})
    method = "key_destroyed" if destroyed else "objects_deleted_only"
    await deps.repo.add_audit(
        file_id, "erased", principal.email, trace_id_var.get(),
        "Document șters definitiv: cheia de criptare a fost distrusă"
        if destroyed else
        "Document șters: obiectele au fost șterse (fișier mai vechi, fără cheie proprie)")
    log.warning("file erased", extra={"file_id": file_id, "user": principal.email, "method": method})
    return {"file_id": file_id, "erased": True, "method": method, "objects_deleted_from": removed}
