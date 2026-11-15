# ADR-0035: Second copies and self-healing
Status: accepted (improvements.md 2.2). Extends ADR-0034 (the scrubber detected; now it also heals).

## Context
Integrity was provable and a corrupt file was never served, but the answer to damage was "409, restore from backup". For outage records, which must be available immediately, a refused download during an incident is itself the failure.

## Decision
- **Policy** gains `replicas` (0 or 1) and an optional `replica_storage`. One extra copy lives on a DIFFERENT storage; without `replica_storage` the first of s3, ftp, postgres that is not the primary is used. `meter_readings` cannot have replicas (exactly-once ingest, tiny files). The documented four-field policy shape is unchanged: the new fields appear only when switched on, and an update that does not mention them (the UI never sends them) keeps them. Off by default in `policies.yaml`; switched on per category by a normal policy change, hot-reloaded.
- **Table `file_replicas(file_id, storage, storage_key, status)`**, status `ok | pending | failed`. No change to `files_metadata`.
- **Write path**: after the primary is stored and acknowledged, the stored bytes are streamed to the other storage and the copy is re-hashed before it counts. A failure never fails the upload: the copy is `pending` and the scrubber completes it. Encrypted documents are copied as ciphertext; the same per-file key reads both. Chunked uploads do the same.
- **Download**: if the primary fails its SHA-256 (or is missing), the replica is verified, the primary is rewritten from it and re-verified, and the file is served with `X-Repaired: true`. If the primary's storage is down (or cannot be rewritten) the verified replica is served with `X-Served-From`. If there is no good copy, the answer is the same 409 as before. Repaired damage is NOT counted as an integrity failure (it does not page); it is counted in `filegrid_repairs_total{category,storage,kind,outcome}`, written to the audit trail (`repaired`) and logged.
- **Scrubber**: repairs a damaged primary from the copy, rewrites a damaged or missing copy from the primary (`replica_repaired`), and creates the copy for files stored before `replicas` was switched on. When both copies are bad it reports exactly as before and the critical alert fires.
- **Alerts**: `FileRepaired` (info: damage that was repaired) and `RepairFailed` (critical), both with promtool tests. Series are created at 0 so the first repair is seen by `increase()`.

## Cost and limits (stated plainly)
- Writes double (and the copy is re-read once to verify it). That is why it is per category and off by default: reasonable for outage records and connection documents, wasteful for large drone videos.
- One copy only. Two simultaneous failures (both copies damaged) still lose availability; the scrubber's alert is what shortens that window.
- A repaired primary is trusted only after its SHA-256 matches the one recorded at ingest.
- The copy is written synchronously after the primary, so an upload to a policy with replicas takes longer; a crash between the two leaves a `pending` or absent copy that the scrubber fixes.

## Proof
`scripts/demo/self-heal.sh` (policy switch, damage the primary and download, damage it again and let the scrubber repair it, damage the copy, damage both) passed on the live stack; 34 unit tests (`test_replication.py`).
