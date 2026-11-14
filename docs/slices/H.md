# Slice H — observability
Branch: feat/slice-h-observability · Based on: `main` at dabf884 · **Nothing is committed or pushed yet.**

## Status
- Built and verified. Backend instrumentation, Prometheus + Alertmanager + webhook, Loki + Alloy, Grafana (3 dashboards as code), nginx `/grafana/`, two demo scripts, 6 ADRs.
- Verified on an **isolated compose project** (`fghtest`: own volumes, own random secrets, host ports 18080/18443) so the stack that was already running on the machine was not touched or rebuilt.
- Left to do before the pull request: merge the latest `main` into this branch on the owner's machine and run the full list of section 6 once more with the real `./start.sh`; fold this file into `progress.md` / `explanations.md` / `docs/SPEC.md` (integrator).

## Built (what / why / how)
**Metrics (O1)** — `backend/metrics.py`, exactly the contract of CLAUDE.md section 4 (8 metrics, labels, buckets). Every request handler is wrapped in `metrics.observe(category, operation)`; downloads end at the last byte; a chunked upload is `chunk` on the staging storage per chunk plus one `ingest` on the policy's storage for the finished file. Storage timings are taken in `BreakerStorage` and measure time INSIDE the storage (a slow client's upload or download pace is excluded). `prometheus_client` multiprocess mode aggregates the 4 workers of a container. Alert-critical series start at 0 (an `increase()` that missed the first-ever corrupt file was found by experiment and fixed). ADR-0014.
**Trace (O2)** — one trace id from the gateway to the storage: nginx writes a JSON access line carrying the id the backend assigned; the backend logs each storage call (`storage op`: storage, op, outcome, duration_ms, category) under the request's id; the outbox drainer writes a queued reading out under its ORIGINAL id. Loki holds the lines (trace id as structured metadata); the "Trace one operation" dashboard and `scripts/demo/trace-one-request.sh` find them by id. ADR-0015.
**Alerts (O3)** — 17 rules, each tied to a guarantee of table 2.1 (meter gap/loss/errors/outbox/duplicates, outage latency, unencrypted personal data, integrity failure, stalled video upload, plus platform health). Alertmanager delivers to our webhook receiver. Rules are unit-tested with `promtool` and fired for real by `scripts/demo/alert-fire.sh`. ADR-0016.
**Logs** — Loki (48 h) fed by Grafana Alloy through a read-only, loopback-only Docker API proxy; only this compose project's containers. ADR-0017.
**Dashboards as code** — `infrastructure/grafana/` (provisioning + 3 generated dashboards, 47 panels) behind nginx at `https://localhost/grafana/` with an admin password generated into `.env`. ADR-0018. Privacy rules for telemetry: ADR-0019.

**How to open it** (after `./start.sh`): `https://localhost/grafana/` · user `admin` · password = `GRAFANA_ADMIN_PASSWORD` in `.env`. Dashboards: *Operations* (per category and per storage latency p50/p95/p99, throughput, error rate; isolation panels; simulator), *Business guarantees*, *Trace one operation*.

## Proof (exact commands and results; all on the isolated stack unless noted)
- `backend` tests: **214 passed** on Linux/Python 3.12 (141 existing + 73 in `test_slice_h.py`); 213 + 1 skipped on Windows (the skipped one needs POSIX process liveness). Command: the one in CLAUDE.md section 7.
- `promtool test rules infrastructure/prometheus/alerts_test.yml`: **SUCCESS** (each alert fires on its situation, stays quiet otherwise). Configs also pass `promtool check config`, `amtool check-config`, `loki -verify-config`, `alloy fmt`, `nginx -t`, `docker compose config`.
- `scripts/demo/trace-one-request.sh`: **PASSED**, twice in a row. One upload with a chosen trace id: the gateway line, the backend request, the call into S3 (`s3 put ok 120 ms`) and `file stored` are all found by that id; the same upload is visible in `filegrid_requests_total` and `filegrid_storage_operation_duration_seconds_count`.
- `scripts/demo/alert-fire.sh` (full, ~8 min): **PASSED**. Three guarantees broken on the live system: a byte changed inside S3 -> `FileIntegrityFailure`; a connection document stored unencrypted -> `PersonalDataStoredUnencrypted`; a chunked upload left idle -> `VideoUploadStalled`. Each fired in Prometheus AND was delivered to the team webhook; the two state-based alerts resolved and the "resolved" notification was delivered. `alert-fire.sh quick` (integrity only) takes ~30 s.
- Teammates' demos run against the same stack as regression, all **exit 0**: `smoke`, `policy-change`, `duplicate-reading` (rerun on the final backend too), `spike-10x` (outage p95 during a 10x meter storm: 198 ms vs 236 ms baseline), `kill-storage` (237 readings accepted into the outbox, none lost), `unauthorized-direct-storage-access`.
- Alerts also fired by themselves during those demos and resolved: `MeterDuplicateReadingsHigh` (17%), `StorageCircuitOpen` and `StorageErrorRateHigh` (postgres-blob stopped). No false alert on the 10x spike.
- `scripts/security/secrets-scan.sh`: **PASSED** (13 commits and the working tree, `.env` never read).
- Looked at in a browser: all 3 dashboards render with live data (per-storage latency, the outbox peaking at 288 and draining to 0 during `kill-storage`, the 10x meter spike). Prometheus scrapes the backend by DNS discovery; Loki holds logs from exactly this project's 15 services and none from another project on the machine.
- Found and fixed while testing (details in the ADRs): the first corrupt file would not have fired its alert (`increase()` on a series born at 1); a duplicate arriving while its original is still stored was counted as a server error; failed outbox drain retries inflated the failure-rate panels; the trace dashboard matched Loki's and Grafana's own log lines about the lookup.

## SPEC rows (status line to paste into docs/SPEC.md)
- O1: DONE (`filegrid_*` contract metrics per category and per storage; Grafana *Operations* at /grafana/; `promtool`/tests in `test_slice_h.py`)
- O2: DONE (`scripts/demo/trace-one-request.sh`: gateway -> backend -> storage call found by one trace id in Loki)
- O3: DONE (`scripts/demo/alert-fire.sh`: integrity, unencrypted personal data and stalled upload fire and reach the team webhook; rules unit-tested with promtool)
- Supports (evidence only, owned by others): S1 (isolation panels + spike-10x result), R1 (breaker, outbox and storage-error panels and alerts), X1 (Prometheus, Loki, Alertmanager, Alloy publish no port; only nginx does).

## Shared-file edits (each one under this slice's marker)
- `docker-compose.yml`: `x-backend-env` +3 lines (`PROMETHEUS_MULTIPROC_DIR`, `PROMETHEUS_SAMPLE_SECONDS`, `PROMETHEUS_UPLOAD_STALL_SECONDS`); services +7 (`prometheus`, `alertmanager`, `alert-webhook`, `loki`, `alloy`, `docker-proxy`, `grafana`); volumes +3 (`promdata`, `lokidata`, `grafanadata`). 121 lines added, none changed.
- `backend/main.py`: `import metrics`; the sampler task; `GET /metrics` route (internal; nginx still answers 404 for it).
- `backend/config.py`: `prometheus_sample_seconds`, `prometheus_upload_stall_seconds` (with defaults) + `from_env`.
- `backend/requirements.txt`: `prometheus-client==0.21.1` (already used by the simulator).
- `backend/repo.py`: protocol + `PostgresRepo` methods `count_unencrypted_pii`, `upload_stall_stats` (read-only queries; no schema change).
- `backend/tests/conftest.py`: `FakeRepo` methods and fixtures `metric`, `breaker_client` (no existing fake or fixture changed).
- `.env.example`, `start.sh`, `start.ps1`: `GRAFANA_ADMIN_PASSWORD` (generated), `LOKI_COMPOSE_PROJECT` (not a secret).
- Not shared files, but edited as H owns instrumentation: `routes/files.py`, `routes/uploads.py`, `meter_ingest.py`, `meter_outbox.py`, `chunked_upload.py`, `bulkhead.py`, `storage/breaker.py`, and `infrastructure/nginx/default.conf` (JSON access log + `/grafana/`). Behaviour is unchanged; the existing tests of slices A-G pass untouched.

## ADRs written
- docs/adr/0014-metrics-contract-and-multiprocess.md
- docs/adr/0015-trace-propagation.md
- docs/adr/0016-alerting.md
- docs/adr/0017-log-pipeline.md
- docs/adr/0018-grafana-as-code.md
- docs/adr/0019-telemetry-privacy.md

## Known gaps
1. **No exemplars / OpenTelemetry spans.** `prometheus_client` has no exemplars in multiprocess mode (required by the contract), so metrics and traces are linked by labels and time, not by a click; there is no waterfall view (ADR-0015).
2. **The Docker socket is mounted** (read-only) into the log proxy. It is GET-only, loopback-only inside Alloy's network namespace, and the compose-project filter is in Alloy's config, not enforced by the proxy (ADR-0017). Compose-specific: Kubernetes needs a DaemonSet collector (slice J).
3. **The webhook receiver is unauthenticated** on the internal network: any container there could post a fake alert or read the list. It stands in for a team channel; a real one (Slack, Teams) replaces it.
4. **Meter gap/loss alerts partly rest on the simulator's `sim_*` metrics** (`sim_dropped_total`, `sim_backlog`); real meters would need their own signal. The gap alert (`MeterReadingsNotArriving`) rests on FileGrid's own counters.
5. **Storage servers themselves are not scraped** (no postgres/minio/ftp exporters): "per storage" numbers are measured at FileGrid's adapters (what FileGrid experiences), not CPU/disk of the storage servers.
6. **The PII alert reports a count, not which document**, and the clean-up needs the database (there is no delete API).
7. **Small undercounts:** a download whose client disconnects before the first byte streams is not recorded; the last chunk's `chunk` duration includes the assembly time.
8. **Not verified:** whether toxiproxy follows a backend IP change. I restarted it after recreating the backend in the test stack to be safe; if the gateway returns 502 after a backend rebuild, `docker compose restart toxiproxy` is the first thing to try.
9. Retention is short on purpose (logs 48 h, metrics 2 days); the Grafana volume keeps only what Grafana itself stores, dashboards come from git.
10. `start.sh`'s final message cannot print the Grafana URL and password on a slice branch (that block is not under a slice marker): the integrator should add one line after the merge.

## Requests to other slices
- (to I) The contract is implemented as written. `PROMETHEUS_URL` default `http://prometheus:9090` works from the backend (service `prometheus`, internal network). Extra metrics are available: `filegrid_pii_unencrypted_files`, `filegrid_uploads_in_progress`, `filegrid_uploads_stalled`. Read ADR-0014 for what `chunk` vs `ingest`, `queued`, and `drain` mean before computing throughput or error rates. Agreed? n/a (information).
- (to J) Prometheus finds replicas by DNS on the service name `backend` (port 8000) and each container aggregates its own workers: keep the name, no `container_name`, no `ports` on `backend`. The *Operations* dashboard's "Backend instances up" is the panel that shows N after `--scale backend=N`. Replicas are scraped within 5 s of appearing. Agreed? n/a (information).
- (to integrator) After merging: fold this file into the shared docs, add the Grafana line to the start scripts' success message, and mention `/grafana/` in the README (Romanian) demo walkthrough next to O1-O3.
