"""Chaos console API (improvements.md 2.1): break things on purpose, safely, and watch.

Guards: the console does nothing unless CHAOS_ENABLED=true (default off); only an admin may
use it; every change is written to the audit trail (file id 0000...c4a0) and logged; every fault
expires by itself (at most CHAOS_MAX_SECONDS) so a forgotten experiment cannot become an outage.
`GET /admin/chaos` always answers (so the UI can say the console is off); the rest is refused
with 403 while it is off.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from auth import Principal, require_roles
from chaos import CHAOS_AUDIT_ID, network_client
from logging_setup import trace_id_var
from policies import STORAGES

log = logging.getLogger("filegrid.chaos")
router = APIRouter()
ADMIN = require_roles("admin")


def _enabled(deps) -> None:
    if not deps.settings.chaos_enabled:
        raise HTTPException(403, "Chaos console is disabled (set CHAOS_ENABLED=true to use it)")


async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(422, "Body must be a JSON object") from None
    if not isinstance(body, dict):
        raise HTTPException(422, "Body must be a JSON object")
    return body


def _number(body: dict, key: str, default: float, low: float, high: float) -> float:
    value = body.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise HTTPException(422, f"{key} must be a number between {low} and {high}")
    return float(value)


async def _audit(deps, principal: Principal, details: str) -> None:
    await deps.repo.add_audit(CHAOS_AUDIT_ID, "chaos", principal.email, trace_id_var.get(), details)
    log.warning("chaos console used", extra={"user": principal.email, "details": details})


@router.get("/admin/chaos")
async def status(request: Request, _: Principal = Depends(ADMIN)) -> dict:
    deps = request.app.state.deps
    s = deps.settings
    body: dict = {"enabled": s.chaos_enabled, "max_seconds": s.chaos_max_seconds,
                  "default_seconds": s.chaos_default_seconds}
    if not s.chaos_enabled:
        return body
    faults = await deps.repo.active_faults()
    network = None
    try:
        network = await network_client(deps).degraded()
    except Exception:  # toxiproxy unreachable: say so instead of guessing
        log.warning("toxiproxy not reachable", exc_info=True)
    body.update(
        faults=faults,
        network_degraded=network,
        storages={name: {"circuit": getattr(st, "state", "closed")}
                  for name, st in deps.storages.items()},
        recent=(await deps.repo.list_audit(CHAOS_AUDIT_ID))[:15],
    )
    return body


@router.put("/admin/chaos/storage/{name}")
async def break_storage(
    name: str, request: Request, principal: Principal = Depends(ADMIN)
) -> dict:
    """Body: {"kind": "down" | "slow", "seconds": 0.1-30 (slow only), "duration_seconds": ...}"""
    deps = request.app.state.deps
    _enabled(deps)
    if name not in STORAGES:
        raise HTTPException(404, f"Unknown storage '{name}'")
    body = await _body(request)
    kind = body.get("kind")
    if kind not in ("down", "slow"):
        raise HTTPException(422, "kind must be 'down' or 'slow'")
    s = deps.settings
    seconds = _number(body, "seconds", 2.0, 0.1, 30.0) if kind == "slow" else 0.0
    duration = _number(body, "duration_seconds", s.chaos_default_seconds, 5.0, s.chaos_max_seconds)
    await deps.repo.set_fault(name, kind, seconds, duration, principal.email)
    what = "oprit" if kind == "down" else f"încetinit (+{seconds:g} s pe operație)"
    await _audit(deps, principal, f"Storage {name} {what} pentru {duration:g} s (simulat)")
    return {"target": name, "kind": kind, "seconds": seconds, "duration_seconds": duration}


@router.delete("/admin/chaos/storage/{name}")
async def heal_storage(
    name: str, request: Request, principal: Principal = Depends(ADMIN)
) -> dict:
    deps = request.app.state.deps
    _enabled(deps)
    if name not in STORAGES:
        raise HTTPException(404, f"Unknown storage '{name}'")
    cleared = await deps.repo.clear_faults(name)
    await _audit(deps, principal, f"Storage {name}: defecțiune simulată oprită manual")
    return {"target": name, "cleared": cleared}


@router.put("/admin/chaos/network")
async def network(request: Request, principal: Principal = Depends(ADMIN)) -> dict:
    """Body: {"mode": "bad" | "ok", "duration_seconds": ...}. `bad` = 400±200 ms latency each
    way, 2 MB/s upload and 20% of connections cut. It also slows THIS console's own requests."""
    deps = request.app.state.deps
    _enabled(deps)
    body = await _body(request)
    mode = body.get("mode")
    if mode not in ("bad", "ok"):
        raise HTTPException(422, "mode must be 'bad' or 'ok'")
    client = network_client(deps)
    duration = None
    if mode == "bad":
        duration = _number(body, "duration_seconds", deps.settings.chaos_default_seconds,
                           5.0, deps.settings.chaos_max_seconds)
    try:
        if mode == "bad":
            await client.degrade()
            await deps.repo.set_fault("network", "bad", 0, duration, principal.email)
            await _audit(deps, principal,
                         f"Rețea degradată pentru {duration:g} s (latență, bandă, conexiuni tăiate)")
            return {"mode": "bad", "duration_seconds": duration}
        await client.restore()
        await deps.repo.clear_faults("network")
        await _audit(deps, principal, "Rețea restabilită manual")
        return {"mode": "ok"}
    except Exception as exc:
        log.exception("toxiproxy call failed")
        raise HTTPException(502, "Toxiproxy is not reachable") from exc


@router.delete("/admin/chaos")
async def clear_all(request: Request, principal: Principal = Depends(ADMIN)) -> dict:
    """The panic button: remove every injected fault and restore the network."""
    deps = request.app.state.deps
    _enabled(deps)
    cleared = await deps.repo.clear_faults(None)
    restored = True
    try:
        await network_client(deps).restore()
    except Exception:
        restored = False
        log.warning("toxiproxy not reachable while clearing", exc_info=True)
    await _audit(deps, principal, f"Toate defecțiunile simulate au fost oprite ({cleared})")
    return {"cleared": cleared, "network_restored": restored}
