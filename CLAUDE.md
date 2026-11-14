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
- **NEVER commit or push without the user's explicit say-so.** Never push to `main`, never force-push. Slices H, I, J are built in parallel on separate branches and machines: follow **"Parallel slices H, I, J"** below.
- Teammates' code (`frontend/`, `simulator/`, `API_CONTRACT.md`, `policies.yaml`) is owned by others: minimal, justified edits only, each logged in `progress.md`.
- No secrets in git: `.env.example` only; secrets generated at first start; gitleaks check.
- No host-published ports except nginx. Storages live on the internal Docker network.
- Never buffer a whole file in RAM; stream (64 KB reads, 5 MB chunks).
- Test data is synthetic. Never real personal data.
- TDD per slice; no failing tests; slices from `docs/SPEC.md`; each slice leaves `docker compose up` working.
- Ask the user only about blocking decisions; otherwise pick a sensible default and record an ADR in `docs/adr/`.
- After every meaningful step, update `progress.md`. After every implemented feature, update `explanations.md`. Keep `docs/SPEC.md` statuses current. **Exception on slice branches H, I, J:** write to `docs/slices/<X>.md` instead (see below).
- README is in Romanian (clean-machine quickstart, demo walkthrough in the order of the PDF checklist).

## Parallel slices H, I, J (3 machines, 3 branches) — merge-safety rules
Three people build slices H, I and J at the same time, each on their own machine and branch. These rules exist so the three branches merge back with no conflicts, or only trivial ones. They bind every session on every branch. If a task seems to need breaking one, stop and ask the user (who agrees it with the other two) before writing code.

### 0. Setup (once per machine)
- Base: `main` at the commit that contains these rules and the `slice H/I/J` marker comments. Before branching: `git checkout main && git pull`. Do not start from an older commit.
- Branches: H -> `feat/slice-h-observability`, I -> `feat/slice-i-analytics`, J -> `feat/slice-j-scale`.
- Each machine runs `./start.sh` (or `start.ps1`) once. It generates that machine's own `.env` (passwords, master key). `.env` is never copied between machines, never committed.
- Baseline check before writing code: backend tests green, `scripts/demo/smoke.sh` and `scripts/demo/kill-storage.sh` pass.

### 1. Scope of each slice (from `docs/SPEC.md`)
- **H (observability)**: O1 latency p50/p95/p99, throughput, error rate per category AND storage; O2 one operation traceable request -> storage (`X-Trace-Id` in metrics/logs, Loki); O3 automatic alerts when a business guarantee (see "Business guarantees" above) is at risk. Prometheus + Grafana (as code, provisioned) + Loki (+ log collector) + Alertmanager. Demos: `scripts/demo/trace-one-request.sh`, `scripts/demo/alert-fire.sh`.
- **I (analytics)**: T1 best storage per category and why; T2 normal day vs peak day; T3 what the data says about the network (meter anomalies vs `sim_label`, zones with most outages). Real `GET /api/v1/analytics/summary` (shape = `AnalyticsSummary` in `frontend/src/types/filegrid.ts`), benchmark and scenario scripts, the frontend Analytics tab on real data.
- **J (scale-out)**: S2 capacity grows by adding instances without code changes (`docker compose up -d --scale backend=N` demo); Kubernetes manifests (Deployments, Services, HPA, NetworkPolicy keeping storages unreachable: X1 for k8s).

### 2. File ownership
**Owned (only that slice creates or edits them):**

| Slice | Owns |
|---|---|
| H | `backend/metrics.py` (+ any `backend/observability*.py`), `infrastructure/prometheus/`, `infrastructure/grafana/`, `infrastructure/loki/`, `infrastructure/alloy/` (or promtail), `infrastructure/alertmanager/`, `infrastructure/nginx/default.conf` (e.g. `/grafana/`), `scripts/demo/trace-one-request.sh`, `scripts/demo/alert-fire.sh`, `backend/tests/test_slice_h.py` |
| I | `backend/analytics.py`, `backend/routes/analytics.py`, `scripts/benchmark/`, `scripts/scenarios/`, everything under `frontend/` (Analytics page, `api.ts`, `filegrid.ts`), `API_CONTRACT.md`, `backend/tests/test_slice_i.py` |
| J | `k8s/`, `scripts/demo/scale-out.sh`, the `backend:` service definition in `docker-compose.yml` (only if scaling needs it), `backend/tests/test_slice_j.py` |

**Instrumentation of existing code is H only:** `backend/routes/files.py`, `backend/routes/uploads.py`, `backend/meter_ingest.py`, `backend/meter_outbox.py`, `backend/chunked_upload.py`, `backend/storage/*`, `backend/bulkhead.py`, `backend/logging_setup.py`, `backend/tracing.py`. I and J do not edit them. If I or J needs a change there, write it in `docs/slices/<X>.md` under "Requests to other slices" and agree it with H first.

**Nobody edits on a slice branch:** `progress.md`, `explanations.md`, `docs/SPEC.md`, `README.md`, `policies.yaml`, `simulator/`, existing ADRs, the existing tests of slices A-G (`backend/tests/test_slice_[a-g].py`), and this file. The integrator updates the shared docs at merge time (section 6).

### 3. Shared files: additive only, under your own marker
These files carry comment markers `slice H (...)`, `slice I (...)`, `slice J (...)`. Add your lines **directly below your own slice's marker** and nowhere else. Never edit, move, reorder, reformat or delete existing lines (including the other slices' markers). That keeps each branch's change in its own hunk, so git merges them without conflicts.

| File | What goes under your marker |
|---|---|
| `docker-compose.yml` | backend env lines (`x-backend-env`), new services (end of `services:`), new volumes (end of `volumes:`) |
| `backend/main.py` | imports; middleware / app-level routes (e.g. `/metrics`); `app.include_router(...)` under `/api/v1`; background tasks (`tasks.append(...)`) |
| `backend/config.py` | new `Settings` fields (**always with a default**, so no other slice's tests break) and their `from_env` lines |
| `backend/requirements.txt` | new pinned dependencies (`name==x.y.z`, a version at least 2 weeks old) |
| `backend/repo.py` | new `Repo` protocol methods; new `PostgresRepo` methods (end of file). Schema: new tables or columns only with `CREATE ... IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` run from your own new method or SQL constant, never by editing `SCHEMA` |
| `backend/tests/conftest.py` | new `FakeRepo` methods; new fixtures (end of file). Existing fakes and fixtures are not changed |
| `.env.example` | new optional settings, commented, with their defaults |
| `start.sh` **and** `start.ps1` | new generated secrets (`ensure KEY "$(rand)"` / `Ensure KEY (Rand)`), always in both files |

No other shared file may be edited. A file not listed in sections 2 or 3 belongs to nobody on a slice branch.

### 4. Interfaces agreed in advance (so I does not wait for H, and J does not break H)
**Metrics contract (H produces, I consumes). Names, types and labels are fixed:**

| Metric | Type | Labels |
|---|---|---|
| `filegrid_requests_total` | counter | `category`, `storage`, `operation`, `outcome` |
| `filegrid_request_duration_seconds` | histogram | `category`, `storage`, `operation` |
| `filegrid_bytes_total` | counter (plaintext bytes) | `category`, `storage`, `operation` |
| `filegrid_storage_operation_duration_seconds` | histogram | `storage`, `op` (`put`/`get`/`delete`), `outcome` (`ok`/`error`) |
| `filegrid_integrity_failures_total` | counter | `category`, `storage`, `reason` |
| `filegrid_bulkhead_rejections_total` | counter | `category` |
| `filegrid_outbox_queued` | gauge | none (meter readings waiting in the outbox) |
| `filegrid_breaker_open` | gauge (0/1) | `storage` |

- Label values: `category` is one of the 4 categories; `storage` is `postgres`, `s3`, `ftp`, or `none` (rejected before any storage); `operation` is `ingest`, `chunk`, `download`, `verify` or `drain`; `outcome` is `ok`, `duplicate`, `queued`, `client_error`, `server_error` or `integrity_error`.
- Histogram buckets (seconds): `0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60`.
- The backend runs several Uvicorn workers: H uses prometheus_client **multiprocess mode**, so `/metrics` aggregates all workers. `/metrics` stays on the internal root path (nginx keeps answering 404 for it).
- The simulator's existing `sim_*` metrics (port 8080, `/metrics`) are scraped as they are; their names are not changed.
- **Prometheus**: compose service `prometheus`, internal port `9090`, no host port, scrape interval 5 s, retention of at least 24 h. Job `backend` uses **DNS service discovery** (`dns_sd_configs`, name `backend`, type A, port 8000), so `--scale backend=N` (J) is picked up with no config change. Job `simulator` targets `simulator:8080`.
- **I reads metrics** only through `PROMETHEUS_URL` (default `http://prometheus:9090`, I's setting) with the PromQL HTTP API and the names above. Until H is merged, I tests against a fake Prometheus. If Prometheus is unreachable, `/analytics/summary` still answers 200 with the data it has (never 500).
- **Grafana** is reached only through nginx at `https://localhost/grafana/` (H edits nginx). Its admin password is generated by the start scripts (`GRAFANA_ADMIN_PASSWORD`, under H's marker).
- **Scaling rules for everyone:** the compose service stays named `backend` on port 8000; no `container_name:` and no `ports:` on `backend`; the backend stays stateless (no local files, no in-memory state another replica would need). J does not rename services that H scrapes.
- **API**: I owns `/api/v1/analytics/*` and its section of `API_CONTRACT.md`. H and J add no `/api/v1` endpoint. Existing endpoints and response shapes are frozen.
- **Database**: only I adds read queries for analytics (`meter_readings`, `files_metadata`, `audit_logs`). Nobody alters the columns of existing tables; new objects only as in section 3.
- **Ports**: no new host-published ports. Only nginx publishes 80/443.

### 5. Numbers and names reserved
- ADRs: H `0014`-`0019`, I `0020`-`0025`, J `0026`-`0031`.
- Tests: `backend/tests/test_slice_<x>.py` only. Do not edit other slices' test files.
- Scripts: `scripts/demo/<name>.sh` only with the names in section 2, plus slice-prefixed helpers (`scripts/<x>-*.sh`).
- New env vars have a clear owner: H `PROMETHEUS_*` (except `PROMETHEUS_URL`), `GRAFANA_*`, `LOKI_*`, `ALERT*`; I `PROMETHEUS_URL`, `ANALYTICS_*`, `BENCHMARK_*`; J `SCALE_*`, `K8S_*`.

### 6. Notes, commits and merging
- Each branch keeps its own log in `docs/slices/<X>.md` (`H.md`, `I.md`, `J.md`): what was built, why, how to prove it (exact commands), the status line for each of its `docs/SPEC.md` rows, every shared-file edit (section 3), known gaps, and "Requests to other slices".
- Commit often on your own branch and push only your own branch, with the user's say-so. Never push to `main`. Merges go through pull requests.
- **Merge order: H, then J, then I.** Before opening its pull request, a branch merges the latest `main` into itself on its own machine, resolves any conflicts there, and runs: the backend tests (all slices), `smoke.sh`, `policy-change.sh`, `duplicate-reading.sh`, `kill-storage.sh`, `spike-10x.sh`, `unauthorized-direct-storage-access.sh`, `scripts/security/secrets-scan.sh`, plus its own demos.
- **Integrator** (one agreed person; default: Gabriel) merges each pull request. After each merge the integrator folds `docs/slices/<X>.md` into `progress.md`, `explanations.md` and `docs/SPEC.md`, reruns the full demo list on `main`, and tells the other branches to merge `main`.
- To finish after merging: once both H and J are in, Prometheus must see N backend replicas (the `scale-out.sh` demo shows them in Grafana). Once I is in, the Analytics tab shows real H metrics.

### 7. Windows and tooling pitfalls already paid for (all machines)
- Do not write files with Python `Path.write_text` on Windows: it converts line endings to CRLF. Use the Edit tool or `write_bytes`. `.sh` files stay LF; `start.ps1` / `start.cmd` are CRLF by `.gitattributes`.
- Git Bash: with `MSYS_NO_PATHCONV=1` exported, native `curl.exe` needs Windows paths (`cygpath -m`) for `-o` / `-D`, and never `/dev/null`.
- With `set -o pipefail`, never pipe into `head` or `grep -q` from a producer that keeps writing; write to a file first.
- Never run token-handling scripts with `bash -x`: it prints secrets.
- Backend tests run on Python 3.12 (the host may lack asyncpg wheels): `docker run --rm -v "<repo>/backend:/src:ro" python:3.12-slim sh -c "cp -r /src /app && cd /app && pip install -q -r requirements-dev.txt && python -m pytest -q tests"`.
- A native PostgreSQL on the host's port 5432 is not FileGrid; the demos tell them apart.
