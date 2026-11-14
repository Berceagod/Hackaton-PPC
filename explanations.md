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

## Slice D — roles, audit, real file list, frontend on the real backend
**What:** the React UI runs against the real API (mock off): login, upload, list/search/filter, integrity-checked download, audit and policies all go through `https://localhost/api/v1`. Access is role based, and the public surface reveals nothing to anonymous callers.
**Why:** PDF hard rule 6 (UI to upload/search/download), §4 "access to FileGrid is authenticated", and the "connection docs auditable" guarantee.
**How it works:**
- Three roles. `admin` and `dispecer` log in (`POST /auth/login`, HS256 JWT, 24 h). The simulator uses `SIMULATOR_TOKEN` (role `service`) and may only send meter readings: if it leaked, it could not read any file. Only `admin` may change policies; the UI shows the Policies page read-only to a dispecer.
- JWTs are checked strictly: algorithm fixed to HS256 (no `none`, no algorithm chosen by the token), `exp/sub/role` required, unknown role -> 403. On any 401 the UI drops the session and returns to the login screen.
- `GET /files`: newest first, filters `category`, `storage`, search `q` (filename, trace id, zone), `limit`/`offset`. Meter readings are left out unless asked for (`category=meter_readings` or a search), so ~36,000 readings/hour do not bury manual uploads.
- Audit: every upload, download, verify and *blocked* download (corrupt file) is a row in `audit_logs` with user, time and trace id. The UI understands upload/download; the exact event is in `event`.
- Downloads send `Content-Disposition` per RFC 6266 (ASCII `filename` + UTF-8 `filename*`): a name like `raport_întrerupere_ștație.pdf` used to crash the response with a 500.
- Health: `/livez` (process alive, used by Docker) and `/health` (per-storage report) are open only on the internal network. Through nginx, `/api/v1/health` needs a logged-in user; the header badge polls it (Online / Degradat / Offline).
- The gateway's TLS key pair is kept in the `gateway-certs` volume, so re-creating nginx keeps the certificate the simulator trusts (before, every rebuild silently made the simulator drop readings).
**Prove it:** `cd backend && pytest -q tests` (`tests/test_slice_d.py`); in the browser log in as dispecer -> Policies shows "Doar citire"; upload a PDF with diacritics in its name and download it; `curl -k https://localhost/api/v1/health` -> 401; `docker compose up -d --force-recreate nginx` then `scripts/sim.sh status` -> `failed` stays 0.
**Not yet:** chunked upload for files > 5 MB (slice E), encrypted connection_docs (F), analytics endpoint (I).

## Slice E — chunked, resumable uploads; files > 1 GB on flat memory
**What:** drone videos and any file over 5 MB go up in 5 MB chunks. Each chunk is verified and stored as it arrives; a broken transfer resumes by sending only the missing chunks; the server assembles the file into the storage the policy names. A 1.2 GB file passes through a backend capped at 512 MB.
**Why:** PDF §4 "files > 1 GB without the service's resources growing with the size", "transfers finish or resume on slow/unstable links", and the drone-video guarantee in §2.1.
**How it works** (`backend/chunked_upload.py`, ADR-0010):
- `POST /upload/chunk` carries `upload_id` (UUID chosen by the browser), the chunk index/total, the file's name/category/metadata and optionally the whole-file `sha256`/`size_bytes` and the chunk's own `chunk_sha256`.
- The chunk is streamed (64 KB reads) to S3 as `_staging/<upload_id>/<index>` and recorded in `upload_chunks`. Same index again = same key and row, so retries never duplicate. A chunk whose SHA-256 differs from `chunk_sha256` is thrown away (422) and the client resends it. Chunks may arrive in any order and on any backend replica.
- `GET /upload/{id}` lists `received` and `missing` chunks, so a client that lost its connection resends only what is missing.
- When the last missing chunk lands, exactly one request wins the `open -> assembling` status change in the database and streams the chunks in order into the target storage (S3 multipart, FTP or Postgres), computing SHA-256 on the way. If the client declared a size or SHA and it does not match, nothing is kept. If the target storage is down, the chunks stay staged and resending the last chunk retries. Then the file row, the session's `complete` status and an audit entry are written, and the staging copies are deleted. A chunk resent after that gets the same answer.
- The browser no longer hashes a > 512 MB file in memory (it cannot do so in pieces); every chunk is still verified and the server hashes the whole file.
**Prove it:** `scripts/demo/large-file-1gb.sh` (default 1200 MB): uploads all chunks but one, shows the server reporting it missing, resends it, compares SHA-256 and the downloaded bytes, and prints the backend's memory (78 -> 107 MiB under a 512 MB limit). Unit tests: `tests/test_slice_e.py`.
**Not yet:** cleanup of abandoned uploads; resume after a browser page reload in the UI; toxiproxy bad-network demo (slice G).

## Slice F — encryption at rest and security hardening
**What:** personal data (connection documents, and any category whose policy says `encryption: true`) is encrypted with AES-256-GCM inside the backend before it reaches FTP, S3 or Postgres; storages cannot be reached from outside; no password or key lives in the repository; login is rate limited.
**Why:** PDF §4 Security (storages only reachable through FileGrid; unauthorized direct storage access exposes no personal data; no password or key in the repo) and the §2.1 guarantee for connection documents. It also unblocks hard rule 1: all four categories are now stored and retrieved.
**How it works:**
- `crypto_envelope.py` encrypts in 64 KB segments, each sealed with AES-256-GCM. The segment number and a "last segment" flag are bound into each seal, so cutting, reordering, flipping a bit or moving the ciphertext to another file is detected before any byte is released. Memory stays one segment, so 1 GB documents still stream.
- Each file has its own key, derived (HKDF-SHA256) from the master key and the file id. The master key is 32 random bytes generated by the start script into `.env` and given only to the backend container. A stolen storage or metadata database decrypts nothing.
- Upload, download, verify, chunked upload (whose temporary chunks are encrypted too) and meter readings all go through it when the policy asks. The SHA-256 the user sees is that of the original file; a tampered ciphertext gives `409 decryption_failed` and an audit entry.
- Network: only nginx publishes ports; every storage lives on an `internal: true` Docker network. nginx sends HSTS, nosniff, X-Frame-Options and Referrer-Policy and limits login to 20 attempts/minute per IP.
- Secrets: `.env` holds everything secret and is git-ignored. The UI passwords are random per machine and printed by `start.sh` / `start.ps1`. `secrets-scan.sh` runs gitleaks over the history and the working tree.
**Prove it:** `scripts/demo/unauthorized-direct-storage-access.sh` (ports closed; storages unreachable from the public network; on each storage the raw connection document has no CNP/name while a plain outage record does; FileGrid returns the exact original; the key is only in the backend). `scripts/security/secrets-scan.sh`. Unit tests: `tests/test_slice_f.py`.
**Not yet:** key rotation; Kubernetes NetworkPolicy (slice J).

## Slice G — isolation between categories and resilience to failures
**What:** a 10x spike in one category does not slow the others; a storage going down stops nothing else and loses no confirmed file; uploads and meter readings get through a slow, lossy network by themselves, never duplicated.
**Why:** PDF §4 Scalabilitate (10x spike) and Reziliență (storage down, bad network, retries without duplicates).
**How it works:**
- *Outbox for meter readings* (`meter_outbox.py`): if the reading's storage is down, its exact bytes are kept in the metadata DB as `queued` and the meter gets 202. A worker writes queued readings out as soon as the storage answers again, then they are `stored` like any other. Nothing confirmed is ever lost; a resend is a 409.
- *Circuit breaker* per storage: after 3 failures in a row calls fail immediately for 5 s instead of waiting on timeouts, so a dead storage does not hold request slots.
- *Bulkheads*: every category has its own concurrency limit (by policy priority) and meter readings have their own DB connection pool; the backend runs 4 worker processes so CPU-heavy uploads cannot starve other categories.
- *Policy changes* are pushed to every worker at once with Postgres NOTIFY.
- *Meters keep what they could not send*: the simulator stores undelivered readings in a backlog and resends them until FileGrid confirms.
- *Bad network*: toxiproxy between nginx and the backend can add latency, cap bandwidth and cut connections on demand.
- Abandoned chunked uploads are deleted after 24 h without activity.
**Prove it:** `scripts/demo/kill-storage.sh`, `scripts/demo/spike-10x.sh`, `scripts/demo/slow-network.sh`; unit tests `tests/test_slice_g.py`.
**Not yet:** queueing for UI categories when their storage is down (they get 503 and retry); a persistent simulator backlog.

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
