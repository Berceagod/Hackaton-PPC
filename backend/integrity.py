"""Streaming SHA-256 helpers (64 KB reads; never holds a whole file in memory)."""

import hashlib
from typing import AsyncIterator

from storage.base import CHUNK


class HashingStream:
    """Wraps an async chunk source, hashing and counting bytes as they flow through."""

    def __init__(self, source: AsyncIterator[bytes]) -> None:
        self._source = source
        self._hash = hashlib.sha256()
        self.size = 0

    def __aiter__(self) -> "HashingStream":
        return self

    async def __anext__(self) -> bytes:
        chunk = await self._source.__anext__()
        self._hash.update(chunk)
        self.size += len(chunk)
        return chunk

    @property
    def hexdigest(self) -> str:
        return self._hash.hexdigest()


async def upload_chunks(upload_file) -> AsyncIterator[bytes]:
    """Yield an UploadFile in 64 KB pieces."""
    while True:
        chunk = await upload_file.read(CHUNK)
        if not chunk:
            return
        yield chunk


async def sha256_of_stream(chunks: AsyncIterator[bytes]) -> tuple[str, int]:
    stream = HashingStream(chunks.__aiter__())
    async for _ in stream:
        pass
    return stream.hexdigest, stream.size
