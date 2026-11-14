# explanations.md — what is implemented, why, and how it works

Add a section for each feature when it lands. Format: **What / Why / How / How to prove it**.

## Slice A — skeleton + one category end to end (outage_records → S3)
**What:** `docker compose up` (via `./start.sh`) runs nginx (TLS, SPA, the only public port), the FastAPI backend, a metadata PostgreSQL and MinIO. Login → upload → list → download works over `https://localhost/api/v1`.
**Why:** it is the thinnest path that proves the architecture: gateway, auth, streaming storage, metadata, integrity.
**How it works:**
- `start.sh` generates `.env` with random secrets on first run (nothing secret in git; `.env.example` lists the keys).
- nginx generates a self-signed cert at container start, redirects 80→443, serves the React build (compiled with `VITE_USE_MOCK=false`) and proxies `/api/` to `backend:8000` using Docker DNS (so `--scale backend=N` works later). Uploads/downloads are not buffered by nginx.
- `TraceMiddleware` creates/propagates `X-Trace-Id`, returns it in the response and puts it in every JSON log line.
- `POST /ingest`: policy (from `policies.yaml`) picks the storage; the upload is read in 64 KB pieces, SHA-256 is computed on the fly while `S3Storage.put_stream` pushes it to MinIO (single `put_object` if <5 MB, otherwise multipart in 5 MB parts, so RAM stays bounded). Only after S3 confirms is the metadata row + audit entry written and `202 {file_id, trace_id, sha256, storage}` returned. If S3 is down: 503 and nothing is recorded.
- `GET /files/{id}/download`: re-hashes the stored bytes first; mismatch or missing object → `409` + `X-Integrity-Verified: false` and no bytes are sent; otherwise streams the file with `X-Integrity-Verified: true` and `X-File-Sha256`. `/verify` returns the same check as JSON. Every upload/download/verify is written to `audit_logs`.
- Auth: `POST /auth/login` → HS256 JWT (users from env); the simulator's `SIMULATOR_TOKEN` is accepted as role `service`.
- Tables (`files_metadata`, `audit_logs`, `meter_readings`, `policies_config`) are created with `CREATE TABLE IF NOT EXISTS` under an advisory lock.
**Prove it:** `scripts/demo/smoke.sh` (login, 401 without token, 12 MB upload, SHA match, byte-identical download, verify, tamper with the S3 object → 409). Unit tests: `cd backend && .venv/Scripts/python -m pytest -q tests`.
**Not yet:** FTP/Postgres storage, hot-reload policies, meter idempotency, encryption, RabbitMQ queues, metrics, resumable chunks.

## Slice B — three storages + policy engine with hot reload
**What:** FileGrid now has three real, separate storage services (MinIO for S3, a dedicated `postgres-blob` database, a pure-ftpd FTP server) behind one adapter interface, and the storage of each category is decided by a policy that an admin can change at runtime.
**Why:** PDF hard gates "FTP, S3 and DB each actually used as a separate service" and "policy configurable without code changes".
**How it works:**
- `storage/base.py` is the contract: `put_stream`, `get_stream`, `delete`, `healthy`. The routes never know which storage they talk to; they look it up by the name in the policy.
- `PostgresStorage` splits a file into 1 MB rows of `blob_chunks(key, seq, data)` inside one transaction (all chunks durable or none) and reads them back row by row, so memory stays bounded. It connects lazily: if that DB is down the backend still starts.
- `FtpStorage` opens a fresh FTP connection per operation (that is the reconnect), uploads to `<key>.part` and renames to `<key>` only when the upload finished, so a half-written file is never visible.
- `PolicyStore`: `policies.yaml` seeds the `policies_config` table on first start; afterwards the table is the truth. `PUT /admin/policies` (admin only) validates the document (JSON or YAML; unknown category, storage or priority → 422 and nothing is applied), saves it and applies it at once. Other backend replicas re-read the table within 2 s, so no restart is needed even when scaled out.
- Each file's metadata row remembers which storage it was written to, so changing a policy affects only new uploads; older files are still downloaded from where they are.
- `/health` returns HTTP 200 with `status: degraded` and a per-storage check list when a storage is down, so one dead storage does not get the backend restarted.
**Prove it:** `scripts/demo/policy-change.sh` routes the same category to postgres, ftp and s3 in turn, downloads byte-identically from each, checks the rows in postgres-blob and the file on the FTP disk, and shows the backend container was never restarted. Unit tests: `tests/test_slice_b.py`.
**Not yet:** meter-reading ingest (slice C), encryption for connection_docs (slice F), retry queue when a storage is down (slice G).

## Slice C — meter readings: never lost, never duplicated
**What:** the teammates' simulator now sends readings to the real backend through the TLS gateway, and each reading is stored exactly once no matter how many times it is retried or re-sent.
**Why:** PDF guarantee "no reading is lost and none is doubled (otherwise the bills are wrong)" and "retries do not duplicate meter readings".
**How it works** (`backend/meter_ingest.py`):
- A reading is identified by `(meter_id, timestamp)` read from the reading itself. The database has `UNIQUE (meter_id, reading_ts)`; that constraint, not application code, decides who wins, so it also holds with several backend replicas.
- Three steps: **claim** (insert the row as `pending`; if the insert conflicts, someone else has it) -> **store** the file in the storage the policy names -> **commit** the file's metadata row and flip the reading to `stored` in one transaction. Only then does the sender get `202`.
- Already stored -> `409`, which the simulator counts as "duplicate correctly rejected".
- If the storage write fails, the claim is deleted and the sender gets `503`; its retry is then treated as a new reading instead of a false duplicate.
- If a duplicate arrives while the original is still being written, it gets `503 Retry-After: 1` rather than `409`: we never tell a sender "stored" before it is. Its retry gets `409`.
- If a request dies between claim and commit, the `pending` claim is taken over by the next attempt after 5 seconds. Because the storage key is derived from the reading, repeating the write overwrites the same object.
- Voltage, zone and the simulator's `sim_label` are copied into columns of `meter_readings` (plus the full reading as JSONB) so the analytics slice can query them without opening files.
- The simulator verifies the gateway's TLS certificate: nginx publishes only its public certificate on a shared volume.
**Prove it:** `scripts/demo/duplicate-reading.sh` (re-sends 200 stored readings: all 409 and the row count does not move; sends one new reading 40 times in parallel: one `202`, one row). `scripts/sim.sh mode storm` raises the rate 10x. Unit tests: `tests/test_slice_c.py`.
**Limits today:** if the meter storage is down for longer than the simulator's own retries (about 6 s), the simulator drops the reading. Accepting and queueing readings during an outage is slice G.

## Documentation
Documentation written before code:
- `docs/SPEC.md` — requirement-to-proof traceability matrix (status of every PDF requirement).
- `docs/adr/` — decisions: stack, contract additions, TLS gateway, ingest status codes and dedup, frontend wiring, simulator wiring, storage mapping, core architecture.
- `CLAUDE.md` — ground rules; `progress.md` — running log.

## Target architecture (planned)
```
browser/simulator --TLS--> nginx (only public port)
                              | /api/v1  -> backend (FastAPI, stateless, N replicas)
                              | /        -> SPA static
backend --> postgres-meta   (files_metadata, meter_readings, audit_logs, policies_config, upload state)
        --> RabbitMQ        (q.meter_readings, q.outage_records, q.connection_docs, q.drone_videos + DLQ)
workers (per queue) --> storage adapters: postgres_storage | s3_storage (MinIO) | ftp_storage
observability: Prometheus <- /metrics ; Loki <- JSON logs (trace_id) ; Grafana ; Alertmanager -> webhook
```
Why: per-category queues + workers isolate a 10x spike in one category from the others; stateless backend scales with `--scale`; the policy decides the storage, so changing it needs no code.
