# progress.md — running log

Update after every meaningful step. Newest entries at the bottom of "Log". Nothing is committed unless the user says so.

## Status
- Branch: `feat/backend-infra` (from `main`). **No commits made.** No pushes.
- Current slice: **A, B and C DONE (uncommitted, awaiting review/commit say-so)**. Next: slice D (auth roles, audit endpoint, real /files list, frontend against the real backend, contract update).
- Slice C proof: `backend` pytest 54/54 green; `scripts/demo/duplicate-reading.sh` passes (burst of 200 already-stored readings -> all 409, 0 new rows in meter_readings and files_metadata; one new reading sent 40x in parallel -> exactly one 202, the rest 409/503, one row; download byte-identical). Live: simulator runs through `https://nginx` with TLS verification, normal 10/s and storm 100/s for 25 s with 0 failed / 0 dropped / 0 pending / 0 duplicate rows over 4,345 readings. With postgres-blob stopped: reading -> 503 and no row left; S3 ingest still 202; after restart the same reading -> 202, then 409.
- Slice B proof: `backend` pytest 32/32 green; `scripts/demo/policy-change.sh` passes (dispecer PUT → 403, invalid → 422, outage_records routed to postgres → ftp → s3 by PUT /admin/policies with no restart, byte-identical download from each, rows in postgres-blob, file on FTP disk, policy persisted); `scripts/demo/smoke.sh` still passes; with `docker compose stop ftp` health is `degraded` (HTTP 200), S3 ingest still 202, FTP-routed ingest 503.
- Slice A proof: `backend` pytest 14/14 green (`cd backend && .venv/Scripts/python -m pytest -q tests`); `./start.sh` brings up nginx(TLS)+backend+postgres-meta+minio healthy; `scripts/demo/smoke.sh` passes (login, 401 w/o token, 12 MB ingest 202 with sha match, list, byte-identical download, verify, tamper → 409).

## Stack change (2026-11-14)
User supplied a second prompt on top of `ecc-prompt-v2.md`: backend is **Python FastAPI** (not Spring Boot) with **RabbitMQ** per-category queues, asyncpg, aioboto3, aioftp, prometheus_client. The rest of the original plan (slices, demo scripts, infra, docs) stays. Spring-specific wording in the original (Micrometer, Resilience4j, Testcontainers-Java, Maven) maps to: prometheus_client, circuit breaker in Python, testcontainers-python, pip/requirements.txt.

## Slice plan (unchanged order)
A skeleton + compose + 1 category e2e · B StorageBackend x3 + policy engine · C meter idempotency + simulator · D auth/audit/list/download/frontend live · E chunked/resumable + streaming · F encryption + security hardening · G isolation (RabbitMQ queues/workers) + resilience · H observability + alerts · I analytics/benchmark/scenarios · J k8s + scale · K docs/README.

## Open design points
- Large bodies and RabbitMQ: queue messages for `/ingest` (≤10 MB) can carry a reference to a staged object; chunked/large uploads must bypass the broker body and stream directly to storage. Decide in slice A/B and record an ADR.
- "Ack only after durable write": a persistent RabbitMQ message with publisher confirm counts as durable for `/ingest`; chunk path acks per chunk after durable chunk write.
- Dedup 409 semantics: ADR-0004.

## Log
- Read PDF (4 pages), API_CONTRACT.md, policies.yaml, frontend `api.ts` + `filegrid.ts`, nginx.conf, frontend Dockerfile, simulator env vars. Repo state confirmed: backend/, infrastructure/, docker-compose.yml, start.sh empty.
- Created branch `feat/backend-infra`.
- Wrote `docs/SPEC.md` (traceability matrix) and `docs/adr/0001..0008`.
- Wrote `CLAUDE.md`, `progress.md`, `explanations.md`.
- Updated ADR-0001/0008 and SPEC O1 for the Python stack.
- Slice A built: `backend/` (FastAPI: config, logging_setup, tracing, policies, auth, repo, integrity, deps, routes/{health,auth_routes,files}, storage/{base,s3_storage}, tests), `infrastructure/nginx/` (TLS gateway + SPA build with VITE_USE_MOCK=false), `infrastructure/minio/` (built from source, ADR-0009), `docker-compose.yml`, `start.sh`, `.env.example`, root `.dockerignore`, `scripts/demo/smoke.sh`.
- Docker Desktop was not running; I launched it. MinIO images are gone from Docker Hub/quay → ADR-0009.
- Slice A deviations from plan: compose has only nginx/backend/postgres-meta/minio (postgres-blob, ftp, rabbitmq arrive in B/G where first used). Simulator not wired yet (slice C). `/metrics` not yet implemented (slice H; gateway already 404s it).
- Windows note: Git Bash curl needs `cygpath -m` paths inside `-F file=@...` (handled in smoke.sh).
- Slice B built: `backend/storage/postgres_storage.py` (1 MB rows in `blob_chunks`, one transaction per file, lazy connect), `backend/storage/ftp_storage.py` (aioftp, `.part` + rename, connection per operation), `backend/policies.py` rewritten (validation, DB persistence, 2 s TTL refresh), `backend/routes/admin.py` (GET/PUT /admin/policies), repo `load_policies/save_policies`, config + main wiring, `tests/test_slice_b.py`; compose services `postgres-blob` and `ftp` (stilliard/pure-ftpd) with volumes; `start.sh` now adds any missing `.env` key without touching existing ones; `.env.example` extended; `scripts/demo/policy-change.sh`; ADR-0007 updated.
- Behaviour changes in B: `/health` is now always HTTP 200 with `status: ok|degraded` (one storage down must not get the backend restarted); S3 `healthy()` creates the bucket if missing; S3 not ready at startup is a warning, not a crash; `meter_readings` ingest returns 501 until slice C; `encryption: true` categories (connection_docs by default) return 501 until slice F.
- Known gaps after B: connection_docs cannot be stored with the default policy until slice F (encryption); FTP is therefore only exercised by changing a policy; `ftp` has no healthcheck in compose; tamper → 409 is only demonstrated live on S3; storage adapters have no direct unit tests (covered by the live demo script against real services).
- Slice C built: `backend/meter_ingest.py` (claim -> store -> complete), repo methods `claim_reading/complete_reading/release_reading/find_by_sha`, `meter_readings.status` + `claimed_at` columns (idempotent ALTER for existing DBs), sha256 dedup for `deduplicate: true` categories, `Content-Disposition` filename sanitising, `tests/test_slice_c.py`; compose service `simulator` + `gateway-ca` volume (public cert only) and `40-gen-cert.sh` publishing it; `scripts/sim.sh` (status/mode/rate/pause/resume/dupes/burst); `scripts/demo/duplicate-reading.sh`; ADR-0004 and ADR-0006 rewritten; `.env.example` documents `SIM_*`.
- Simulator code untouched. It verifies the gateway certificate (`VERIFY_TLS=/certs/tls.crt`) instead of disabling checks.
- Incident: a `bash -x` trace I ran printed `SIMULATOR_TOKEN` into the session output. Rotated it right away (deleted the line from `.env`, re-ran `./start.sh`); simulator confirmed working with the new token. Avoid `bash -x` on scripts that handle tokens.
- Windows notes: do NOT export `MSYS_NO_PATHCONV=1` globally in a script that also uses `curl -o /dev/null` (curl exit 23); only `scripts/sim.sh` sets it. Parallel `>>` appends to one file lose lines on Git Bash; write one file per job.
- Known gaps after C: (1) the simulator gives up after 5 retries (~6 s), so an outage of the meter storage longer than that loses readings at the sender; buffering accepted readings while a storage is down is slice G (queue/outbox). (2) `/files` with no category filter is now dominated by meter readings (one metadata row per reading); fix in slice D (e.g. cap per category). (3) `API_CONTRACT.md` not yet updated with the new endpoints and the 409/503 semantics; do in slice D. (4) simulator `INGEST_MODE=json` unsupported. (5) meter readings write no per-reading audit rows (volume); audit matters for connection_docs.
- TODO next: not yet verified in a browser (UI login/upload against real backend) — do in slice D; read `simulator/sim.py` in detail before slice C; pin `MINIO_REF` before final demo.

## Edits to teammates' code
(none yet)
