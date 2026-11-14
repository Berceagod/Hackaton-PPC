"""Streaming AES-256-GCM for data at rest (ADR-0011).

Format (version 1):  MAGIC "FGE1" | 8-byte random nonce prefix | segment_0 | segment_1 | ...
Each segment is up to 64 KB of plaintext sealed with AES-256-GCM (16-byte tag):
  nonce = prefix (8) || segment index (4, big endian)
  AAD   = "filegrid/v1|" + object id + "|" + index + "|" + final flag
The final flag on the last segment makes truncation detectable; the index in nonce and AAD
makes reordering detectable; the object id in the AAD makes moving ciphertext from one
file to another detectable. Every segment is authenticated before any of its plaintext is
released, and memory stays at about one segment whatever the file size.

Keys: one AES-256 key per object, derived with HKDF-SHA256 from the master key (only in
the backend's environment) and the object id. Nothing secret is stored next to the data:
a copy of a storage, or of the metadata database, decrypts nothing on its own.
"""

import os
from typing import AsyncIterator

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"FGE1"
PREFIX_LEN = 8
SEGMENT = 64 * 1024
TAG = 16
HEADER_LEN = len(MAGIC) + PREFIX_LEN
MAX_SEGMENTS = 2**32


class DecryptionError(Exception):
    """Ciphertext is not authentic: tampered, truncated, reordered, wrong key or wrong file."""


def derive_key(master_key: bytes, object_id: str) -> bytes:
    if len(master_key) != 32:
        raise ValueError("master key must be 32 bytes")
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None,
        info=b"filegrid/v1/object-key/" + object_id.encode(),
    ).derive(master_key)


def _aad(object_id: str, index: int, final: bool) -> bytes:
    return f"filegrid/v1|{object_id}|{index}|{int(final)}".encode()


def _nonce(prefix: bytes, index: int) -> bytes:
    if index >= MAX_SEGMENTS:
        raise ValueError("object too large for one key/nonce prefix")
    return prefix + index.to_bytes(4, "big")


async def encrypt_stream(
    plaintext: AsyncIterator[bytes], master_key: bytes, object_id: str
) -> AsyncIterator[bytes]:
    aes = AESGCM(derive_key(master_key, object_id))
    prefix = os.urandom(PREFIX_LEN)
    yield MAGIC + prefix
    buf = bytearray()
    index = 0
    async for piece in plaintext:
        buf += piece
        # strictly more than one segment buffered: this one is certainly not the last
        while len(buf) > SEGMENT:
            seg = bytes(buf[:SEGMENT])
            del buf[:SEGMENT]
            yield aes.encrypt(_nonce(prefix, index), seg, _aad(object_id, index, False))
            index += 1
    # the last segment always exists (possibly empty) and carries final=1
    yield aes.encrypt(_nonce(prefix, index), bytes(buf), _aad(object_id, index, True))


async def decrypt_stream(
    ciphertext: AsyncIterator[bytes], master_key: bytes, object_id: str
) -> AsyncIterator[bytes]:
    aes = AESGCM(derive_key(master_key, object_id))
    block = SEGMENT + TAG
    buf = bytearray()
    prefix: bytes | None = None
    index = 0

    def open_segment(seg: bytes, final: bool) -> bytes:
        try:
            return aes.decrypt(_nonce(prefix, index), seg, _aad(object_id, index, final))
        except InvalidTag:
            raise DecryptionError(f"segment {index} failed authentication") from None

    async for piece in ciphertext:
        buf += piece
        if prefix is None:
            if len(buf) < HEADER_LEN:
                continue
            if bytes(buf[: len(MAGIC)]) != MAGIC:
                raise DecryptionError("not a FileGrid encrypted object")
            prefix = bytes(buf[len(MAGIC):HEADER_LEN])
            del buf[:HEADER_LEN]
        while len(buf) > block:  # more follows: not the final segment
            seg = bytes(buf[:block])
            del buf[:block]
            yield open_segment(seg, False)
            index += 1
    if prefix is None:
        raise DecryptionError("missing header")
    if len(buf) < TAG:
        raise DecryptionError("truncated object")
    yield open_segment(bytes(buf), True)
