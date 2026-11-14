"""PostgreSQL blob adapter (separate `postgres-blob` instance).

Files are stored as 1 MB rows in `blob_chunks`, never as one giant value, so both
writing and reading stream with bounded memory. The whole write is one transaction:
either every chunk is durable or none is.
"""

import asyncio
import logging
from typing import AsyncIterator

import asyncpg

from storage.base import CHUNK, ObjectNotFound, StorageError

log = logging.getLogger("filegrid.storage.postgres")

ROW_SIZE = 1024 * 1024

DDL = """
CREATE TABLE IF NOT EXISTS blob_chunks (
    key  TEXT    NOT NULL,
    seq  INTEGER NOT NULL,
    data BYTEA   NOT NULL,
    PRIMARY KEY (key, seq)
)
"""


class PostgresStorage:
    name = "postgres"

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None
        self._lock = asyncio.Lock()

    async def _get_pool(self) -> asyncpg.Pool:
        """Lazy connect: the backend must boot (and serve other categories) even if this DB is down."""
        if self._pool is not None:
            return self._pool
        async with self._lock:
            if self._pool is None:
                try:
                    pool = await asyncpg.create_pool(
                        self._dsn, min_size=1, max_size=8, timeout=5
                    )
                    async with pool.acquire() as con:
                        await con.execute(DDL)
                    self._pool = pool
                except (OSError, asyncpg.PostgresError, asyncio.TimeoutError) as exc:
                    raise StorageError(f"postgres-blob unavailable: {exc}") from exc
        return self._pool

    async def put_stream(self, key: str, chunks: AsyncIterator[bytes]) -> int:
        pool = await self._get_pool()
        size = 0
        buf = bytearray()
        seq = 0
        try:
            async with pool.acquire() as con:
                async with con.transaction():
                    await con.execute("DELETE FROM blob_chunks WHERE key = $1", key)
                    async for chunk in chunks:
                        buf += chunk
                        size += len(chunk)
                        while len(buf) >= ROW_SIZE:
                            await con.execute(
                                "INSERT INTO blob_chunks (key, seq, data) VALUES ($1,$2,$3)",
                                key, seq, bytes(buf[:ROW_SIZE]),
                            )
                            del buf[:ROW_SIZE]
                            seq += 1
                    if buf or seq == 0:
                        await con.execute(
                            "INSERT INTO blob_chunks (key, seq, data) VALUES ($1,$2,$3)",
                            key, seq, bytes(buf),
                        )
        except (OSError, asyncpg.PostgresError) as exc:
            raise StorageError(f"postgres put failed: {exc}") from exc
        return size

    async def get_stream(self, key: str) -> AsyncIterator[bytes]:
        pool = await self._get_pool()
        seq = 0
        try:
            while True:
                async with pool.acquire() as con:
                    data = await con.fetchval(
                        "SELECT data FROM blob_chunks WHERE key = $1 AND seq = $2",
                        key, seq,
                    )
                if data is None:
                    if seq == 0:
                        raise ObjectNotFound(key)
                    return
                for i in range(0, len(data), CHUNK):
                    yield data[i : i + CHUNK]
                seq += 1
        except (OSError, asyncpg.PostgresError) as exc:
            raise StorageError(f"postgres get failed: {exc}") from exc

    async def delete(self, key: str) -> None:
        pool = await self._get_pool()
        try:
            async with pool.acquire() as con:
                await con.execute("DELETE FROM blob_chunks WHERE key = $1", key)
        except (OSError, asyncpg.PostgresError) as exc:
            raise StorageError(f"postgres delete failed: {exc}") from exc

    async def healthy(self) -> bool:
        try:
            pool = await self._get_pool()
            async with pool.acquire() as con:
                await con.fetchval("SELECT 1")
            return True
        except (StorageError, OSError, asyncpg.PostgresError):
            return False
