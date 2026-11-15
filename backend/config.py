"""Runtime configuration, read from environment variables only (no secrets in code)."""

import os
from dataclasses import dataclass, field


def _req(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _build_users() -> dict:
    users = {
        "dispecer@ppc.ro": {"password": _req("DISPECER_PASSWORD"), "role": "dispecer",
                            "name": "Dispecer Muntenia"},
        "admin@ppc.ro": {"password": _req("ADMIN_PASSWORD"), "role": "admin",
                         "name": "Admin FileGrid"},
    }
    auditor = os.environ.get("AUDITOR_PASSWORD", "").strip()
    if auditor:  # optional: reads audit trails, never file content (improvements.md 2.5)
        users["auditor@ppc.ro"] = {"password": auditor, "role": "auditor", "name": "Auditor"}
    return users


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
    prometheus_sample_seconds: float = 10.0          # how often state gauges are refreshed
    prometheus_upload_stall_seconds: float = 300.0   # a chunked upload idle this long is "stalled"

    # ---- slice I (analytics) settings (always with a default): add directly below this line ----
    prometheus_url: str = "http://prometheus:9090"  # slice H; summary still answers if this is down
    analytics_range: str = "5m"                     # PromQL window for the live table
    analytics_timeout_seconds: float = 2.0

    # ---- slice J (scale-out) settings (always with a default): add directly below this line ----

    # background scrubber (improvements.md 2.2): re-verify stored files on its own
    scrub_enabled: bool = True
    scrub_interval_seconds: float = 30.0     # pause between batches
    scrub_batch: int = 5                     # files checked per batch, per worker
    scrub_recheck_seconds: float = 3600.0    # a file is re-checked at most this often
    scrub_max_bytes: int = 64 * 1024 * 1024  # bigger files are skipped by the background job
    scrub_categories: tuple = ("outage_records", "connection_docs", "drone_videos")
    # testing overlay (admin only, off unless enabled): synthetic traffic and one-press demo runs
    testing_enabled: bool = False
    testing_self_url: str = "http://127.0.0.1:8000"
    simulator_url: str = "http://simulator:8080"
    # login lockout with backoff (improvements.md 2.5): after N consecutive failures an account
    # is refused for base * 2^(failures-N) seconds, at most `max`
    login_max_failures: int = 5
    login_lock_base_seconds: float = 30.0
    login_lock_max_seconds: float = 900.0
    # storage canary (improvements.md 2.8): a tiny synthetic round trip on every storage
    canary_enabled: bool = True
    canary_interval_seconds: float = 60.0
    # chaos console (improvements.md 2.1): OFF unless explicitly enabled; admin only, audited
    chaos_enabled: bool = False
    chaos_max_seconds: float = 600.0         # every fault expires by itself, at most this long
    chaos_default_seconds: float = 60.0
    toxiproxy_url: str = "http://toxiproxy:8474"

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
            prometheus_sample_seconds=float(os.environ.get("PROMETHEUS_SAMPLE_SECONDS", "10")),
            prometheus_upload_stall_seconds=float(
                os.environ.get("PROMETHEUS_UPLOAD_STALL_SECONDS", "300")
            ),

            # ---- slice I (analytics) from_env lines: add directly below this line ----
            prometheus_url=os.environ.get("PROMETHEUS_URL", "http://prometheus:9090"),
            analytics_range=os.environ.get("ANALYTICS_RANGE", "5m"),
            analytics_timeout_seconds=float(os.environ.get("ANALYTICS_TIMEOUT_SECONDS", "2")),

            # ---- slice J (scale-out) from_env lines: add directly below this line ----
            scrub_enabled=os.environ.get("SCRUB_ENABLED", "true").strip().lower() not in ("0", "false", "no"),
            scrub_interval_seconds=float(os.environ.get("SCRUB_INTERVAL_SECONDS", "30")),
            scrub_batch=int(os.environ.get("SCRUB_BATCH", "5")),
            scrub_recheck_seconds=float(os.environ.get("SCRUB_RECHECK_SECONDS", "3600")),
            scrub_max_bytes=int(os.environ.get("SCRUB_MAX_BYTES", str(64 * 1024 * 1024))),
            testing_enabled=os.environ.get("TESTING_ENABLED", "false").strip().lower() in ("1", "true", "yes"),
            testing_self_url=os.environ.get("TESTING_SELF_URL", "http://127.0.0.1:8000"),
            simulator_url=os.environ.get("SIMULATOR_URL", "http://simulator:8080"),
            login_max_failures=int(os.environ.get("LOGIN_MAX_FAILURES", "5")),
            login_lock_base_seconds=float(os.environ.get("LOGIN_LOCK_BASE_SECONDS", "30")),
            login_lock_max_seconds=float(os.environ.get("LOGIN_LOCK_MAX_SECONDS", "900")),
            canary_enabled=os.environ.get("CANARY_ENABLED", "true").strip().lower() not in ("0", "false", "no"),
            canary_interval_seconds=float(os.environ.get("CANARY_INTERVAL_SECONDS", "60")),
            chaos_enabled=os.environ.get("CHAOS_ENABLED", "false").strip().lower() in ("1", "true", "yes"),
            chaos_max_seconds=float(os.environ.get("CHAOS_MAX_SECONDS", "600")),
            chaos_default_seconds=float(os.environ.get("CHAOS_DEFAULT_SECONDS", "60")),
            toxiproxy_url=os.environ.get("TOXIPROXY_URL", "http://toxiproxy:8474"),
            scrub_categories=tuple(
                c.strip() for c in os.environ.get(
                    "SCRUB_CATEGORIES", "outage_records,connection_docs,drone_videos"
                ).split(",") if c.strip()
            ),
            users=_build_users(),
        )
