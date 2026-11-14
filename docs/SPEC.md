# FileGrid — SPEC & traceability matrix

Source of truth: `FileGrid PPC.pdf` (RO). Build brief: `ecc-prompt-v2.md`.
Status legend: TODO / PARTIAL / DONE. Every row ends with a **proof command** (filled in as slices land).

## 1. Data categories & guarantees (PDF §2.1)

| Category | Entry | Size | Volume | Guarantee | Default storage |
|---|---|---|---|---|---|
| meter_readings | simulator | 1–50 KB | very many | never lost / never duplicated | postgres |
| outage_records | UI | 1–10 MB | spike on storms | immediately available | s3 |
| connection_docs | UI | 0.1–10 MB | moderate | PII protected + auditable | ftp + AES-256-GCM |
| drone_videos | UI | 100 MB–1 GB+ | few, huge | transfer survives bad links | s3 (multipart) |

## 2. Requirement matrix

| # | Requirement (PDF §) | Design element | Slice | Proof command | Status |
|---|---|---|---|---|---|
| F1 | Store+retrieve 4 categories (§3) | `/ingest`, `/upload/chunk`, `/files/{id}/download` | A,B,E | `scripts/demo/smoke.sh` | PARTIAL (outage_records on all 3 storages; meter_readings stored+retrieved; docs=slice F, big video=slice E) |
| F2 | FTP, S3, DB each real & separate (§3) | `StorageBackend` x3; services `ftp`, `minio`, `postgres-blob` | B | `docker compose ps` + benchmark | DONE (`scripts/demo/policy-change.sh`: postgres-blob, ftp, minio are separate services, each written and read back) |
| F3 | Policy w/o code change (§3) | `policies.yaml` + `PUT /admin/policies` hot reload | B | `scripts/demo/policy-change.sh` | DONE (`scripts/demo/policy-change.sh`: PUT /admin/policies, no restart, persisted in DB) |
| F4 | Retrieved == stored, provable (§3) | SHA-256 at ingest, verified on read, `/files/{id}/verify` | A,D | `scripts/demo/corrupt-file.sh` | PARTIAL (proven on all 3 storages for download; tamper→409 proven on S3) |
| F5 | One command start + README (§3) | `start.sh`, compose, `.env` generated | A,K | `./start.sh` on clean clone | PARTIAL (start.sh works; README pending) |
| F6 | UI upload/search/download (§3) | existing React app, mock off | D | browser walkthrough | TODO |
| F7 | Simulator, configurable volume (§3) | existing simulator wired to gateway | C | `docker compose logs simulator` | DONE (`scripts/demo/duplicate-reading.sh`, `scripts/sim.sh`; volume via SIM_* env and runtime control; normal 10/s and storm 100/s run with 0 failed) |
| F8 | All code in Git (§3) | this repo | all | `git status` clean | TODO |
| S1 | 10x spike in one category doesn't hurt others (§4) | per-category executors/queues/bulkheads, priority | G | `scripts/demo/spike-10x.sh` | TODO |
| S2 | Scale by adding instances (§4) | stateless backend, `--scale backend=N`, k8s HPA | J | `scripts/demo/scale-out.sh` | TODO |
| S3 | >1 GB with flat resources (§4) | streaming, S3 multipart, no buffering | E | `scripts/demo/large-file-1gb.sh` | TODO |
| O1 | p50/95/99, throughput, error rate per category×storage | prometheus_client → Prometheus → Grafana as code | H | Grafana dashboard | TODO |
| O2 | One operation traceable end to end | `X-Trace-Id` in logs/spans/metrics exemplars → Loki | H | `scripts/demo/trace-one-request.sh` | TODO |
| O3 | Auto-alert when a 2.1 guarantee is at risk | Prometheus rules + Alertmanager webhook | H | `scripts/demo/alert-fire.sh` | TODO |
| X1 | Storages not reachable externally | internal docker network, no host ports; k8s NetworkPolicy | F,J | `scripts/demo/unauthorized-direct-storage-access.sh` | PARTIAL (nginx only host port; storage demo script pending) |
| X2 | External traffic encrypted, access authenticated | nginx TLS (self-signed at start), JWT + roles + service token | D,F | `curl -k` with/without token | PARTIAL (TLS+JWT live; roles for admin routes pending) |
| X3 | Unauthorized direct storage access exposes no PII | AES-256-GCM envelope encryption before storage + audit | F | same as X1 | TODO |
| X4 | No secrets in repo | `.env.example`, secrets generated at first start, gitleaks | F | `gitleaks detect` | TODO |
| R1 | Storage down ≠ other categories down, no confirmed loss | ack after durable write, Resilience4j, outbox/retry | G | `scripts/demo/kill-storage.sh` | PARTIAL (postgres-blob stopped: meter 503 + claim released, retry 202; s3 ingest 202 meanwhile; outbox/retry queue = slice G) |
| R2 | Bad network: transfers finish/resume | resumable chunked upload, persisted state, toxiproxy | E,G | `scripts/demo/slow-network.sh` | TODO |
| R3 | Retries never duplicate a reading; corruption detected | Idempotency-Key + UNIQUE → 409; checksum mismatch → 409 | C,D | `duplicate-reading.sh`, `corrupt-file.sh` | PARTIAL (meter half DONE: `scripts/demo/duplicate-reading.sh` burst of 200 -> 0 new rows, 40 parallel -> 1 row; corruption -> 409 proven on S3, `corrupt-file.sh` pending) |
| T1 | Best storage per category and why (§5) | benchmark script → real `/analytics/summary` | I | `scripts/benchmark/run.sh` | TODO |
| T2 | Normal vs peak day (§5) | `normal-day`/`peak-day`/`storm` scenarios | I | `scripts/scenarios/*.sh` | TODO |
| T3 | What data says about the network (§5) | anomaly detection vs `sim_label`, zone outages | I | `/analytics/summary` | TODO |

## 3. Out of scope / assumptions
- Frontend stays React (PDF §6 says stack is a recommendation) — ADR-0001.
- Test data is synthetic only.
- k8s manifests are validated on kind/minikube, not a cloud cluster.
