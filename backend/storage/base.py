"""Storage adapter contract. Every backend streams: no whole-file buffering."""

from typing import AsyncIterator, Protocol

CHUNK = 64 * 1024  # read granularity (64 KB)


class StorageError(Exception):
    """Storage unavailable or failed (nothing durable was written)."""


class ObjectNotFound(StorageError):
    """The object is missing from the storage."""


class StorageBackend(Protocol):
    name: str

    async def put_stream(self, key: str, chunks: AsyncIterator[bytes]) -> int:
        """Persist the stream durably; return the number of bytes written."""

    def get_stream(self, key: str) -> AsyncIterator[bytes]:
        """Stream the object back in chunks. Raises ObjectNotFound."""

    async def delete(self, key: str) -> None: ...

    async def healthy(self) -> bool: ...
