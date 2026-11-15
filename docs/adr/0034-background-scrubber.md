# ADR-0034: Background scrubber (detect damage before anyone asks)
Status: accepted (improvements.md 2.2, detection only)

## Context
Corruption was detected only when someone downloaded the file. A file nobody opens for months can rot unnoticed, and by then the last good copy may be gone.

## Decision
- Every worker runs `scrubber.run_scrubber`: each `SCRUB_INTERVAL_SECONDS` (30) it claims `SCRUB_BATCH` (5) files and re-verifies them with the same code as a download (decrypting first when encrypted). A mismatch is reported exactly like a failed download: the integrity counter moves (so the existing `FileIntegrityFailure` alert fires, shown live: no download involved), the file's audit trail gets `scrub_failed`, an error is logged, and `verify_status` becomes `failed`.
- **Claiming is a single statement**: `UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED) RETURNING`, never-checked files first, then the oldest check. Workers and replicas therefore never take the same file twice, with no coordination service. A file is re-checked at most every `SCRUB_RECHECK_SECONDS` (1 h).
- A **storage outage is not the file's fault**: the check is cancelled (`skipped`) and the file returns to the front of the queue; nothing is marked failed.
- Scope: `outage_records`, `connection_docs`, `drone_videos`. **Meter readings are excluded** (millions of tiny files; each is verified on read; their guarantee is exactly-once ingest). Files above `SCRUB_MAX_BYTES` (64 MB) are skipped so a background job never reads gigabytes.
- `GET /admin/scrubber` (admin) reports what it has seen (verified, failed, never checked, last check). New metric `filegrid_scrub_files_total{category,storage,outcome}`.
- All of it is configurable by environment variable, off switch included (`SCRUB_ENABLED`).

## What it does NOT do
It does not repair. Auto-repair needs a second copy on another storage (replicas), which is not implemented (improvements.md 2.2 remains open for that part). Detection alone already changes the incident from "a user hit a 409 months later" to "the team is paged now".

## Consequences
- Measured on the live test stack: 381 existing files scanned, 36 verified in the first pass, exactly the 4 files earlier demos had corrupted were flagged, no false positive; a freshly damaged file was found 3 s after becoming due.
- The load is bounded (batch, size cap, hourly re-check); raise the batch on large installations.
- Proof: `scripts/demo/scrubber.sh`.
