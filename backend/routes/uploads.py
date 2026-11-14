"""Chunked, resumable upload endpoints (ADR-0010)."""

from fastapi import APIRouter, Depends, Form, Request, UploadFile

import metrics
from auth import Principal, require_roles
from chunked_upload import STAGING_STORAGE, ChunkRequest, receive_chunk, upload_status
from logging_setup import trace_id_var
from policies import CATEGORIES
from routes.files import _storage_for

router = APIRouter()
USERS = require_roles("dispecer", "admin")  # the simulator token cannot upload files


@router.post("/upload/chunk")
async def upload_chunk(
    request: Request,
    file_chunk: UploadFile,
    upload_id: str = Form(...),
    chunk_index: int = Form(...),
    total_chunks: int = Form(...),
    filename: str = Form(...),
    category: str = Form(...),
    metadata: str = Form("{}"),
    sha256: str | None = Form(None),
    size_bytes: int | None = Form(None),
    chunk_sha256: str | None = Form(None),
    principal: Principal = Depends(USERS),
):
    req = ChunkRequest(upload_id, chunk_index, total_chunks, filename, category, metadata,
                       sha256, size_bytes, chunk_sha256)
    deps = request.app.state.deps
    # slice H: every chunk is an observation; its bytes land on the staging storage
    async with metrics.observe(category, "chunk") as obs:
        obs.note(storage=STAGING_STORAGE)
        resp = await _receive(deps, principal, category, req, file_chunk)
        if hasattr(resp, "status_code"):  # a JSONResponse (409 duplicate, ...), not a plain dict
            obs.from_response(resp.status_code)
        return resp


async def _receive(deps, principal, category, req, file_chunk):
    if deps.bulkheads is None or category not in CATEGORIES:
        return await receive_chunk(deps, principal, trace_id_var.get(), req, file_chunk,
                                   _storage_for)
    priority = (await deps.policies.get(category)).priority  # bulkhead (slice G)
    async with deps.bulkheads.slot(category, priority):
        return await receive_chunk(deps, principal, trace_id_var.get(), req, file_chunk,
                                   _storage_for)


@router.get("/upload/{upload_id}")
async def get_upload(upload_id: str, request: Request, principal: Principal = Depends(USERS)):
    return await upload_status(request.app.state.deps, principal, upload_id)
