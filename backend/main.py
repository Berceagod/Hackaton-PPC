"""FileGrid backend entrypoint."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from config import Settings
from deps import Deps
from logging_setup import setup_logging
from policies import PolicyStore
from repo import PostgresRepo
from routes import admin, auth_routes, files, health
from storage.base import StorageError
from storage.ftp_storage import FtpStorage
from storage.postgres_storage import PostgresStorage
from storage.s3_storage import S3Storage
from tracing import TraceMiddleware

log = logging.getLogger("filegrid.main")


async def build_real_deps() -> Deps:
    settings = Settings.from_env()
    repo = await PostgresRepo.connect(settings.database_url)
    await repo.init_schema()
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
    return Deps(
        settings=settings,
        repo=repo,
        storages={
            "postgres": PostgresStorage(settings.blob_database_url),
            "s3": s3,
            "ftp": FtpStorage(
                settings.ftp_host,
                settings.ftp_port,
                settings.ftp_user,
                settings.ftp_password,
                settings.ftp_max_concurrency,
            ),
        },
        policies=policies,
    )


def create_app(deps: Deps | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging()
        app.state.deps = deps or await build_real_deps()
        log.info("backend ready")
        yield

    app = FastAPI(title="FileGrid API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(TraceMiddleware)
    app.include_router(health.router)  # /health (docker healthcheck)
    for prefix in ("/api/v1",):
        app.include_router(health.router, prefix=prefix)
        app.include_router(auth_routes.router, prefix=prefix)
        app.include_router(admin.router, prefix=prefix)
        app.include_router(files.router, prefix=prefix)
    return app


app = create_app()
