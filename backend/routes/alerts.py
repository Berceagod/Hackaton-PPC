"""GET /alerts: what is wrong right now, for the app's alert bell.

Merges two sources:
- Prometheus: the alert rules in infrastructure/prometheus/alerts.yml that are firing (or about
  to: `pending`). If Prometheus cannot be reached the response says so and still carries the
  application's own alerts;
- the application itself: events it knows first-hand, such as an automatic failover.
"""

import asyncio
import json
import logging
import urllib.error
import urllib.request

from fastapi import APIRouter, Depends, Request

from auth import Principal, require_roles

log = logging.getLogger("filegrid.alerts")
router = APIRouter()
READERS = require_roles("dispecer", "admin", "auditor")
ORDER = {"critical": 0, "warning": 1, "info": 2}


def _prometheus_alerts(base: str, timeout: float) -> list[dict] | None:
    url = base.rstrip("/") + "/api/v1/alerts"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("status") != "success":
        return None
    alerts = (payload.get("data") or {}).get("alerts")
    return alerts if isinstance(alerts, list) else None


def _from_prometheus(item: dict) -> dict:
    labels, notes = item.get("labels") or {}, item.get("annotations") or {}
    name = labels.get("alertname", "alert")
    extra = ",".join(f"{k}={v}" for k, v in sorted(labels.items()) if k not in ("alertname", "severity", "guarantee"))
    return {
        "id": f"prom:{name}:{extra}",
        "source": "prometheus",
        "severity": labels.get("severity", "warning"),
        "title": notes.get("summary") or name,
        "detail": notes.get("description") or "",
        "runbook": notes.get("runbook"),
        "guarantee": labels.get("guarantee"),
        "category": labels.get("category"),
        "storage": labels.get("storage"),
        "since": item.get("activeAt"),
        "state": item.get("state", "firing"),
        "name": name,
    }


def _from_app(row: dict) -> dict:
    return {
        "id": f"app:{row['key']}",
        "source": "app",
        "severity": row["severity"],
        "title": row["title"],
        "detail": row["detail"],
        "runbook": None,
        "guarantee": None,
        "category": row.get("category"),
        "storage": None,
        "since": row["raised_at"],
        "state": "resolved" if row.get("resolved_at") else "firing",
        "name": row["key"],
    }


def _target(item: dict) -> str:
    """Short label of what an alert is about: 'outage_records · s3'."""
    return " · ".join(x for x in (item.get("category"), item.get("storage")) if x) or item["name"]


def group(items: list[dict]) -> list[dict]:
    """One card per kind of alert. The same rule firing for three storages is ONE problem seen in
    three places, so it is shown once with a count and the list of what it affects."""
    buckets: dict[tuple, list[dict]] = {}
    for item in items:
        buckets.setdefault((item["name"], item["state"], item["source"]), []).append(item)
    out = []
    for (name, _state, _source), members in buckets.items():
        first = members[0]
        if len(members) == 1:
            out.append({**first, "count": 1, "targets": []})
            continue
        since = min((m["since"] for m in members if m.get("since")), default=None)
        out.append({**first, "id": f"group:{name}:{first['state']}", "count": len(members),
                    "title": f"{name}: {len(members)} cazuri active",
                    "since": since, "category": None, "storage": None,
                    "targets": sorted({_target(m) for m in members})})
    return out


@router.get("/alerts")
async def alerts(request: Request, _: Principal = Depends(READERS)) -> dict:
    deps = request.app.state.deps
    prom = await asyncio.to_thread(
        _prometheus_alerts, deps.settings.prometheus_url, deps.settings.analytics_timeout_seconds)
    items = [_from_prometheus(a) for a in (prom or []) if a.get("state") in ("firing", "pending")]
    try:
        items += [_from_app(r) for r in await deps.repo.list_alerts(50)]
    except Exception:
        log.warning("application alerts unavailable", exc_info=True)
    items = group(items)
    items.sort(key=lambda a: (a["state"] == "resolved", a["state"] == "pending",
                              ORDER.get(a["severity"], 3), a["since"] or ""))
    active = [a for a in items if a["state"] == "firing"]
    return {
        "alerts": items,
        "counts": {sev: sum(1 for a in active if a["severity"] == sev) for sev in ORDER},
        "prometheus_ok": prom is not None,
    }
