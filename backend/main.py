"""FileGrid backend entrypoint."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from bulkhead import Bulkheads
from chunked_upload import purge_stale_uploads
from config import Settings
from deps import Deps
from logging_setup import setup_logging
from meter_outbox import run_drainer
from policies import PolicyStore
from repo import PostgresRepo
from routes import admin, auth_routes, files, health, uploads
from storage.base import StorageError
from storage.breaker import BreakerStorage
from storage.ftp_storage import FtpStorage
from storage.postgres_storage import PostgresStorage
from storage.s3_storage import S3Storage
from tracing import TraceMiddleware

log = logging.getLogger("filegrid.main")

PURGE_INTERVAL_SECONDS = 900


async def build_real_deps() -> Deps:
    settings = Settings.from_env()
    # Pools are per worker process: 4 workers x (6 + 4) = 40 connections per container, so
    # two containers stay under Postgres' default limit of 100 (slice G).
    repo = await PostgresRepo.connect(settings.database_url, max_size=settings.db_pool)
    await repo.init_schema()
    # Meter readings get a pool of their own (same DB): a storm of readings cannot take
    # the connections the UI categories need (bulkhead, slice G).
    meter_repo = await PostgresRepo.connect(settings.database_url, max_size=settings.meter_db_pool)
    s3 = S3Storage(
        settings.s3_endpoint,
        settings.s3_access_key,
        settings.s3_secret_key,
        settings.s3_bucket,
    )
    try:
        await s3.ensure_ready(attempts=15)
    except StorageError:
        log.warning("S3 not ready at startup; /health will report degraded until it is")
    policies = PolicyStore.from_yaml(settings.policies_path, persistence=repo)
    await policies.load()
    try:  # a policy saved by any worker/replica reaches this one at once (slice G)
        await repo.listen_policies(policies.invalidate)
    except Exception:
        log.warning("policy change notifications unavailable; falling back to periodic refresh")

    def breaker(storage):
        return BreakerStorage(storage, settings.breaker_threshold,
                              settings.breaker_cooldown_seconds)

    return Deps(
        settings=settings,
        repo=repo,
        storages={
            "postgres": breaker(PostgresStorage(settings.blob_database_url)),
            "s3": breaker(s3),
            "ftp": breaker(FtpStorage(
                settings.ftp_host,
                settings.ftp_port,
                settings.ftp_user,
                settings.ftp_password,
                settings.ftp_max_concurrency,
            )),
        },
        policies=policies,
        meter_repo=meter_repo,
        bulkheads=Bulkheads(settings.bulkhead_limits, settings.bulkhead_wait_seconds),
    )


async def _purge_loop(deps: Deps, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await purge_stale_uploads(deps, deps.settings.upload_ttl_hours * 3600)
        except Exception:
            log.exception("purge of abandoned uploads failed")
        try:
            await asyncio.wait_for(stop.wait(), PURGE_INTERVAL_SECONDS)
        except (asyncio.TimeoutError, TimeoutError):
            pass


def create_app(deps: Deps | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging()
        app.state.deps = deps or await build_real_deps()
        stop = asyncio.Event()
        tasks = []
        if deps is None:  # real service: background workers (tests drive them directly)
            tasks = [asyncio.create_task(run_drainer(app.state.deps, stop)),
                     asyncio.create_task(_purge_loop(app.state.deps, stop))]
        log.info("backend ready")
        yield
        stop.set()
        for t in tasks:
            try:
                await asyncio.wait_for(t, 5)
            except Exception:
                t.cancel()

    app = FastAPI(title="FileGrid API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(TraceMiddleware)
    app.include_router(health.router)  # /livez, /health: internal only (docker, prometheus)
    for prefix in ("/api/v1",):
        app.include_router(health.api_router, prefix=prefix)  # /health needs a user here
        app.include_router(auth_routes.router, prefix=prefix)
        app.include_router(admin.router, prefix=prefix)
        app.include_router(files.router, prefix=prefix)
        app.include_router(uploads.router, prefix=prefix)
    return app


app = create_app()
