"""Evidence endpoints: audit-chain verification, signed receipts, scrubber status."""

import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

import metrics
import receipts
from auth import Principal, require_roles
from logging_setup import trace_id_var
from routes.files import _load_or_404, _verify

log = logging.getLogger("filegrid.trust")
router = APIRouter()

ADMIN = require_roles("admin")
USERS = require_roles("dispecer", "admin")
MAX_RECEIPT_BYTES = 64 * 1024  # a receipt is about 1 KB; refuse anything bigger


@router.get("/audit/verify")
async def audit_verify(
    request: Request, _: Principal = Depends(require_roles("admin", "auditor"))
) -> dict:
    """Recompute the whole audit chain. `valid: false` names the first row that does not fit."""
    result = await request.app.state.deps.repo.verify_audit_chain()
    if not result["valid"]:
        log.error("audit chain verification failed", extra={"first_break": result["first_break"]})
    return result


@router.get("/admin/scrubber")
async def scrubber_status(request: Request, _: Principal = Depends(ADMIN)) -> dict:
    deps = request.app.state.deps
    s = deps.settings
    stats = await deps.repo.scrub_stats(list(s.scrub_categories))
    return {
        "enabled": s.scrub_enabled,
        "interval_seconds": s.scrub_interval_seconds,
        "batch": s.scrub_batch,
        "recheck_seconds": s.scrub_recheck_seconds,
        "max_bytes": s.scrub_max_bytes,
        "categories": list(s.scrub_categories),
        **stats,
    }


@router.get("/files/{file_id}/receipt")
async def receipt(
    file_id: str,
    request: Request,
    verify: bool = Query(True, description="re-hash the stored bytes before signing"),
    principal: Principal = Depends(USERS),
) -> JSONResponse:
    """A signed statement about this file. By default the bytes are re-verified first; a file
    that fails the check gets NO receipt (409), exactly like a download."""
    deps = request.app.state.deps
    rec = await _load_or_404(deps, file_id)
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    row = await deps.repo.get_file_key(file_id)
    if row and row["status"] == "erased":
        raise HTTPException(410, "File erased: no receipt can be issued")
    verification: dict = {"performed": False, "ok": None, "checked_at": None, "reason": None}
    if verify:
        async with metrics.observe(rec.category, "verify") as obs:
            obs.note(storage=rec.storage)
            ok, _actual, reason = await _verify(deps, rec)
            if not ok:
                metrics.integrity_failure(rec.category, rec.storage, reason)
                obs.note(outcome="integrity_error")
            else:
                obs.add_bytes(rec.size_bytes)
        if not ok:
            await deps.repo.add_audit(
                file_id, "receipt_refused", principal.email, trace_id_var.get(),
                f"Chitanță refuzată: integritate eșuată ({reason})",
            )
            return JSONResponse(status_code=409, headers={"X-Integrity-Verified": "false"},
                                content={"error": "integrity_check_failed", "reason": reason,
                                         "file_id": file_id})
        verification = {"performed": True, "ok": True, "checked_at": checked_at, "reason": None}

    payload = {
        "version": 1,
        "receipt_id": str(uuid.uuid4()),
        "issued_at": checked_at,
        "issuer": "FileGrid",
        "file": {
            "id": rec.id,
            "filename": rec.filename,
            "category": rec.category,
            "storage": rec.storage,
            "size_bytes": rec.size_bytes,
            "sha256": rec.sha256,
            "encrypted": rec.encrypted,
            "stored_at": rec.created_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
            "trace_id": rec.trace_id,
        },
        "verification": verification,
    }
    await deps.repo.add_audit(
        file_id, "receipt", principal.email, trace_id_var.get(),
        "Chitanță de integritate emisă" + (" (verificată acum)" if verify else " (fără reverificare)"),
    )
    return JSONResponse(content=receipts.sign(deps.settings.master_key, payload))


@router.get("/receipts/public-key")
async def receipts_public_key(request: Request) -> dict:
    """Public on purpose: it is what lets anyone check a receipt without trusting us."""
    return receipts.public_key_info(request.app.state.deps.settings.master_key)


@router.post("/receipts/verify")
async def receipts_verify(request: Request) -> dict:
    """Public: is this receipt one we signed, and unchanged?"""
    raw = await request.body()
    if len(raw) > MAX_RECEIPT_BYTES:
        raise HTTPException(413, "receipt too large")
    try:
        doc = json.loads(raw)
    except ValueError:
        raise HTTPException(422, "body must be a JSON receipt") from None
    valid, reason = receipts.verify(request.app.state.deps.settings.master_key, doc)
    if reason in ("malformed", "malformed_signature"):
        raise HTTPException(422, f"not a receipt ({reason})")
    return {"valid": valid, "reason": reason}
