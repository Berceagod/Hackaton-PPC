"""Metadata repository (PostgreSQL via asyncpg). Tables are created idempotently at startup."""

import asyncio
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Protocol

import asyncpg

import audit_chain

SCHEMA = """
CREATE TABLE IF NOT EXISTS files_metadata (
    id           UUID PRIMARY KEY,
    filename     TEXT        NOT NULL,
    category     TEXT        NOT NULL,
    storage      TEXT        NOT NULL,
    storage_key  TEXT        NOT NULL,
    size_bytes   BIGINT      NOT NULL,
    sha256       CHAR(64)    NOT NULL,
    encrypted    BOOLEAN     NOT NULL DEFAULT FALSE,
    trace_id     TEXT        NOT NULL,
    zone         TEXT,
    station      TEXT,
    description  TEXT,
    uploaded_by  TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_files_category ON files_metadata (category, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_files_storage  ON files_metadata (storage);

CREATE TABLE IF NOT EXISTS audit_logs (
    id         UUID PRIMARY KEY,
    file_id    UUID        NOT NULL,
    action     TEXT        NOT NULL,
    username   TEXT        NOT NULL,
    trace_id   TEXT        NOT NULL,
    details    TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_audit_file ON audit_logs (file_id, created_at DESC);

CREATE TABLE IF NOT EXISTS meter_readings (
    id          BIGSERIAL PRIMARY KEY,
    meter_id    TEXT        NOT NULL,
    reading_ts  TIMESTAMPTZ NOT NULL,
    zone        TEXT,
    voltage_v   DOUBLE PRECISION,
    sim_label   TEXT,
    payload     JSONB,
    file_id     UUID,
    trace_id    TEXT,
    status      TEXT        NOT NULL DEFAULT 'pending',   -- pending | stored
    claimed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (meter_id, reading_ts)                          -- the idempotency guarantee
);
-- databases created before slice C
ALTER TABLE meter_readings ADD COLUMN IF NOT EXISTS status     TEXT        NOT NULL DEFAULT 'pending';
ALTER TABLE meter_readings ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ NOT NULL DEFAULT now();
-- slice G outbox: a reading whose storage is down is kept here (exact bytes) as 'queued'
-- and written out by the drainer; raw is cleared once stored (ADR-0013)
ALTER TABLE meter_readings ADD COLUMN IF NOT EXISTS raw         BYTEA;
ALTER TABLE meter_readings ADD COLUMN IF NOT EXISTS filename    TEXT;
ALTER TABLE meter_readings ADD COLUMN IF NOT EXISTS uploaded_by TEXT;
CREATE INDEX IF NOT EXISTS idx_meter_queued ON meter_readings (claimed_at) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS idx_files_cat_sha ON files_metadata (category, sha256);

-- slice E: chunked, resumable uploads (ADR-0010)
CREATE TABLE IF NOT EXISTS upload_sessions (
    id           UUID PRIMARY KEY,                     -- client-chosen upload_id
    owner        TEXT        NOT NULL,
    category     TEXT        NOT NULL,
    filename     TEXT        NOT NULL,
    total_chunks INT         NOT NULL,
    size_bytes   BIGINT,                               -- declared by the client (optional)
    sha256       CHAR(64),                             -- declared by the client (optional)
    metadata     JSONB       NOT NULL DEFAULT '{}',
    trace_id     TEXT        NOT NULL,
    status       TEXT        NOT NULL DEFAULT 'open',  -- open | assembling | complete | failed
    encrypted    BOOLEAN     NOT NULL DEFAULT FALSE,   -- staged chunks encrypted (slice F)
    file_id      UUID,
    reason       TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE upload_sessions ADD COLUMN IF NOT EXISTS encrypted BOOLEAN NOT NULL DEFAULT FALSE;
CREATE TABLE IF NOT EXISTS upload_chunks (
    upload_id   UUID        NOT NULL REFERENCES upload_sessions (id) ON DELETE CASCADE,
    idx         INT         NOT NULL,
    size_bytes  BIGINT      NOT NULL,
    sha256      CHAR(64)    NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (upload_id, idx)                       -- a resent chunk replaces itself
);

CREATE TABLE IF NOT EXISTS policies_config (
    category    TEXT PRIMARY KEY,
    storage     TEXT        NOT NULL,
    encryption  BOOLEAN     NOT NULL,
    deduplicate BOOLEAN     NOT NULL,
    priority    TEXT        NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- tamper-evident audit chain (improvements.md 2.3): seq orders the chain; rows written
-- before this change have NULL hashes and are simply not part of it
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS seq       BIGSERIAL;
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS prev_hash CHAR(64);
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS row_hash  CHAR(64);
CREATE INDEX IF NOT EXISTS idx_audit_seq ON audit_logs (seq);

-- background scrubber (improvements.md 2.2): when each file was last re-verified
ALTER TABLE files_metadata ADD COLUMN IF NOT EXISTS verified_at   TIMESTAMPTZ;
ALTER TABLE files_metadata ADD COLUMN IF NOT EXISTS verify_status TEXT;  -- checking | ok | failed
CREATE INDEX IF NOT EXISTS idx_files_scrub ON files_metadata (category, verified_at NULLS FIRST);

-- per-file random keys, stored only wrapped by the master key (improvements.md 2.5). A row with
-- wrapped = NULL and status 'erased' is a tombstone: the file's key was destroyed on purpose.
CREATE TABLE IF NOT EXISTS file_keys (
    file_id    UUID PRIMARY KEY,
    wrapped    BYTEA,
    status     TEXT        NOT NULL DEFAULT 'active',   -- active | erased
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    erased_at  TIMESTAMPTZ,
    erased_by  TEXT
);

-- failed logins per account, for lockout with backoff (improvements.md 2.5)
CREATE TABLE IF NOT EXISTS login_attempts (
    email        TEXT PRIMARY KEY,
    failures     INTEGER     NOT NULL,
    last_failure TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_attempts_last ON login_attempts (last_failure);

-- testing overlay: small shared key/value state (generator on/off, running job) and a lease so
-- only one worker generates traffic
CREATE TABLE IF NOT EXISTS testing_state (
    key        TEXT PRIMARY KEY,
    value      TEXT        NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS testing_lease (
    name       TEXT PRIMARY KEY,
    holder     TEXT        NOT NULL,
    until      TIMESTAMPTZ NOT NULL
);

-- chaos console (improvements.md 2.1): injected faults, shared by every replica, self-expiring
CREATE TABLE IF NOT EXISTS chaos_faults (
    target     TEXT PRIMARY KEY,            -- s3 | ftp | postgres | network
    kind       TEXT        NOT NULL,        -- down | slow | bad
    seconds    DOUBLE PRECISION NOT NULL DEFAULT 0,
    expires_at TIMESTAMPTZ NOT NULL,
    created_by TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- second copy of a file on another storage (improvements.md 2.2): ok | pending (not written
-- yet or last write failed; the scrubber completes it) | failed (copy found damaged, repair failed)
CREATE TABLE IF NOT EXISTS file_replicas (
    file_id     UUID PRIMARY KEY,
    storage     TEXT        NOT NULL,
    storage_key TEXT        NOT NULL,
    status      TEXT        NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

AUDIT_LOCK = 7461003  # serialises appends so the chain has one order


@dataclass
class FileRecord:
    id: str
    filename: str
    category: str
    storage: str
    storage_key: str
    size_bytes: int
    sha256: str
    encrypted: bool
    trace_id: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    zone: str | None = None
    station: str | None = None
    description: str | None = None
    uploaded_by: str | None = None

    def to_api(self) -> dict:
        """Exact shape of frontend FileRecord (storage_key is internal)."""
        data = asdict(self)
        data.pop("storage_key")
        data["created_at"] = self.created_at.isoformat()
        return {k: v for k, v in data.items() if v is not None}


class Repo(Protocol):
    async def init_schema(self) -> None: ...
    async def insert_file(self, rec: FileRecord) -> None: ...
    async def get_file(self, file_id: str) -> FileRecord | None: ...
    async def list_files(
        self,
        category: str | None,
        storage: str | None,
        q: str | None,
        limit: int,
        offset: int = 0,
        exclude_category: str | None = None,
    ) -> list[FileRecord]: ...
    async def add_audit(
        self, file_id: str, action: str, user: str, trace_id: str, details: str | None
    ) -> None: ...
    async def list_audit(self, file_id: str) -> list[dict]: ...
    async def load_policies(self) -> dict | None: ...
    async def save_policies(self, policies: dict) -> None: ...
    async def find_by_sha(self, category: str, sha256: str) -> FileRecord | None: ...
    async def claim_reading(
        self, claim: dict, stale_after: float
    ) -> tuple[str, str | None]:
        """Returns (state, file_id); state is new | takeover | duplicate | in_flight."""

    async def complete_reading(
        self, meter_id: str, reading_ts: datetime, rec: FileRecord
    ) -> None: ...
    async def release_reading(
        self, meter_id: str, reading_ts: datetime, file_id: str
    ) -> None: ...
    async def healthy(self) -> bool: ...
    # chunked uploads (slice E)
    async def open_upload(self, session: dict) -> dict: ...
    async def get_upload(self, upload_id: str) -> dict | None: ...
    async def record_chunk(self, upload_id: str, index: int, size: int, sha256: str) -> int: ...
    async def received_chunks(self, upload_id: str) -> list[int]: ...
    async def start_assembly(self, upload_id: str, stale_after: float) -> bool: ...
    async def complete_upload(self, upload_id: str, rec: FileRecord) -> None: ...
    async def reopen_upload(self, upload_id: str) -> None: ...
    async def fail_upload(self, upload_id: str, reason: str) -> None: ...
    # meter outbox + stale uploads (slice G)
    async def queue_reading(self, meter_id, reading_ts, raw, filename, uploaded_by) -> bool: ...
    async def claim_queued(self, limit: int, retry_after: float) -> list[dict]: ...
    async def count_queued(self) -> int: ...
    async def list_stale_uploads(self, older_than_seconds: float) -> list[tuple[str, int]]: ...
    async def delete_upload(self, upload_id: str) -> None: ...

    # ---- slice H (observability) Repo protocol methods: add directly below this line ----
    async def count_unencrypted_pii(self) -> int: ...
    async def upload_stall_stats(self, stall_seconds: float) -> tuple[int, int]: ...

    # ---- slice I (analytics) Repo protocol methods: add directly below this line ----
    async def ensure_analytics_schema(self) -> None: ...
    async def save_measurements(self, kind: str, scenario: str | None, rows: list[dict]) -> None: ...
    async def latest_measurements(self) -> list[dict]: ...
    async def analytics_snapshot(self) -> dict: ...

    # ---- slice J (scale-out) Repo protocol methods: add directly below this line ----

    # audit chain and scrubber (improvements.md)
    async def verify_audit_chain(self) -> dict: ...
    async def claim_for_scrub(
        self, categories: list[str], max_bytes: int, recheck_seconds: float, limit: int
    ) -> "list[FileRecord]": ...
    async def finish_scrub(self, file_id: str, status: str | None) -> None: ...
    async def scrub_stats(self, categories: list[str]) -> dict: ...
    async def set_replica(self, file_id: str, storage: str, storage_key: str, status: str) -> None: ...
    async def get_replica(self, file_id: str) -> dict | None: ...
    async def replica_stats(self) -> dict: ...
    async def set_testing(self, key: str, value: dict, duration: float) -> None: ...
    async def get_testing(self, key: str) -> dict | None: ...
    async def clear_testing(self, key: str) -> None: ...
    async def try_lease(self, name: str, holder: str, seconds: float) -> bool: ...
    async def put_file_key(self, file_id: str, wrapped: bytes) -> None: ...
    async def get_file_key(self, file_id: str) -> dict | None: ...
    async def erase_file_key(self, file_id: str, by: str) -> bool: ...
    async def list_file_keys(self, after: str | None, limit: int) -> list[dict]: ...
    async def replace_file_key(self, file_id: str, wrapped: bytes) -> None: ...
    async def login_state(self, email: str) -> tuple[int, float | None]: ...
    async def login_failed(self, email: str) -> int: ...
    async def login_ok(self, email: str) -> None: ...
    async def set_fault(self, target: str, kind: str, seconds: float, duration: float, by: str) -> None: ...
    async def clear_faults(self, target: str | None = None) -> int: ...
    async def active_faults(self) -> list[dict]: ...


_COLS = (
    "id, filename, category, storage, storage_key, size_bytes, sha256, encrypted, "
    "trace_id, zone, station, description, uploaded_by, created_at"
)
_INSERT_FILE = (
    f"INSERT INTO files_metadata ({_COLS}) "
    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)"
)


def _file_args(rec: FileRecord) -> tuple:
    return (
        uuid.UUID(rec.id),
        rec.filename,
        rec.category,
        rec.storage,
        rec.storage_key,
        rec.size_bytes,
        rec.sha256,
        rec.encrypted,
        rec.trace_id,
        rec.zone,
        rec.station,
        rec.description,
        rec.uploaded_by,
        rec.created_at,
    )


def _like_literal(text: str) -> str:
    """Search text is a literal: escape the LIKE wildcards (backslash is the default escape)."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _row_to_record(row: asyncpg.Record) -> FileRecord:
    d = dict(row)
    d["id"] = str(d["id"])
    d["sha256"] = d["sha256"].strip()
    return FileRecord(**d)


class PostgresRepo:
    POLICY_CHANNEL = "filegrid_policies"

    def __init__(self, pool: asyncpg.Pool, dsn: str | None = None) -> None:
        self._pool = pool
        self._dsn = dsn
        self._listener: asyncpg.Connection | None = None

    @classmethod
    async def connect(cls, dsn: str, max_size: int = 10) -> "PostgresRepo":
        pool = None
        last: Exception | None = None
        for _ in range(30):  # postgres may still be starting
            try:
                pool = await asyncpg.create_pool(dsn, min_size=1, max_size=max_size)
                break
            except (OSError, asyncpg.PostgresError) as exc:
                last = exc
                await asyncio.sleep(2)
        if pool is None:
            raise RuntimeError(f"Postgres not reachable: {last}")
        return cls(pool, dsn)

    async def close(self) -> None:
        await self._pool.close()

    async def init_schema(self) -> None:
        async with self._pool.acquire() as con:
            # advisory lock: several backend replicas may start at once
            await con.execute("SELECT pg_advisory_lock(7461001)")
            try:
                await con.execute(SCHEMA)
            finally:
                await con.execute("SELECT pg_advisory_unlock(7461001)")

    async def insert_file(self, rec: FileRecord) -> None:
        async with self._pool.acquire() as con:
            await con.execute(_INSERT_FILE, *_file_args(rec))

    async def find_by_sha(self, category: str, sha256: str) -> FileRecord | None:
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                f"SELECT {_COLS} FROM files_metadata WHERE category = $1 AND sha256 = $2 "
                "ORDER BY created_at LIMIT 1",
                category, sha256,
            )
        return _row_to_record(row) if row else None

    # ---- meter readings: claim -> store -> complete (see meter_ingest.py) ----

    async def claim_reading(self, claim: dict, stale_after: float) -> tuple[str, str | None]:
        async with self._pool.acquire() as con:
            # The UNIQUE (meter_id, reading_ts) constraint decides who owns the reading.
            row = await con.fetchrow(
                "INSERT INTO meter_readings (meter_id, reading_ts, zone, voltage_v, sim_label, "
                "payload, file_id, trace_id, status, claimed_at) "
                "VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8,'pending',now()) "
                "ON CONFLICT (meter_id, reading_ts) DO NOTHING RETURNING file_id",
                claim["meter_id"], claim["reading_ts"], claim["zone"], claim["voltage_v"],
                claim["sim_label"], claim["payload"], uuid.UUID(claim["file_id"]), claim["trace_id"],
            )
            if row:
                return "new", str(row["file_id"])
            # A claim left 'pending' by a crashed/failed request: adopt it.
            row = await con.fetchrow(
                "UPDATE meter_readings SET claimed_at = now(), trace_id = $3 "
                "WHERE meter_id = $1 AND reading_ts = $2 AND status = 'pending' "
                "AND claimed_at < now() - make_interval(secs => $4) RETURNING file_id",
                claim["meter_id"], claim["reading_ts"], claim["trace_id"], float(stale_after),
            )
            if row:
                return "takeover", str(row["file_id"])
            row = await con.fetchrow(
                "SELECT status, file_id FROM meter_readings WHERE meter_id = $1 AND reading_ts = $2",
                claim["meter_id"], claim["reading_ts"],
            )
        if row is None:  # released between our statements; sender retries
            return "in_flight", None
        # 'queued' is durably accepted (outbox, slice G): a resend is a duplicate too
        return (("duplicate" if row["status"] in ("stored", "queued") else "in_flight"),
                str(row["file_id"]))

    async def complete_reading(self, meter_id, reading_ts, rec: FileRecord) -> None:
        async with self._pool.acquire() as con:
            async with con.transaction():
                await con.execute(
                    _INSERT_FILE + " ON CONFLICT (id) DO UPDATE SET storage = EXCLUDED.storage, "
                    "storage_key = EXCLUDED.storage_key, sha256 = EXCLUDED.sha256, "
                    "size_bytes = EXCLUDED.size_bytes, trace_id = EXCLUDED.trace_id",
                    *_file_args(rec),
                )
                await con.execute(
                    "UPDATE meter_readings SET status = 'stored', raw = NULL "
                    "WHERE meter_id = $1 AND reading_ts = $2",
                    meter_id, reading_ts,
                )

    async def release_reading(self, meter_id, reading_ts, file_id: str) -> None:
        async with self._pool.acquire() as con:
            await con.execute(
                "DELETE FROM meter_readings WHERE meter_id = $1 AND reading_ts = $2 "
                "AND status = 'pending' AND file_id = $3",
                meter_id, reading_ts, uuid.UUID(file_id),
            )

    # ---- chunked uploads (see chunked_upload.py) ----

    async def open_upload(self, s: dict) -> dict:
        async with self._pool.acquire() as con:
            await con.execute(
                "INSERT INTO upload_sessions (id, owner, category, filename, total_chunks, "
                "size_bytes, sha256, metadata, trace_id, encrypted) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10) ON CONFLICT (id) DO NOTHING",
                uuid.UUID(s["id"]), s["owner"], s["category"], s["filename"], s["total_chunks"],
                s["size_bytes"], s["sha256"], json.dumps(s["metadata"]), s["trace_id"],
                bool(s.get("encrypted")),
            )
        return await self.get_upload(s["id"])

    async def get_upload(self, upload_id: str) -> dict | None:
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT id, owner, category, filename, total_chunks, size_bytes, sha256, "
                "metadata, trace_id, status, file_id, reason, encrypted "
                "FROM upload_sessions WHERE id = $1",
                uuid.UUID(upload_id),
            )
        if row is None:
            return None
        d = dict(row)
        d["id"] = str(d["id"])
        d["file_id"] = str(d["file_id"]) if d["file_id"] else None
        if isinstance(d["metadata"], str):
            d["metadata"] = json.loads(d["metadata"])
        return d

    async def record_chunk(self, upload_id: str, index: int, size: int, sha256: str) -> int:
        uid = uuid.UUID(upload_id)
        async with self._pool.acquire() as con:
            await con.execute(
                "INSERT INTO upload_chunks (upload_id, idx, size_bytes, sha256) "
                "VALUES ($1,$2,$3,$4) ON CONFLICT (upload_id, idx) DO UPDATE SET "
                "size_bytes = EXCLUDED.size_bytes, sha256 = EXCLUDED.sha256, received_at = now()",
                uid, index, size, sha256,
            )
            # activity keeps a slow upload from being purged as abandoned (slice G)
            await con.execute(
                "UPDATE upload_sessions SET updated_at = now() WHERE id = $1", uid
            )
            return await con.fetchval(
                "SELECT count(*) FROM upload_chunks WHERE upload_id = $1", uid
            )

    async def received_chunks(self, upload_id: str) -> list[int]:
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                "SELECT idx FROM upload_chunks WHERE upload_id = $1 ORDER BY idx",
                uuid.UUID(upload_id),
            )
        return [r["idx"] for r in rows]

    async def start_assembly(self, upload_id: str, stale_after: float) -> bool:
        """Only one request may assemble an upload (the status change is the lock)."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                "UPDATE upload_sessions SET status = 'assembling', updated_at = now() "
                "WHERE id = $1 AND (status = 'open' OR (status = 'assembling' "
                "AND updated_at < now() - make_interval(secs => $2))) RETURNING id",
                uuid.UUID(upload_id), float(stale_after),
            )
        return row is not None

    async def complete_upload(self, upload_id: str, rec: FileRecord) -> None:
        async with self._pool.acquire() as con:
            async with con.transaction():
                await con.execute(_INSERT_FILE, *_file_args(rec))
                await con.execute(
                    "UPDATE upload_sessions SET status = 'complete', file_id = $2, "
                    "updated_at = now() WHERE id = $1",
                    uuid.UUID(upload_id), uuid.UUID(rec.id),
                )

    async def reopen_upload(self, upload_id: str) -> None:
        async with self._pool.acquire() as con:
            await con.execute(
                "UPDATE upload_sessions SET status = 'open', updated_at = now() "
                "WHERE id = $1 AND status = 'assembling'",
                uuid.UUID(upload_id),
            )

    async def fail_upload(self, upload_id: str, reason: str) -> None:
        async with self._pool.acquire() as con:
            await con.execute(
                "UPDATE upload_sessions SET status = 'failed', reason = $2, updated_at = now() "
                "WHERE id = $1",
                uuid.UUID(upload_id), reason,
            )

    # ---- meter outbox (slice G, see meter_outbox.py) ----

    async def queue_reading(self, meter_id, reading_ts, raw: bytes, filename: str,
                            uploaded_by: str) -> bool:
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                "UPDATE meter_readings SET status = 'queued', raw = $3, filename = $4, "
                "uploaded_by = $5, claimed_at = now() "
                "WHERE meter_id = $1 AND reading_ts = $2 AND status = 'pending' RETURNING id",
                meter_id, reading_ts, raw, filename, uploaded_by,
            )
        return row is not None

    async def claim_queued(self, limit: int, retry_after: float) -> list[dict]:
        """Take a batch of queued readings not tried in the last `retry_after` seconds.
        SKIP LOCKED + the claimed_at bump let several backend replicas drain in parallel
        without taking the same reading."""
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                "UPDATE meter_readings SET claimed_at = now() WHERE id IN ("
                "  SELECT id FROM meter_readings WHERE status = 'queued' "
                "  AND claimed_at <= now() - make_interval(secs => $2) "
                "  ORDER BY claimed_at LIMIT $1 FOR UPDATE SKIP LOCKED) "
                "RETURNING meter_id, reading_ts, zone, file_id, trace_id, raw, filename, uploaded_by",
                limit, float(retry_after),
            )
        return [{**dict(r), "file_id": str(r["file_id"])} for r in rows]

    async def count_queued(self) -> int:
        async with self._pool.acquire() as con:
            return await con.fetchval("SELECT count(*) FROM meter_readings WHERE status = 'queued'")

    # ---- stale chunked uploads (slice G) ----

    async def list_stale_uploads(self, older_than_seconds: float) -> list[tuple[str, int]]:
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                "SELECT id, total_chunks FROM upload_sessions WHERE status IN ('open', 'failed') "
                "AND updated_at < now() - make_interval(secs => $1)",
                float(older_than_seconds),
            )
        return [(str(r["id"]), r["total_chunks"]) for r in rows]

    async def delete_upload(self, upload_id: str) -> None:
        async with self._pool.acquire() as con:
            await con.execute("DELETE FROM upload_sessions WHERE id = $1", uuid.UUID(upload_id))

    async def get_file(self, file_id: str) -> FileRecord | None:
        try:
            fid = uuid.UUID(file_id)
        except ValueError:
            return None
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                f"SELECT {_COLS} FROM files_metadata WHERE id = $1", fid
            )
        return _row_to_record(row) if row else None

    async def list_files(
        self, category, storage, q, limit=500, offset=0, exclude_category=None
    ) -> list[FileRecord]:
        clauses, args = [], []
        if category:
            args.append(category)
            clauses.append(f"category = ${len(args)}")
        if exclude_category:
            args.append(exclude_category)
            clauses.append(f"category <> ${len(args)}")
        if storage:
            args.append(storage)
            clauses.append(f"storage = ${len(args)}")
        if q:
            args.append(f"%{_like_literal(q)}%")
            n = len(args)
            clauses.append(
                f"(filename ILIKE ${n} OR trace_id ILIKE ${n} OR zone ILIKE ${n})"
            )
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        args += [limit, offset]
        sql = (
            f"SELECT {_COLS} FROM files_metadata {where} "
            f"ORDER BY created_at DESC, id LIMIT ${len(args) - 1} OFFSET ${len(args)}"
        )
        async with self._pool.acquire() as con:
            rows = await con.fetch(sql, *args)
        return [_row_to_record(r) for r in rows]

    async def add_audit(self, file_id, action, user, trace_id, details) -> None:
        """Append to the hash chain. One writer at a time (advisory lock held until commit),
        so `seq` order, `prev_hash` and the insert can never interleave."""
        row = {
            "id": uuid.uuid4(),
            "file_id": uuid.UUID(file_id),
            "action": action,
            "username": user,
            "trace_id": trace_id,
            "details": details,
            "created_at": audit_chain.now_utc(),
        }
        async with self._pool.acquire() as con:
            async with con.transaction():
                await con.execute("SELECT pg_advisory_xact_lock($1)", AUDIT_LOCK)
                prev = await con.fetchval(
                    "SELECT row_hash FROM audit_logs WHERE row_hash IS NOT NULL "
                    "ORDER BY seq DESC LIMIT 1"
                )
                prev = (prev or audit_chain.GENESIS).strip()
                await con.execute(
                    "INSERT INTO audit_logs (id, file_id, action, username, trace_id, details, "
                    "created_at, prev_hash, row_hash) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                    row["id"], row["file_id"], action, user, trace_id, details,
                    row["created_at"], prev, audit_chain.row_hash(prev, row),
                )

    async def verify_audit_chain(self) -> dict:
        verifier = audit_chain.ChainVerifier()
        async with self._pool.acquire() as con:
            async with con.transaction():  # a cursor needs a transaction; rows stream, no big list
                async for r in con.cursor(
                    "SELECT seq, id, file_id, action, username, trace_id, details, created_at, "
                    "prev_hash, row_hash FROM audit_logs WHERE row_hash IS NOT NULL ORDER BY seq"
                ):
                    verifier.feed(dict(r))
        return verifier.result()

    async def list_audit(self, file_id: str) -> list[dict]:
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                "SELECT id, file_id, action, username, created_at, details "
                "FROM audit_logs WHERE file_id = $1 ORDER BY created_at DESC",
                uuid.UUID(file_id),
            )
        return [
            {
                "id": str(r["id"]),
                "file_id": str(r["file_id"]),
                "action": r["action"],
                "user": r["username"],
                "timestamp": r["created_at"].isoformat(),
                "details": r["details"],
            }
            for r in rows
        ]

    async def load_policies(self) -> dict | None:
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                "SELECT category, storage, encryption, deduplicate, priority FROM policies_config"
            )
        if not rows:
            return None
        return {
            r["category"]: {
                "storage": r["storage"],
                "encryption": r["encryption"],
                "deduplicate": r["deduplicate"],
                "priority": r["priority"],
            }
            for r in rows
        }

    async def save_policies(self, policies: dict) -> None:
        async with self._pool.acquire() as con:
            async with con.transaction():
                for name, p in policies.items():
                    await con.execute(
                        "INSERT INTO policies_config (category, storage, encryption, deduplicate, priority) "
                        "VALUES ($1,$2,$3,$4,$5) "
                        "ON CONFLICT (category) DO UPDATE SET storage=EXCLUDED.storage, "
                        "encryption=EXCLUDED.encryption, deduplicate=EXCLUDED.deduplicate, "
                        "priority=EXCLUDED.priority, updated_at=now()",
                        name, p["storage"], p["encryption"], p["deduplicate"], p["priority"],
                    )
                # delivered on commit to every worker and replica listening (slice G)
                await con.execute(f"NOTIFY {self.POLICY_CHANNEL}")

    async def listen_policies(self, on_change) -> None:
        """Call on_change() whenever any process saves policies. Uses one dedicated
        connection (LISTEN cannot share a pooled connection)."""
        self._listener = await asyncpg.connect(self._dsn)
        await self._listener.add_listener(self.POLICY_CHANNEL, lambda *_: on_change())

    async def healthy(self) -> bool:
        try:
            async with self._pool.acquire() as con:
                await con.fetchval("SELECT 1")
            return True
        except Exception:
            return False

    # ---- slice H (observability) PostgresRepo methods: add directly below this line ----
    async def count_unencrypted_pii(self) -> int:
        """connection_docs files stored without encryption: personal data at risk (alert)."""
        async with self._pool.acquire() as con:
            return int(
                await con.fetchval(
                    "SELECT count(*) FROM files_metadata "
                    "WHERE category = 'connection_docs' AND NOT encrypted"
                )
            )

    async def upload_stall_stats(self, stall_seconds: float) -> tuple[int, int]:
        """(in progress, stalled): chunked uploads still open or assembling, and those with
        no chunk received for longer than `stall_seconds` (updated_at moves with every chunk)."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT count(*) AS in_progress, "
                "count(*) FILTER (WHERE updated_at < now() - make_interval(secs => $1)) AS stalled "
                "FROM upload_sessions WHERE status IN ('open', 'assembling')",
                float(stall_seconds),
            )
        return int(row["in_progress"]), int(row["stalled"])

    # ---- slice I (analytics) PostgresRepo methods: add directly below this line ----

    async def ensure_analytics_schema(self) -> None:
        async with self._pool.acquire() as con:
            await con.execute(
                "CREATE TABLE IF NOT EXISTS analytics_measurements ("
                " id BIGSERIAL PRIMARY KEY,"
                " run_id UUID NOT NULL,"
                " kind TEXT NOT NULL,"
                " scenario TEXT,"
                " category TEXT NOT NULL,"
                " storage TEXT NOT NULL,"
                " samples INT NOT NULL,"
                " ok INT NOT NULL,"
                " errors INT NOT NULL,"
                " latency_avg_ms DOUBLE PRECISION NOT NULL,"
                " latency_p50_ms DOUBLE PRECISION NOT NULL,"
                " latency_p95_ms DOUBLE PRECISION NOT NULL,"
                " latency_p99_ms DOUBLE PRECISION NOT NULL,"
                " throughput_mbps DOUBLE PRECISION NOT NULL,"
                " bytes_total BIGINT NOT NULL,"
                " created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            await con.execute(
                "CREATE INDEX IF NOT EXISTS idx_analytics_measurements_latest "
                "ON analytics_measurements (kind, scenario, category, storage, created_at DESC)"
            )

    async def save_measurements(self, kind, scenario, rows) -> None:
        import uuid as _uuid

        run_id = _uuid.uuid4()
        async with self._pool.acquire() as con:
            async with con.transaction():
                for row in rows:
                    await con.execute(
                        "INSERT INTO analytics_measurements (run_id, kind, scenario, category, "
                        "storage, samples, ok, errors, latency_avg_ms, latency_p50_ms, "
                        "latency_p95_ms, latency_p99_ms, throughput_mbps, bytes_total) "
                        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)",
                        run_id, kind, scenario, row["category"], row["storage"],
                        row["samples"], row["ok"], row["errors"],
                        row["latency_avg_ms"], row["latency_p50_ms"],
                        row["latency_p95_ms"], row["latency_p99_ms"],
                        row["throughput_mbps"], row["bytes_total"],
                    )

    async def latest_measurements(self) -> list[dict]:
        async with self._pool.acquire() as con:
            found = await con.fetch(
                "SELECT DISTINCT ON (kind, COALESCE(scenario, ''), category, storage) "
                "kind, scenario, category, storage, samples, ok, errors, "
                "latency_avg_ms, latency_p50_ms, latency_p95_ms, latency_p99_ms, "
                "throughput_mbps, bytes_total "
                "FROM analytics_measurements "
                "ORDER BY kind, COALESCE(scenario, ''), category, storage, created_at DESC"
            )
        return [
            {
                "kind": row["kind"],
                "scenario": row["scenario"],
                "category": row["category"],
                "storage": row["storage"],
                "samples": int(row["samples"]),
                "ok": int(row["ok"]),
                "errors": int(row["errors"]),
                "latency_avg_ms": float(row["latency_avg_ms"]),
                "latency_p50_ms": float(row["latency_p50_ms"]),
                "latency_p95_ms": float(row["latency_p95_ms"]),
                "latency_p99_ms": float(row["latency_p99_ms"]),
                "throughput_mbps": float(row["throughput_mbps"]),
                "bytes_total": int(row["bytes_total"]),
            }
            for row in found
        ]

    async def analytics_snapshot(self) -> dict:
        # Thresholds match analytics.classify_reading: 50 / 207 / 253 V and 3.5x median power.
        from analytics import POWER_KW_PATTERN, ZONES, new_snapshot

        snap = new_snapshot()
        anomaly_sql = """
            WITH base AS (
                SELECT
                    meter_id,
                    COALESCE(zone, '') AS zone,
                    NULLIF(sim_label, '') AS sim_label,
                    voltage_v,
                    CASE
                        WHEN jsonb_typeof(payload -> 'events') = 'array' THEN payload -> 'events'
                        ELSE '[]'::jsonb
                    END AS events,
                    CASE
                        WHEN (payload #>> '{instant,active_power_kw}') ~ 'POWER_KW_PATTERN'
                        THEN (payload #>> '{instant,active_power_kw}')::double precision
                        ELSE NULL
                    END AS power_kw
                FROM meter_readings
                WHERE status IN ('stored', 'queued')
            ),
            med AS (
                SELECT meter_id,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY power_kw) AS med_power
                FROM base
                WHERE power_kw IS NOT NULL AND power_kw > 0
                GROUP BY meter_id
            ),
            marked AS (
                SELECT
                    b.zone,
                    b.sim_label,
                    CASE
                        WHEN b.voltage_v IS NOT NULL AND b.voltage_v < 50 THEN 'outage'
                        WHEN b.voltage_v IS NOT NULL AND b.voltage_v < 207 THEN 'voltage_sag'
                        WHEN b.voltage_v IS NOT NULL AND b.voltage_v > 253 THEN 'overvoltage'
                        WHEN jsonb_exists_any(
                            b.events, ARRAY['TAMPER_COVER_OPEN', 'MAGNETIC_FIELD_DETECTED']
                        ) THEN 'tamper'
                        WHEN b.power_kw IS NOT NULL AND m.med_power IS NOT NULL AND m.med_power > 0
                             AND b.power_kw > 3.5 * m.med_power THEN 'load_spike'
                        ELSE NULL
                    END AS detected
                FROM base b
                LEFT JOIN med m ON m.meter_id = b.meter_id
            )
            SELECT
                zone,
                count(*) FILTER (WHERE detected IS NOT NULL)::int AS detected,
                count(*) FILTER (
                    WHERE sim_label IN ('voltage_sag', 'overvoltage', 'load_spike', 'tamper', 'outage')
                )::int AS labelled,
                count(*) FILTER (WHERE detected IS NOT NULL AND detected = sim_label)::int AS confirmed
            FROM marked
            GROUP BY zone
        """.replace("POWER_KW_PATTERN", POWER_KW_PATTERN)
        async with self._pool.acquire() as con:
            file_rows = await con.fetch(
                "SELECT category, zone, count(*)::int AS n FROM files_metadata "
                "WHERE category IN ('outage_records', 'drone_videos') GROUP BY category, zone"
            )
            anomaly_rows = await con.fetch(anomaly_sql)
            audit_n = await con.fetchval(
                "SELECT count(*)::int FROM audit_logs a "
                "JOIN files_metadata f ON f.id = a.file_id "
                "WHERE f.category = 'connection_docs'"
            )
        buckets = {"outage_records": "outages", "drone_videos": "drones"}
        for row in file_rows:
            bucket = buckets.get(row["category"])
            if bucket and row["zone"] in ZONES:
                snap[bucket][row["zone"]] = int(row["n"])
        for row in anomaly_rows:
            snap["totals"]["detected"] += int(row["detected"])
            snap["totals"]["labelled"] += int(row["labelled"])
            snap["totals"]["confirmed"] += int(row["confirmed"])
            if row["zone"] in ZONES:
                snap["detected"][row["zone"]] = int(row["detected"])
                snap["labelled"][row["zone"]] = int(row["labelled"])
                snap["confirmed"][row["zone"]] = int(row["confirmed"])
        snap["audit_connection_docs"] = int(audit_n or 0)
        return snap

    # ---- slice J (scale-out) PostgresRepo methods: add directly below this line ----

    # ---- background scrubber (improvements.md 2.2) ----
    async def claim_for_scrub(self, categories, max_bytes, recheck_seconds, limit):
        """Take the files whose last check is oldest (never-checked first). Marking them
        'checking' in the same statement, with SKIP LOCKED, lets several workers and replicas
        scrub at once without ever taking the same file twice."""
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                "WITH picked AS ("
                "  SELECT id FROM files_metadata"
                "  WHERE category = ANY($1) AND size_bytes <= $2"
                "    AND (verified_at IS NULL OR verified_at < now() - make_interval(secs => $3))"
                "  ORDER BY verified_at NULLS FIRST, created_at"
                "  LIMIT $4 FOR UPDATE SKIP LOCKED) "
                "UPDATE files_metadata f SET verified_at = now(), verify_status = 'checking' "
                "FROM picked WHERE f.id = picked.id "
                "RETURNING f.id, f.filename, f.category, f.storage, f.storage_key, f.size_bytes, "
                "f.sha256, f.encrypted, f.trace_id, f.zone, f.station, f.description, "
                "f.uploaded_by, f.created_at",
                list(categories), int(max_bytes), float(recheck_seconds), int(limit),
            )
        return [_row_to_record(r) for r in rows]

    async def finish_scrub(self, file_id: str, status: str | None) -> None:
        """status None = could not check (storage down): back to the front of the queue."""
        async with self._pool.acquire() as con:
            if status is None:
                await con.execute(
                    "UPDATE files_metadata SET verified_at = NULL, verify_status = NULL "
                    "WHERE id = $1", uuid.UUID(file_id))
            else:
                await con.execute(
                    "UPDATE files_metadata SET verified_at = now(), verify_status = $2 "
                    "WHERE id = $1", uuid.UUID(file_id), status)

    async def scrub_stats(self, categories) -> dict:
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT count(*) FILTER (WHERE verify_status = 'ok') AS ok, "
                "count(*) FILTER (WHERE verify_status = 'failed') AS failed, "
                "count(*) FILTER (WHERE verified_at IS NULL) AS never_checked, "
                "count(*) AS total, max(verified_at) AS last_checked "
                "FROM files_metadata WHERE category = ANY($1)", list(categories))
        return {
            "files_total": int(row["total"]),
            "verified_ok": int(row["ok"]),
            "failed": int(row["failed"]),
            "never_checked": int(row["never_checked"]),
            "last_checked_at": row["last_checked"].isoformat() if row["last_checked"] else None,
        }

    # ---- replicas (improvements.md 2.2) ----
    async def set_replica(self, file_id, storage, storage_key, status) -> None:
        async with self._pool.acquire() as con:
            await con.execute(
                "INSERT INTO file_replicas (file_id, storage, storage_key, status) "
                "VALUES ($1,$2,$3,$4) ON CONFLICT (file_id) DO UPDATE SET "
                "storage = EXCLUDED.storage, storage_key = EXCLUDED.storage_key, "
                "status = EXCLUDED.status, updated_at = now()",
                uuid.UUID(file_id), storage, storage_key, status)

    async def get_replica(self, file_id) -> dict | None:
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT storage, storage_key, status FROM file_replicas WHERE file_id = $1",
                uuid.UUID(file_id))
        return dict(row) if row else None

    async def replica_stats(self) -> dict:
        async with self._pool.acquire() as con:
            rows = await con.fetch("SELECT status, count(*)::int AS n FROM file_replicas GROUP BY status")
        counts = {r["status"]: r["n"] for r in rows}
        return {"total": sum(counts.values()), "ok": counts.get("ok", 0),
                "pending": counts.get("pending", 0), "failed": counts.get("failed", 0)}

    # ---- chaos faults (improvements.md 2.1) ----
    async def set_fault(self, target, kind, seconds, duration, by) -> None:
        async with self._pool.acquire() as con:
            await con.execute(
                "INSERT INTO chaos_faults (target, kind, seconds, expires_at, created_by) "
                "VALUES ($1,$2,$3, now() + make_interval(secs => $4), $5) "
                "ON CONFLICT (target) DO UPDATE SET kind = EXCLUDED.kind, seconds = EXCLUDED.seconds, "
                "expires_at = EXCLUDED.expires_at, created_by = EXCLUDED.created_by, created_at = now()",
                target, kind, float(seconds), float(duration), by)

    async def clear_faults(self, target=None) -> int:
        async with self._pool.acquire() as con:
            if target is None:
                res = await con.execute("DELETE FROM chaos_faults")
            else:
                res = await con.execute("DELETE FROM chaos_faults WHERE target = $1", target)
        return int(res.split()[-1])

    async def active_faults(self) -> list[dict]:
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                "SELECT target, kind, seconds, created_by, expires_at, "
                "extract(epoch FROM (expires_at - now())) AS remaining "
                "FROM chaos_faults WHERE expires_at > now() ORDER BY target")
        return [{"target": r["target"], "kind": r["kind"], "seconds": float(r["seconds"]),
                 "created_by": r["created_by"], "expires_at": r["expires_at"].isoformat(),
                 "remaining_seconds": round(float(r["remaining"]), 1)} for r in rows]

    async def expired_faults(self) -> list[str]:
        """Targets whose fault ran out (the reaper undoes anything outside the database)."""
        async with self._pool.acquire() as con:
            rows = await con.fetch("DELETE FROM chaos_faults WHERE expires_at <= now() RETURNING target")
        return [r["target"] for r in rows]

    # ---- per-file keys (improvements.md 2.5) ----
    async def put_file_key(self, file_id, wrapped) -> None:
        async with self._pool.acquire() as con:
            await con.execute(
                "INSERT INTO file_keys (file_id, wrapped) VALUES ($1, $2) "
                "ON CONFLICT (file_id) DO NOTHING", uuid.UUID(file_id), bytes(wrapped))

    async def get_file_key(self, file_id) -> dict | None:
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT wrapped, status FROM file_keys WHERE file_id = $1", uuid.UUID(file_id))
        return {"wrapped": row["wrapped"], "status": row["status"]} if row else None

    async def erase_file_key(self, file_id, by) -> bool:
        """Destroy the wrapped key (tombstone). False when the file never had one."""
        async with self._pool.acquire() as con:
            res = await con.execute(
                "UPDATE file_keys SET wrapped = NULL, status = 'erased', erased_at = now(), "
                "erased_by = $2 WHERE file_id = $1 AND status = 'active'", uuid.UUID(file_id), by)
        return res.endswith("1")

    async def list_file_keys(self, after, limit) -> list[dict]:
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                "SELECT file_id::text AS file_id, wrapped FROM file_keys "
                "WHERE status = 'active' AND ($1::uuid IS NULL OR file_id > $1::uuid) "
                "ORDER BY file_id LIMIT $2", uuid.UUID(after) if after else None, int(limit))
        return [dict(r) for r in rows]

    async def replace_file_key(self, file_id, wrapped) -> None:
        async with self._pool.acquire() as con:
            await con.execute(
                "UPDATE file_keys SET wrapped = $2 WHERE file_id = $1 AND status = 'active'",
                uuid.UUID(file_id), bytes(wrapped))

    # ---- login lockout (improvements.md 2.5) ----
    async def login_state(self, email) -> tuple[int, float | None]:
        """(consecutive failures, seconds since the last one) for this account."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT failures, extract(epoch FROM (now() - last_failure)) AS ago "
                "FROM login_attempts WHERE email = $1", email)
        return (int(row["failures"]), float(row["ago"])) if row else (0, None)

    async def login_failed(self, email) -> int:
        async with self._pool.acquire() as con:
            await con.execute("DELETE FROM login_attempts WHERE last_failure < now() - interval '1 day'")
            row = await con.fetchrow(
                "INSERT INTO login_attempts (email, failures, last_failure) VALUES ($1, 1, now()) "
                "ON CONFLICT (email) DO UPDATE SET failures = login_attempts.failures + 1, "
                "last_failure = now() RETURNING failures", email)
        return int(row["failures"])

    async def login_ok(self, email) -> None:
        async with self._pool.acquire() as con:
            await con.execute("DELETE FROM login_attempts WHERE email = $1", email)

    # ---- testing overlay state ----
    async def set_testing(self, key, value, duration) -> None:
        async with self._pool.acquire() as con:
            await con.execute(
                "INSERT INTO testing_state (key, value, expires_at) "
                "VALUES ($1,$2, now() + make_interval(secs => $3)) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, expires_at = EXCLUDED.expires_at",
                key, json.dumps(value), float(duration))

    async def get_testing(self, key) -> dict | None:
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT value, extract(epoch FROM (expires_at - now())) AS left "
                "FROM testing_state WHERE key = $1 AND expires_at > now()", key)
        if not row:
            return None
        return {**json.loads(row["value"]), "_remaining": round(float(row["left"]), 1)}

    async def clear_testing(self, key) -> None:
        async with self._pool.acquire() as con:
            await con.execute("DELETE FROM testing_state WHERE key = $1", key)

    async def try_lease(self, name, holder, seconds) -> bool:
        """True when this holder owns the lease for the next `seconds` (one worker at a time)."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                "INSERT INTO testing_lease (name, holder, until) "
                "VALUES ($1,$2, now() + make_interval(secs => $3)) "
                "ON CONFLICT (name) DO UPDATE SET holder = EXCLUDED.holder, until = EXCLUDED.until "
                "WHERE testing_lease.until < now() OR testing_lease.holder = EXCLUDED.holder "
                "RETURNING holder", name, holder, float(seconds))
        return row is not None
