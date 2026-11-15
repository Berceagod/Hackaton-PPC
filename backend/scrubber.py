"""Background scrubber (improvements.md 2.2): the system finds damage before anyone asks.

Every `scrub_interval_seconds` a few stored files are re-hashed (decrypted first when they
are encrypted) and compared with the SHA-256 recorded at ingest. A mismatch is reported
exactly like a failed download: the integrity counter moves (so the FileIntegrityFailure
alert fires), the file's audit trail gets a `scrub_failed` entry, and an error is logged.

With replicas (replication.py) it also heals: a damaged primary is rewritten from the verified
copy, a damaged or missing copy is rewritten from the primary, and no alert pages for damage
that was repaired. Without a copy it only detects.

Meter readings are not scrubbed by default: there are millions of tiny files, each one is
verified when read, and their guarantee is exactly-once ingest, not long-term integrity.
Files above `scrub_max_bytes` are skipped so a background job never reads gigabytes.
"""

import asyncio
import logging
import uuid

from fastapi import HTTPException

import metrics
import replication
from audit_chain import GENESIS
from logging_setup import trace_id_var

log = logging.getLogger("filegrid.scrubber")

SCRUBBER_USER = "scrubber"


async def scrub_once(deps) -> dict:
    """Verify one batch. Returns {"ok": n, "failed": n, "skipped": n}."""
    from routes.files import _verify  # imported here: routes import this module's siblings

    s = deps.settings
    rows = await deps.repo.claim_for_scrub(
        list(s.scrub_categories), s.scrub_max_bytes, s.scrub_recheck_seconds, s.scrub_batch
    )
    done = {"ok": 0, "failed": 0, "skipped": 0}
    for rec in rows:
        token = trace_id_var.set(str(uuid.uuid4()))  # every check is traceable on its own
        try:
            try:
                ok, _actual, reason = await _verify(deps, rec)
            except HTTPException:
                # The storage is down: that is not the file's fault. Put it back in the queue.
                await deps.repo.finish_scrub(rec.id, None)
                metrics.scrubbed(rec.category, rec.storage, "skipped")
                done["skipped"] += 1
                continue
            if reason == "erased":
                # erased on purpose (its key was destroyed): not damage, never an alert
                await deps.repo.finish_scrub(rec.id, "erased")
                continue
            if not ok:
                # a verified second copy, if the policy kept one, repairs the primary
                if await replication.heal(deps, rec, SCRUBBER_USER) == "repaired":
                    await deps.repo.finish_scrub(rec.id, "ok")
                    metrics.scrubbed(rec.category, rec.storage, "repaired")
                    done["repaired"] = done.get("repaired", 0) + 1
                    continue
            else:
                await _keep_copy_healthy(deps, rec)
            if ok:
                await deps.repo.finish_scrub(rec.id, "ok")
                metrics.scrubbed(rec.category, rec.storage, "ok")
                done["ok"] += 1
                continue
            await deps.repo.finish_scrub(rec.id, "failed")
            metrics.scrubbed(rec.category, rec.storage, "failed")
            metrics.integrity_failure(rec.category, rec.storage, reason)
            await deps.repo.add_audit(
                rec.id, "scrub_failed", SCRUBBER_USER, trace_id_var.get(),
                f"Verificare periodică: integritate eșuată ({reason})",
            )
            log.error(
                "scrubber found a damaged file",
                extra={"category": rec.category, "storage": rec.storage,
                       "file_id": rec.id, "reason": reason},
            )
            done["failed"] += 1
        finally:
            trace_id_var.reset(token)
    return done


async def _keep_copy_healthy(deps, rec) -> None:
    """The primary is intact: make sure the second copy is too, or create it if the policy
    now asks for one (files stored before `replicas` was switched on, or whose copy could not
    be written at ingest)."""
    try:
        if await deps.repo.get_replica(rec.id) is None:
            policy = await deps.policies.get(rec.category)
            if policy.replicas:
                await replication.replicate(deps, rec, policy, SCRUBBER_USER)
        else:
            await replication.heal_replica(deps, rec, SCRUBBER_USER)
    except Exception:  # the copy is a bonus: never let it hide the primary's result
        log.exception("replica check failed", extra={"file_id": rec.id})


async def anchor_audit_head(deps) -> None:
    """Log the audit chain's head hash: a copy outside the database that its owner cannot
    quietly rewrite once it has been shipped to the log store (see audit_chain.py)."""
    result = await deps.repo.verify_audit_chain()
    log.info(
        "audit anchor",
        extra={"head_hash": result["head_hash"] or GENESIS, "rows": result["checked"],
               "chain_valid": result["valid"]},
    )
    if not result["valid"]:
        log.error("audit chain is BROKEN", extra={"first_break": result["first_break"]})


async def run_scrubber(deps, stop: asyncio.Event) -> None:
    s = deps.settings
    if not s.scrub_enabled:
        log.info("scrubber disabled")
        return
    ticks = 0
    anchor_every = max(int(3600 / max(s.scrub_interval_seconds, 1)), 1)  # about hourly
    while not stop.is_set():
        try:
            await scrub_once(deps)
            ticks += 1
            if ticks % anchor_every == 1:
                await anchor_audit_head(deps)
        except Exception:
            log.exception("scrubber pass failed")
        try:
            await asyncio.wait_for(stop.wait(), max(s.scrub_interval_seconds, 1.0))
        except (asyncio.TimeoutError, TimeoutError):
            pass
