"""Testing overlay API (admin only; off unless TESTING_ENABLED=true) and the live storage
latency for the Analytics tab (any signed-in user, needs only Prometheus)."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

import testing
from auth import Principal, require_roles
from logging_setup import trace_id_var

log = logging.getLogger("filegrid.testing")
router = APIRouter()
ADMIN = require_roles("admin")
TESTING_AUDIT_ID = "00000000-0000-0000-0000-00000000c4a1"  # audit trail of the testing overlay


def _enabled(deps) -> None:
    if not deps.settings.testing_enabled:
        raise HTTPException(403, "Testing overlay is disabled (set TESTING_ENABLED=true to use it)")


async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(422, "Body must be a JSON object") from None
    if not isinstance(body, dict):
        raise HTTPException(422, "Body must be a JSON object")
    return body


async def _audit(deps, principal: Principal, details: str) -> None:
    await deps.repo.add_audit(TESTING_AUDIT_ID, "testing", principal.email, trace_id_var.get(), details)
    log.warning("testing overlay used", extra={"user": principal.email, "details": details})


@router.get("/admin/testing")
async def status(request: Request, _: Principal = Depends(ADMIN)) -> dict:
    deps = request.app.state.deps
    if not deps.settings.testing_enabled:
        return {"enabled": False}
    gen = await deps.repo.get_testing("generator")
    job = await deps.repo.get_testing("job")
    quiet = await deps.repo.get_testing("quiet")
    return {
        "enabled": True,
        "quiet": {"on": quiet is not None, "remaining_seconds": (quiet or {}).get("_remaining", 0)},
        "generator": {"on": bool(gen and gen.get("on")), "categories": (gen or {}).get("categories", []),
                      "rate": (gen or {}).get("rate", 1.0), "remaining_seconds": (gen or {}).get("_remaining", 0)},
        "simulator": await testing.simulator_status(deps),
        "job": ({k: v for k, v in job.items() if not k.startswith("_")} if job else None),
        "jobs": testing.JOBS,
        "limits": {"max_rate": testing.MAX_RATE, "max_duration": testing.MAX_DURATION},
    }


@router.put("/admin/testing/generator")
async def generator(request: Request, principal: Principal = Depends(ADMIN)) -> dict:
    """Body: {"on": bool, "categories": [...], "rate": 0.2-5 files/s per category, "duration_seconds"}."""
    deps = request.app.state.deps
    _enabled(deps)
    body = await _body(request)
    if body.get("on") is not True:
        await deps.repo.clear_testing("generator")
        await _audit(deps, principal, "Generator de fișiere sintetice oprit")
        return {"on": False}
    categories = body.get("categories")
    if (not isinstance(categories, list) or not categories
            or any(c not in testing.GENERATOR_CATEGORIES for c in categories)):
        raise HTTPException(422, f"categories must be a non-empty list of {list(testing.GENERATOR_CATEGORIES)}")
    rate, duration = body.get("rate", 1.0), body.get("duration_seconds", 600)
    for name, value, low, high in (("rate", rate, 0.2, testing.MAX_RATE),
                                   ("duration_seconds", duration, 10, testing.MAX_DURATION)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
            raise HTTPException(422, f"{name} must be a number between {low} and {high}")
    await deps.repo.set_testing(
        "generator", {"on": True, "categories": categories, "rate": float(rate)}, float(duration))
    await _audit(deps, principal, f"Generator pornit: {', '.join(categories)}, {rate:g}/s fiecare, {duration:g} s")
    return {"on": True, "categories": categories, "rate": float(rate), "duration_seconds": float(duration)}


@router.put("/admin/testing/quiet")
async def quiet(request: Request, principal: Principal = Depends(ADMIN)) -> dict:
    """Body: {"on": bool, "duration_seconds": 60-3600 (default 1800)}. While on, the storage
    canary and the background scrubber pause, so only the traffic you create is visible. It ends
    by itself: the safety checks must not stay off by accident."""
    deps = request.app.state.deps
    _enabled(deps)
    body = await _body(request)
    if body.get("on") is not True:
        await deps.repo.clear_testing("quiet")
        await _audit(deps, principal, "Mod liniște oprit: verificările de fundal rulează din nou")
        return {"on": False}
    duration = body.get("duration_seconds", 1800)
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not 60 <= duration <= 3600:
        raise HTTPException(422, "duration_seconds must be a number between 60 and 3600")
    await deps.repo.set_testing("quiet", {"on": True}, float(duration))
    await _audit(deps, principal, f"Mod liniște pornit {duration:g} s: canarul și scrubber-ul sunt în pauză")
    return {"on": True, "duration_seconds": float(duration)}


@router.post("/admin/testing/simulator")
async def simulator(request: Request, principal: Principal = Depends(ADMIN)) -> dict:
    """Body: {"action": "storm" | "normal" | "pause" | "resume"} (relayed to the meter simulator)."""
    deps = request.app.state.deps
    _enabled(deps)
    action = (await _body(request)).get("action")
    calls = {"storm": ("POST", "/mode", {"mode": "storm"}), "normal": ("POST", "/mode", {"mode": "normal"}),
             "pause": ("PATCH", "/config", {"paused": True}), "resume": ("PATCH", "/config", {"paused": False})}
    if action not in calls:
        raise HTTPException(422, f"action must be one of {list(calls)}")
    try:
        await testing.simulator(deps, *calls[action])
    except Exception as exc:
        raise HTTPException(502, "The meter simulator is not reachable") from exc
    await _audit(deps, principal, f"Simulator contoare: {action}")
    return {"action": action, "simulator": await testing.simulator_status(deps)}


@router.post("/admin/testing/jobs/{name}", status_code=202)
async def start_job(name: str, request: Request, principal: Principal = Depends(ADMIN)) -> dict:
    deps = request.app.state.deps
    _enabled(deps)
    if name not in testing.JOBS:
        raise HTTPException(404, f"Unknown job '{name}'")
    if not await testing.start_job(deps, name, principal.email):
        raise HTTPException(409, "Another simulation is still running")
    await _audit(deps, principal, f"Simulare pornită: {testing.JOBS[name]}")
    return {"started": name}


@router.get("/analytics/live")
async def live(request: Request, _: Principal = Depends(require_roles("dispecer", "admin"))) -> dict:
    return await testing.live_latency(request.app.state.deps)
