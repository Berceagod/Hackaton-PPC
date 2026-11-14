"""FTP adapter (aioftp) with a small pool of logged-in sessions.

Why a pool: logging in to the FTP server costs seconds (it uses a deliberately slow
password hash), while a transfer on an open session costs milliseconds. Sessions are
therefore kept and reused; a session that fails, or that was abandoned mid-transfer, is
thrown away and replaced, which is also how the adapter reconnects after an outage.

The pool size caps how many sessions this backend instance holds, so extra operations
wait here instead of being refused by the server ("421 too many users").

Uploads go to `<key>.part` and are renamed to `<key>` only when complete, so a
half-written file is never visible as a stored object.
"""

import asyncio
import logging
import posixpath
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

import aioftp

from storage.base import CHUNK, ObjectNotFound, StorageError

log = logging.getLogger("filegrid.storage.ftp")

_NET_ERRORS = (aioftp.AIOFTPException, OSError, asyncio.TimeoutError, EOFError)
_HEALTH_FRESH_SECONDS = 15.0


class _Lease:
    """A session checked out of the pool. Set `reusable` once the session is known to
    be in a clean state; otherwise it is closed when the lease ends."""

    def __init__(self, client: aioftp.Client) -> None:
        self.client = client
        self.reusable = False


class FtpStorage:
    name = "ftp"

    def __init__(
        self, host: str, port: int, user: str, password: str, max_concurrency: int = 8
    ) -> None:
        self._host, self._port, self._user, self._password = host, port, user, password
        self._slots = asyncio.Semaphore(max_concurrency)
        self._idle: list[aioftp.Client] = []
        self._known_dirs: set[str] = set()
        self._last_ok = 0.0

    # ---------------- session pool ----------------

    async def _open(self, attempts: int = 3) -> aioftp.Client:
        last: Exception | None = None
        for attempt in range(attempts):
            client = aioftp.Client(socket_timeout=30, path_timeout=30)
            try:
                await client.connect(self._host, self._port)
                await client.login(self._user, self._password)
                return client
            except _NET_ERRORS as exc:  # includes 421 "too many users": back off, retry
                last = exc
                client.close()
                if attempt < attempts - 1:
                    await asyncio.sleep(0.3 * 2**attempt)
        raise StorageError(f"ftp unavailable: {last}")

    async def _checkout(self) -> aioftp.Client:
        while self._idle:
            client = self._idle.pop()
            try:
                await client.command("NOOP", "2xx")  # still alive?
                return client
            except _NET_ERRORS:
                client.close()  # dropped by the server or the network: replace it
        return await self._open()

    @asynccontextmanager
    async def _session(self):
        async with self._slots:
            lease = _Lease(await self._checkout())
            try:
                yield lease
            finally:
                if lease.reusable:
                    self._last_ok = time.monotonic()
                    self._idle.append(lease.client)
                else:
                    lease.client.close()

    async def _ensure_dir(self, client: aioftp.Client, directory: str) -> None:
        if directory and directory not in self._known_dirs:
            await client.make_directory(directory, parents=True)
            self._known_dirs.add(directory)

    # ---------------- StorageBackend ----------------

    async def put_stream(self, key: str, chunks: AsyncIterator[bytes]) -> int:
        size = 0
        part = f"{key}.part"
        parent = posixpath.dirname(key)
        async with self._session() as lease:
            client = lease.client
            try:
                await self._ensure_dir(client, parent)
                async with client.upload_stream(part) as out:
                    async for chunk in chunks:
                        await out.write(chunk)
                        size += len(chunk)
                try:
                    await client.rename(part, key)
                except aioftp.StatusCodeError:
                    # server refuses to rename over an existing file: replace it
                    await client.remove(key)
                    await client.rename(part, key)
            except _NET_ERRORS as exc:
                self._known_dirs.discard(parent)  # the directory may be gone
                raise StorageError(f"ftp put failed: {exc}") from exc
            lease.reusable = True
        return size

    async def get_stream(self, key: str) -> AsyncIterator[bytes]:
        async with self._session() as lease:
            client = lease.client
            try:
                try:
                    stream_cm = client.download_stream(key)
                    stream = await stream_cm.__aenter__()
                except aioftp.StatusCodeError as exc:
                    if any(str(code).startswith("55") for code in exc.received_codes):
                        lease.reusable = True  # "no such file" leaves the session clean
                        raise ObjectNotFound(key) from exc
                    raise
                async for block in stream.iter_by_block(CHUNK):
                    yield block
                await stream_cm.__aexit__(None, None, None)
            except ObjectNotFound:
                raise
            except _NET_ERRORS as exc:
                raise StorageError(f"ftp get failed: {exc}") from exc
            # Reached only when the transfer completed; a reader that stops early
            # leaves the session mid-transfer, so it is closed instead of reused.
            lease.reusable = True

    async def delete(self, key: str) -> None:
        async with self._session() as lease:
            try:
                if await lease.client.exists(key):
                    await lease.client.remove(key)
            except _NET_ERRORS as exc:
                raise StorageError(f"ftp delete failed: {exc}") from exc
            lease.reusable = True

    async def healthy(self) -> bool:
        # A recent successful operation is proof enough; do not add load (or wait
        # behind long transfers) just to answer a health check.
        if time.monotonic() - self._last_ok < _HEALTH_FRESH_SECONDS:
            return True
        try:
            async with self._session() as lease:
                lease.reusable = True
            return True
        except StorageError:
            return False
