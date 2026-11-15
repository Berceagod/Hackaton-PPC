"""Per-file random keys, wrapped by the master key (improvements.md 2.5: crypto-shredding).

Before: every encrypted file's key was derived from the master key and the file id, so a
file could never be made unreadable without destroying the master key (and every other file).

Now: each newly encrypted file gets its own random 32-byte key K. K is stored ONLY wrapped
(AES-256-GCM under a key derived from the master key, bound to the file id) in `file_keys`.
The file itself is encrypted with a key derived from K, exactly as before (crypto_envelope).

What that buys:
- **Erasure**: destroying a file's wrapped key makes its ciphertext permanently unreadable,
  wherever copies of that ciphertext still are (replicas, storage snapshots, a restored
  backup of the storage). A backup of the *database* taken before the erasure still holds the
  wrapped key: those backups must age out for erasure to be complete.
- **Master-key rotation without re-encrypting any file**: only the small wrapped keys are
  re-wrapped (`rewrap_all`).

Files encrypted before this change (and meter readings) keep their derived key: there is no
row for them, `material()` returns the master key and the format is unchanged. They cannot be
crypto-shredded (their objects can still be deleted).
"""

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

WRAP_INFO = b"filegrid/v1/file-key-wrap"
NONCE_LEN = 12


class FileErased(Exception):
    """The file's key was destroyed on purpose: its content can never be read again."""


class KeyUnwrapError(Exception):
    """A stored wrapped key does not authenticate (wrong master key, or tampered)."""


def _wrapping_key(master_key: bytes) -> bytes:
    if len(master_key) != 32:
        raise ValueError("master key must be 32 bytes")
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=WRAP_INFO).derive(master_key)


def wrap(master_key: bytes, file_id: str, file_key: bytes) -> bytes:
    nonce = os.urandom(NONCE_LEN)
    return nonce + AESGCM(_wrapping_key(master_key)).encrypt(nonce, file_key, file_id.encode())


def unwrap(master_key: bytes, file_id: str, blob: bytes) -> bytes:
    try:
        return AESGCM(_wrapping_key(master_key)).decrypt(
            blob[:NONCE_LEN], blob[NONCE_LEN:], file_id.encode())
    except (InvalidTag, ValueError):
        raise KeyUnwrapError(f"wrapped key of {file_id} does not authenticate") from None


async def new_file_key(deps, file_id: str) -> bytes:
    """Create and store (wrapped) the key for a file about to be encrypted."""
    file_key = os.urandom(32)
    await deps.repo.put_file_key(file_id, wrap(deps.settings.master_key, file_id, file_key))
    return file_key


async def material(deps, rec) -> bytes:
    """The key material `crypto_envelope` needs for this file: its own key if it has one,
    else the master key (legacy derived key). Raises FileErased after an erasure."""
    row = await deps.repo.get_file_key(rec.id)
    if row is None:
        return deps.settings.master_key
    if row["status"] == "erased" or row["wrapped"] is None:
        raise FileErased(rec.id)
    return unwrap(deps.settings.master_key, rec.id, bytes(row["wrapped"]))


async def destroy(deps, file_id: str, by: str) -> bool:
    """Destroy the wrapped key. Returns False when the file had none (legacy derived key)."""
    return await deps.repo.erase_file_key(file_id, by)


async def rewrap_all(repo, old_master: bytes, new_master: bytes, batch: int = 500) -> int:
    """Master-key rotation: re-wrap every active file key under the new master key. The files
    themselves are not touched. Returns how many keys were re-wrapped."""
    done, after = 0, None
    while True:
        rows = await repo.list_file_keys(after, batch)
        if not rows:
            return done
        for row in rows:
            fid = row["file_id"]
            await repo.replace_file_key(
                fid, wrap(new_master, fid, unwrap(old_master, fid, bytes(row["wrapped"]))))
            done += 1
        after = rows[-1]["file_id"]
