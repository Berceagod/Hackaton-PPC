"""Runtime configuration, read from environment variables only (no secrets in code)."""

import os
from dataclasses import dataclass


def _req(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


@dataclass(frozen=True)
class Settings:
    jwt_secret: str
    simulator_token: str
    database_url: str
    s3_endpoint: str
    s3_access_key: str
    s3_secret_key: str
    s3_bucket: str
    policies_path: str
    blob_database_url: str
    ftp_host: str
    ftp_port: int
    ftp_user: str
    ftp_password: str
    # UI demo accounts (emails fixed by the frontend; passwords come from env)
    users: dict[str, dict[str, str]]
    jwt_ttl_seconds: int = 86400
    ftp_max_concurrency: int = 8  # FTP sessions one backend instance may hold at once

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            jwt_secret=_req("JWT_SECRET"),
            simulator_token=_req("SIMULATOR_TOKEN"),
            database_url=_req("DATABASE_URL"),
            s3_endpoint=_req("S3_ENDPOINT"),
            s3_access_key=_req("S3_ACCESS_KEY"),
            s3_secret_key=_req("S3_SECRET_KEY"),
            s3_bucket=os.environ.get("S3_BUCKET", "filegrid"),
            policies_path=os.environ.get("POLICIES_PATH", "/app/policies.yaml"),
            blob_database_url=_req("BLOB_DATABASE_URL"),
            ftp_host=_req("FTP_HOST"),
            ftp_port=int(os.environ.get("FTP_PORT", "21")),
            ftp_user=_req("FTP_USER"),
            ftp_password=_req("FTP_PASSWORD"),
            ftp_max_concurrency=int(os.environ.get("FTP_MAX_CONCURRENCY", "8")),
            users={
                "dispecer@ppc.ro": {
                    "password": _req("DISPECER_PASSWORD"),
                    "role": "dispecer",
                    "name": "Dispecer Muntenia",
                },
                "admin@ppc.ro": {
                    "password": _req("ADMIN_PASSWORD"),
                    "role": "admin",
                    "name": "Admin FileGrid",
                },
            },
        )
