# CLAUDE.md — FileGrid PPC ground rules

Read this file first, then `progress.md` (where we are). `explanations.md` says what exists and why.
Don't re-read the whole conversation; these three files are the memory.

## Project in one paragraph
FileGrid is the single entry point for field files of a Romanian power distributor (Muntenia, Banat, Dobrogea). Files enter only through our app. FileGrid decides per data category, by configurable policy, where and how each file is stored (FTP, S3, or database), verifies integrity, and measures everything. It is graded on a **live demo**: every claim must be provable with a script. Source of truth: `FileGrid PPC.pdf` (Romanian) and `ecc-prompt-v2.md` (`C:\Users\rauce\Desktop\PPC\`).

## Hard rules from the hackathon PDF (project is NOT evaluated without all)
1. Store and retrieve files of all four categories: `meter_readings`, `outage_records`, `connection_docs`, `drone_videos`.
2. FTP, S3 and database storage each actually used, each a **separate service** (S3 = MinIO).
3. Per-category policy configurable **without code changes** (hot reload).
4. Retrieved file is byte-identical to the stored one and FileGrid can **prove** it (SHA-256).
5. Whole system starts on a clean machine with **ONE command**, then README.
6. UI to upload / search / download manually-uploaded categories.
7. Meter simulator with configurable volume (normal day and peak day).
8. All code (app, simulator, configs, scripts) in the Git repo.

Non-functional (must be demonstrable live): a 10x spike in one category does not degrade others; capacity grows by adding instances (no code change); files >1 GB with resources NOT proportional to size; latency p50/p95/p99, throughput, error rate per category AND storage; one operation traceable request to storage; automatic alerts when a business guarantee is at risk; storages not reachable from outside; external traffic encrypted and access authenticated; unauthorized direct storage access exposes no PII; **no password or key in the repo**; a storage going down loses no confirmed file and stops no other category; transfers resume on bad networks; retries never duplicate a meter reading; corrupt file detected on retrieval.

Business guarantees: meter readings never lost/duplicated; outage records immediately available; connection docs PII-protected and auditable; drone videos survive slow/unstable links.

## Stack (decided; the newer Python prompt overrides the earlier Spring Boot plan — ADR-0001)
- Backend: **Python 3.11+, FastAPI + Uvicorn**, in `backend/`. asyncpg, aioboto3/boto3, aioftp (or ftplib in threadpool), prometheus_client, PyJWT, `cryptography` (AES-256-GCM), pytest (+ testcontainers-python).
- Frontend: React 19 + TS + Vite + Tailwind (teammates' code, keep it).
- Simulator: Python/FastAPI (`simulator/`, teammates' code).
- Infra: Docker Compose, nginx (TLS, only public entry), PostgreSQL (metadata) + separate PostgreSQL (blob storage), MinIO, FTP server, a Postgres outbox + bulkheads + circuit breakers (ADR-0013 replaced RabbitMQ), Prometheus, Grafana, Loki, Alertmanager, toxiproxy, Kubernetes manifests (HPA, NetworkPolicy).
- Contract: `API_CONTRACT.md` (prefix `/api/v1`) + `frontend/src/types/filegrid.ts` JSON shapes are law. Policies: `policies.yaml`.

## Working rules
- **NEVER commit or push without the user's explicit say-so.** Work stays uncommitted on branch `feat/backend-infra` (branched from `main`). Never push to `main`, never force-push.
- Teammates' code (`frontend/`, `simulator/`, `API_CONTRACT.md`, `policies.yaml`) is owned by others: minimal, justified edits only, each logged in `progress.md`.
- No secrets in git: `.env.example` only; secrets generated at first start; gitleaks check.
- No host-published ports except nginx. Storages live on the internal Docker network.
- Never buffer a whole file in RAM; stream (64 KB reads, 5 MB chunks).
- Test data is synthetic. Never real personal data.
- TDD per slice; no failing tests; slices from `docs/SPEC.md`; each slice leaves `docker compose up` working.
- Ask the user only about blocking decisions; otherwise pick a sensible default and record an ADR in `docs/adr/`.
- After every meaningful step, update `progress.md`. After every implemented feature, update `explanations.md`. Keep `docs/SPEC.md` statuses current.
- README is in Romanian (clean-machine quickstart, demo walkthrough in the order of the PDF checklist).
