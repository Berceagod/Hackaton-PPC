import asyncio

from fastapi import APIRouter, Request

router = APIRouter()

CHECK_TIMEOUT_SECONDS = 8.0


@router.get("/livez")
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


@router.get("/health")
async def health(request: Request) -> dict:
    """Dependency report. Always HTTP 200 while the process is alive; `status` is
    "degraded" when any dependency is down. Checks run concurrently with a timeout."""
    deps = request.app.state.deps
    names = ["metadata_db", *deps.storages]
    probes = [deps.repo.healthy, *(s.healthy for s in deps.storages.values())]
    results = await asyncio.gather(*(_check(p) for p in probes))
    checks = dict(zip(names, results))
    return {"status": "ok" if all(results) else "degraded", "checks": checks}
