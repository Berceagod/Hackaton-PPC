"""Runtime configuration, read from environment variables only (no secrets in code)."""

import os
from dataclasses import dataclass, field


def _req(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def parse_master_key(value: str) -> bytes:
    """FILEGRID_MASTER_KEY: 64 hex characters = the 32-byte AES-256 master key (ADR-0011)."""
    try:
        key = bytes.fromhex(value.strip())
    except ValueError:
        raise RuntimeError("FILEGRID_MASTER_KEY must be hex") from None
    if len(key) != 32:
        raise RuntimeError("FILEGRID_MASTER_KEY must be 32 bytes (64 hex characters)")
    return key


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
    # AES-256 master key for data at rest; never stored next to the data (ADR-0011)
    master_key: bytes = field(default=b"", repr=False)
    # slice G: isolation and resilience (all tunable without code changes)
    db_pool: int = 6                       # metadata pool per worker process
    meter_db_pool: int = 4                 # own connection pool for meter readings
    bulkhead_limits: dict = field(default_factory=lambda: {"high": 64, "normal": 32, "low": 8})
    bulkhead_wait_seconds: float = 10.0
    breaker_threshold: int = 3             # consecutive storage failures before failing fast
    breaker_cooldown_seconds: float = 5.0
    upload_ttl_hours: float = 24.0         # abandoned chunked uploads are purged after this

    # ---- slice H (observability) settings (always with a default): add directly below this line ----

    # ---- slice I (analytics) settings (always with a default): add directly below this line ----

    # ---- slice J (scale-out) settings (always with a default): add directly below this line ----

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
            master_key=parse_master_key(_req("FILEGRID_MASTER_KEY")),
            db_pool=int(os.environ.get("DB_POOL", "6")),
            meter_db_pool=int(os.environ.get("METER_DB_POOL", "4")),
            bulkhead_limits={
                "high": int(os.environ.get("BULKHEAD_HIGH", "64")),
                "normal": int(os.environ.get("BULKHEAD_NORMAL", "32")),
                "low": int(os.environ.get("BULKHEAD_LOW", "8")),
            },
            bulkhead_wait_seconds=float(os.environ.get("BULKHEAD_WAIT_SECONDS", "10")),
            breaker_threshold=int(os.environ.get("BREAKER_THRESHOLD", "3")),
            breaker_cooldown_seconds=float(os.environ.get("BREAKER_COOLDOWN_SECONDS", "5")),
            upload_ttl_hours=float(os.environ.get("UPLOAD_TTL_HOURS", "24")),

            # ---- slice H (observability) from_env lines: add directly below this line ----

            # ---- slice I (analytics) from_env lines: add directly below this line ----

            # ---- slice J (scale-out) from_env lines: add directly below this line ----
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
