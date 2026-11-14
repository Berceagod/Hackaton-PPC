import asyncio

from fastapi import APIRouter, Depends, Request

from auth import Principal, require_roles

# Internal paths (/livez, /health): reachable only on the Docker network, because nginx
# proxies nothing but /api/. Used by the container healthcheck and by Prometheus.
router = APIRouter()
# Public paths (/api/v1/livez, /api/v1/health): exposed through nginx. Liveness reveals
# nothing; the dependency report (which storages exist and which are down) needs a user.
api_router = APIRouter()

CHECK_TIMEOUT_SECONDS = 8.0


async def livez() -> dict:
    """Liveness only: is this process serving requests? Used by the container
    healthcheck. It deliberately checks no dependency, so a slow or dead storage can
    never get the backend marked unhealthy and restarted."""
    return {"status": "alive"}


async def _check(probe) -> bool:
    try:
        return bool(await asyncio.wait_for(probe(), CHECK_TIMEOUT_SECONDS))
    except Exception:  # a failing or hanging dependency is simply "down"
        return False


async def health(request: Request) -> dict:
    """Dependency report. Always HTTP 200 while the process is alive; `status` is
    "degraded" when any dependency is down. Checks run concurrently with a timeout."""
    deps = request.app.state.deps
    names = ["metadata_db", *deps.storages]
    probes = [deps.repo.healthy, *(s.healthy for s in deps.storages.values())]
    results = await asyncio.gather(*(_check(p) for p in probes))
    checks = dict(zip(names, results))
    return {"status": "ok" if all(results) else "degraded", "checks": checks}


async def health_for_users(
    request: Request,
    _: Principal = Depends(require_roles("dispecer", "admin")),
) -> dict:
    return await health(request)


router.add_api_route("/livez", livez, methods=["GET"])
router.add_api_route("/health", health, methods=["GET"])
api_router.add_api_route("/livez", livez, methods=["GET"])
api_router.add_api_route("/health", health_for_users, methods=["GET"])
