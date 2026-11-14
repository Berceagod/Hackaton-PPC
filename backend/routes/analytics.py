"""GET /analytics/summary and the measurement log the benchmark and scenarios write (slice I)."""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from analytics import build_summary, fetch_live_metrics, parse_measurement_document
from auth import Principal, require_roles

log = logging.getLogger("filegrid.analytics")
router = APIRouter()


@router.get("/analytics/summary")
async def analytics_summary(
    request: Request,
    _: Principal = Depends(require_roles("dispecer", "admin")),
) -> dict:
    deps = request.app.state.deps
    repo = deps.repo
    await repo.ensure_analytics_schema()
    try:
        live = await fetch_live_metrics(
            deps.settings.prometheus_url,
            deps.settings.analytics_timeout_seconds,
            deps.settings.analytics_range,
        )
    except Exception:
        log.info("prometheus unreachable; analytics summary uses stored measurements")
        live = None
    return build_summary(
        await deps.policies.all(),
        await repo.latest_measurements(),
        await repo.analytics_snapshot(),
        live,
        deps.settings.analytics_range,
    )


@router.post("/analytics/measurements", status_code=204)
async def post_measurements(
    request: Request,
    _: Principal = Depends(require_roles("dispecer", "admin")),
) -> Response:
    try:
        kind, scenario, rows = parse_measurement_document(json.loads(await request.body()))
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(422, f"Invalid measurements: {exc}") from exc
    repo = request.app.state.deps.repo
    await repo.ensure_analytics_schema()
    await repo.save_measurements(kind, scenario, rows)
    return Response(status_code=204)
