"""Metadata repository (PostgreSQL via asyncpg). Tables are created idempotently at startup."""

import asyncio
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Protocol

import asyncpg

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
CREATE INDEX IF NOT EXISTS idx_files_cat_sha ON files_metadata (category, sha256);

CREATE TABLE IF NOT EXISTS policies_config (
    category    TEXT PRIMARY KEY,
    storage     TEXT        NOT NULL,
    encryption  BOOLEAN     NOT NULL,
    deduplicate BOOLEAN     NOT NULL,
    priority    TEXT        NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


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
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, dsn: str) -> "PostgresRepo":
        pool = None
        last: Exception | None = None
        for _ in range(30):  # postgres may still be starting
            try:
                pool = await asyncpg.create_pool(dsn, min_size=1, max_size=10)
                break
            except (OSError, asyncpg.PostgresError) as exc:
                last = exc
                await asyncio.sleep(2)
        if pool is None:
            raise RuntimeError(f"Postgres not reachable: {last}")
        return cls(pool)

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
        return ("duplicate" if row["status"] == "stored" else "in_flight"), str(row["file_id"])

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
                    "UPDATE meter_readings SET status = 'stored' "
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
        async with self._pool.acquire() as con:
            await con.execute(
                "INSERT INTO audit_logs (id, file_id, action, username, trace_id, details) "
                "VALUES ($1,$2,$3,$4,$5,$6)",
                uuid.uuid4(),
                uuid.UUID(file_id),
                action,
                user,
                trace_id,
                details,
            )

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

    async def healthy(self) -> bool:
        try:
            async with self._pool.acquire() as con:
                await con.fetchval("SELECT 1")
            return True
        except Exception:
            return False
