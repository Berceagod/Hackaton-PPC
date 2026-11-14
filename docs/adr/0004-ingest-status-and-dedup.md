# ADR-0004: Ingest status codes, idempotency and deduplication
Status: accepted (updated in slice C)

## Meter readings (never lost, never duplicated)
Identity = `(meter_id, timestamp)` taken from the reading itself; the `Idempotency-Key` header (`{meter_id}:{timestamp}`) is optional and, if present, must match (else 422). `UNIQUE (meter_id, reading_ts)` on `meter_readings` is the only arbiter (no check-then-insert), so it holds across any number of backend replicas.

Flow: claim (`INSERT ... ON CONFLICT DO NOTHING`, status `pending`) -> write the file to the policy's storage under a deterministic key -> one transaction: `files_metadata` row + status `stored` -> `202`.

| Situation | Response |
|---|---|
| new reading, stored durably | 202 `{file_id, trace_id, sha256, storage}` |
| reading already `stored` | 409 `{error: duplicate_reading, file_id, ...}` (what the simulator counts as "duplicate rejected") |
| storage or metadata write failed | claim released, 503 (sender retries and is treated as new) |
| same reading still in flight in another request | 503 + `Retry-After: 1` (never report "stored" before it is; the retry gets 409) |
| claim left `pending` by a crashed request | taken over after 5 s by the next attempt |
| malformed reading / bad meter_id / header mismatch | 422; larger than 1 MB -> 413 |

Storage writes for a reading are idempotent (same key, same bytes), so a takeover racing a slow original is harmless.
`deduplicate` in the meter_readings policy is not consulted: idempotency of readings is a business guarantee, not an option.

## Other categories
`deduplicate: true` -> after the upload is hashed, if the same sha256 already exists in that category the new object is removed and the answer is 409 `{error: duplicate_file, file_id: <existing>}`. This is best effort (no unique constraint): two identical uploads racing each other can both be kept. Default policies have `deduplicate: false` for the UI categories, so the frontend never sees this unless an admin turns it on.

## Not supported
The simulator's optional `INGEST_MODE=json` (raw JSON body) is not implemented; compose pins `INGEST_MODE=multipart`, which is what API_CONTRACT.md describes.
