"""Second copies and self-healing (improvements.md 2.2).

A category whose policy says `replicas: 1` keeps one extra copy of every file on a DIFFERENT
storage (S3 primary -> FTP copy, for example). The copy is the stored bytes, so an encrypted
document stays encrypted and the same per-file key reads both.

What the copy buys:
- a download whose primary is damaged or gone is repaired from the good copy and served
  (`X-Repaired: true`), instead of answering 409;
- a download while the primary storage is down is served from the copy
  (`X-Served-From: <storage>`);
- the scrubber repairs a damaged copy (either side) before anyone asks, and completes a copy
  that could not be written at ingest.

Every repair is verified against the SHA-256 recorded at ingest before it is trusted, written
to the audit trail, counted (`filegrid_repairs_total`) and logged. If BOTH copies are damaged
nothing is served and the usual integrity alert fires.

Cost, stated plainly: replicas double the bytes written (and read by verification), so they
are per category and off by default; drone videos are large, outage records are small and are
the category that must never be lost.
"""

import logging

from crypto_envelope import DecryptionError, decrypt_stream
from integrity import sha256_of_stream
import keystore
import metrics
from storage.base import ObjectNotFound, StorageError

log = logging.getLogger("filegrid.replication")

AUTO_ORDER = ("s3", "ftp", "postgres")  # preference when the policy does not name a storage
SYSTEM_USER = "system"


def replica_target(deps, policy, primary: str) -> str | None:
    """The storage that should hold the copy, or None when the policy asks for none."""
    if not policy.replicas:
        return None
    named = policy.replica_storage
    if named and named != primary and named in deps.storages:
        return named
    for name in AUTO_ORDER:
        if name != primary and name in deps.storages:
            return name
    return None


async def plaintext_of(deps, rec, storage_name: str, key: str):
    """The file's original bytes as held by one specific copy (decrypted with the file's own
    key, or the legacy derived key). Raises keystore.FileErased after an erasure."""
    source = deps.storages[storage_name].get_stream(key)
    if rec.encrypted:
        source = decrypt_stream(source, await keystore.material(deps, rec), rec.id)
    async for piece in source:
        yield piece


async def check_copy(deps, rec, storage_name: str, key: str) -> tuple[bool, str]:
    """Is this copy intact? Returns (ok, reason); reason is '' when ok."""
    try:
        actual, _ = await sha256_of_stream(plaintext_of(deps, rec, storage_name, key))
    except ObjectNotFound:
        return False, "object_missing"
    except keystore.FileErased:
        return False, "erased"
    except DecryptionError:
        return False, "decryption_failed"
    except StorageError:
        return False, "storage_unavailable"
    if actual != rec.sha256:
        return False, "checksum_mismatch"
    return True, ""


async def _copy(deps, src: str, src_key: str, dst: str, dst_key: str) -> None:
    """Stream the stored bytes from one storage to another, never buffering the file."""
    await deps.storages[dst].put_stream(dst_key, deps.storages[src].get_stream(src_key))


async def replicate(deps, rec, policy, actor: str = SYSTEM_USER) -> str:
    """Write and verify the second copy. Returns skipped | ok | pending.

    A failure never fails the upload (the file is already safely stored and acknowledged):
    the copy is recorded as `pending` and the scrubber completes it later."""
    target = replica_target(deps, policy, rec.storage)
    if target is None:
        return "skipped"
    key = rec.storage_key
    try:
        await _copy(deps, rec.storage, rec.storage_key, target, key)
        ok, reason = await check_copy(deps, rec, target, key)
    except StorageError as exc:
        ok, reason = False, f"write_failed: {exc}"
    except Exception:  # a replica problem must never turn a stored file into an upload error
        log.exception("replica write failed", extra={"file_id": rec.id})
        ok, reason = False, "write_failed"
    await deps.repo.set_replica(rec.id, target, key, "ok" if ok else "pending")
    outcome = "ok" if ok else "pending"
    metrics.replica_written(rec.category, target, outcome)
    if ok:
        await deps.repo.add_audit(
            rec.id, "replicated", actor, _trace(), f"Copie secundară scrisă și verificată → {target}")
    else:
        log.warning("replica not written; scrubber will retry",
                    extra={"file_id": rec.id, "storage": target, "reason": reason})
    return outcome


async def heal(deps, rec, actor: str = SYSTEM_USER) -> str | None:
    """The primary copy is damaged, missing or unreachable: use the replica.

    Returns 'repaired' (primary rewritten from the good copy and verified), 'replica' (primary
    could not be rewritten, e.g. its storage is down: serve the replica), or None (no usable
    replica: nothing may be served)."""
    rep = await deps.repo.get_replica(rec.id)
    if not rep or rep["status"] == "pending":
        return None
    ok, reason = await check_copy(deps, rec, rep["storage"], rep["storage_key"])
    if not ok:
        if reason != "storage_unavailable":
            await deps.repo.set_replica(rec.id, rep["storage"], rep["storage_key"], "failed")
        return None
    try:
        await _copy(deps, rep["storage"], rep["storage_key"], rec.storage, rec.storage_key)
        primary_ok, why = await check_copy(deps, rec, rec.storage, rec.storage_key)
    except StorageError as exc:
        primary_ok, why = False, str(exc)
    if primary_ok:
        metrics.repaired(rec.category, rec.storage, "primary", "ok")
        await deps.repo.add_audit(
            rec.id, "repaired", actor, _trace(),
            f"Copia principală ({rec.storage}) reparată din copia de pe {rep['storage']}")
        log.warning("primary repaired from replica",
                    extra={"category": rec.category, "storage": rec.storage,
                           "file_id": rec.id, "replica": rep["storage"]})
        return "repaired"
    metrics.repaired(rec.category, rec.storage, "primary", "failed")
    log.error("primary could not be repaired; serving the replica",
              extra={"category": rec.category, "storage": rec.storage,
                     "file_id": rec.id, "reason": why})
    return "replica"


async def heal_replica(deps, rec, actor: str = SYSTEM_USER) -> str:
    """The primary is intact; check the copy and rewrite it if it is damaged, missing or was
    never written. Returns ok | repaired | failed | skipped (copy's storage unreachable)."""
    rep = await deps.repo.get_replica(rec.id)
    if rep is None:
        return "ok"
    if rep["status"] == "ok":
        ok, reason = await check_copy(deps, rec, rep["storage"], rep["storage_key"])
        if ok:
            return "ok"
        if reason == "storage_unavailable":
            return "skipped"
    try:
        await _copy(deps, rec.storage, rec.storage_key, rep["storage"], rep["storage_key"])
        ok, _ = await check_copy(deps, rec, rep["storage"], rep["storage_key"])
    except StorageError:
        return "skipped"
    await deps.repo.set_replica(rec.id, rep["storage"], rep["storage_key"], "ok" if ok else "failed")
    metrics.repaired(rec.category, rep["storage"], "replica", "ok" if ok else "failed")
    if ok:
        await deps.repo.add_audit(
            rec.id, "replica_repaired", actor, _trace(),
            f"Copia de pe {rep['storage']} refăcută din copia principală ({rec.storage})")
        log.warning("replica repaired from primary",
                    extra={"category": rec.category, "storage": rep["storage"], "file_id": rec.id})
        return "repaired"
    return "failed"


def _trace() -> str:
    from logging_setup import trace_id_var
    return trace_id_var.get()
