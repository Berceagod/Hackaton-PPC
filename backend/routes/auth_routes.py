import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

import metrics
from auth import authenticate, issue_token
from logging_setup import trace_id_var

log = logging.getLogger("filegrid.auth")
AUTH_AUDIT_ID = "00000000-0000-0000-0000-0000000a0701"  # audit trail of account lockouts

router = APIRouter()


class LoginBody(BaseModel):
    email: str
    password: str


@router.post("/auth/login")
async def login(body: LoginBody, request: Request) -> dict:
    deps = request.app.state.deps
    settings = deps.settings
    email = body.email.strip().lower()[:200]

    # lockout with backoff: unknown accounts are tracked too, so the answer never reveals
    # whether an account exists
    failures, ago = await deps.repo.login_state(email)
    if failures >= settings.login_max_failures:
        lock = min(settings.login_lock_base_seconds * 2 ** (failures - settings.login_max_failures),
                   settings.login_lock_max_seconds)
        remaining = lock - (ago or 0.0)
        if remaining > 0:
            metrics.login("locked")
            raise HTTPException(
                status_code=429, detail="Too many failed attempts: try again later",
                headers={"Retry-After": str(int(remaining) + 1)})

    user = authenticate(settings, body.email, body.password)
    if user is None:
        metrics.login("failed")
        count = await deps.repo.login_failed(email)
        if count == settings.login_max_failures:  # the moment it locks, once
            log.warning("account locked after repeated failed logins", extra={"account": email})
            await deps.repo.add_audit(
                AUTH_AUDIT_ID, "login_locked", email, trace_id_var.get(),
                f"Cont blocat temporar după {count} autentificări eșuate")
        raise HTTPException(status_code=401, detail="Invalid credentials")
    await deps.repo.login_ok(email)
    metrics.login("ok")
    return {"token": issue_token(settings, user), "user": user}
