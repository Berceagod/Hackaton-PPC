"""Ingest, list, download (integrity-checked) and verify."""

import json
import logging
import uuid

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
from integrity import HashingStream, sha256_of_stream, upload_chunks
from logging_setup import trace_id_var
from meter_ingest import ingest_meter_reading
from policies import CATEGORIES, STORAGES
from repo import FileRecord
from storage.base import ObjectNotFound, StorageError

log = logging.getLogger("filegrid.files")
router = APIRouter()

INGEST_ROLES = require_roles("dispecer", "admin", "service")
USERS = require_roles("dispecer", "admin")  # people; the simulator token cannot read

# The UI knows two audit actions. Every finer-grained event maps onto one of them
# (the exact event is still returned in `event`).
_UI_ACTION = {"upload": "upload"}


def _safe_filename(name: str) -> str:
    """Keep a client-supplied filename from breaking out of the Content-Disposition header."""
    cleaned = "".join(c for c in name if c.isprintable() and c not in '"\\/')
    return cleaned[:200] or "file"


async def _storage_for(deps, category: str):
    policy = await deps.policies.get(category)
    if policy.encryption:
        raise HTTPException(501, "Encryption not implemented yet (slice F)")
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
    file_id = str(uuid.uuid4())
    key = f"{category}/{file_id}"

    # Single streaming pass: hash while uploading. Nothing is acknowledged
    # until the storage confirmed the durable write.
    stream = HashingStream(upload_chunks(file))
    try:
        size = await storage.put_stream(key, stream)
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
        encrypted=False,
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
    _: Principal = Depends(USERS),
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


async def _verify(deps, rec: FileRecord) -> tuple[bool, str, str]:
    """Re-hash the stored bytes. Returns (ok, actual_sha256, reason)."""
    storage = deps.storages.get(rec.storage)
    if storage is None:
        raise HTTPException(503, f"Storage '{rec.storage}' not available")
    try:
        actual, _ = await sha256_of_stream(storage.get_stream(rec.storage_key))
    except ObjectNotFound:
        return False, "", "object_missing"
    except StorageError as exc:
        raise HTTPException(503, f"Storage '{rec.storage}' unavailable") from exc
    return actual == rec.sha256, actual, "" if actual == rec.sha256 else "checksum_mismatch"


@router.get("/files/{file_id}/verify")
async def verify(
    file_id: str, request: Request, principal: Principal = Depends(USERS)
) -> dict:
    deps = request.app.state.deps
    rec = await _load_or_404(deps, file_id)
    ok, actual, reason = await _verify(deps, rec)
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

    # Verify BEFORE delivering any byte: a corrupt file is never served.
    ok, actual, reason = await _verify(deps, rec)
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

    await deps.repo.add_audit(
        file_id, "download", principal.email, trace_id, "Integritate verificată (SHA-256)"
    )
    storage = deps.storages[rec.storage]
    return StreamingResponse(
        storage.get_stream(rec.storage_key),
        media_type="application/octet-stream",
        headers={
            "X-Integrity-Verified": "true",
            "X-File-Sha256": rec.sha256,
            "Content-Length": str(rec.size_bytes),
            "Content-Disposition": f'attachment; filename="{_safe_filename(rec.filename)}"',
        },
    )


@router.get("/files/{file_id}/audit")
async def audit(
    file_id: str, request: Request, _: Principal = Depends(USERS)
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
