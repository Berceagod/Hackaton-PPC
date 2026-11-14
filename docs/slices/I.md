# Slice I — analytics
Branch: feat/slice-i-analytics · Machine owner: Mihnea

## Status
- Backend summary, measurement log, benchmark and scenario scripts, and the Analytics tab are implemented.
- Prometheus is not on this branch (slice H). The summary answers 200 without it and uses it when `PROMETHEUS_URL` responds.
- Live `scripts/benchmark/run.sh` / scenario runs against a started stack are recorded under Proof. The short sample can pick ftp over s3 for small files; run the defaults (`BENCHMARK_SAMPLES=5`, `BENCHMARK_DRONE_KB=1024`) before the demo if you want a steadier ranking.

## Built (what / why / how)
- `GET /api/v1/analytics/summary` returns `AnalyticsSummary` (plus `narrative.network`). T1 verdicts come from the latest benchmark, T2 bars from scenario runs, T3 zones from `files_metadata`, `meter_readings` and `audit_logs`.
- `POST /api/v1/analytics/measurements` stores a run. Table `analytics_measurements` is created with `CREATE TABLE IF NOT EXISTS` from the repository, not by editing `SCHEMA`.
- `scripts/benchmark/run.sh` writes the same small workload to postgres, s3 and ftp, posts the timings, and restores the previous policy.
- `scripts/scenarios/normal-day.sh` posts `scenario=normal`. `peak-day.sh` (10x meters) and `storm.sh` (10x outages) post `scenario=peak` (one storm bar; the later run wins).
- The Analytics tab reloads, shows an error instead of spinning forever, and prints `narrative.network` under the zone cards. Throughput under 0.1 MB/s is shown in KB/s, so a meter reading is not displayed as 0.0 MB/s.
- ADR-0020, ADR-0021.

## Proof (exact commands and their result)
- `backend` tests: 162 passed (21 in `tests/test_slice_i.py`; slices A–G still green).
- Live, after rebuilding `backend` and `nginx`: `GET /analytics/summary` without a token is 401.
- `BENCHMARK_SAMPLES=2 BENCHMARK_DRONE_KB=64 BENCHMARK_OUTAGE_KB=32 scripts/benchmark/run.sh` PASSED. All 12 category×storage pairs stored 2/2. Policies restored. Verdict: postgres is the best for meter readings (p95 25 ms, against s3 55 ms and ftp 1945 ms). On this short sample ftp was slightly ahead of s3 for outages and for the 64 KB videos; the summary says so instead of repeating the default policy.
- `SCENARIO_METERS=6 SCENARIO_OUTAGES=2 scripts/scenarios/normal-day.sh` then `storm.sh` PASSED. Normal day p95: meters 38 ms, outages 94 ms, docs 31 ms, drone 60 ms. Storm: 42 / 172 / 21 / 82 ms. The outage category is the one that jumped.
- Zone line from the same summary: the detector marked 470 readings and 470 of 471 simulator labels matched (`sim_label`).

## SPEC rows (status line to paste into docs/SPEC.md)
- T1: DONE (benchmark posts measurements; summary names the best storage from p95 or throughput, and says when the current policy is not that storage)
- T2: DONE (`normal-day.sh` / `peak-day.sh` / `storm.sh` fill the two bars; the narrative compares the growth)
- T3: DONE (zone cards from stored outages, drones and detected meter anomalies; `narrative.network` is the agreement with `sim_label`)

## Shared-file edits (each one under this slice's marker)
- docker-compose.yml: `PROMETHEUS_URL`, `ANALYTICS_RANGE`, `ANALYTICS_TIMEOUT_SECONDS` on the backend env anchor
- backend/main.py: import `analytics_router`; `include_router` under `/api/v1`
- backend/config.py: `prometheus_url`, `analytics_range`, `analytics_timeout_seconds` (defaults) and their `from_env` lines
- backend/repo.py: protocol methods; `PostgresRepo.ensure_analytics_schema`, `save_measurements`, `latest_measurements`, `analytics_snapshot`
- backend/tests/conftest.py: the same four methods on `FakeRepo`
- .env.example: commented defaults, including `BENCHMARK_*` and `SCENARIO_*`
- backend/requirements.txt: no new dependency (Prometheus is queried with the standard library)
- start.sh / start.ps1: no new secret

## ADRs written
- docs/adr/0020-analytics-summary.md
- docs/adr/0021-anomaly-detection.md

## Known gaps
- The benchmark uses demo-sized files (`BENCHMARK_DRONE_KB`, default 1 MB), not a 1 GB video, so the drone throughput gap is real but smaller than a full inspection video.
- `peak-day.sh` and `storm.sh` share the single storm bar. Run `normal-day.sh` and then one of them; the later peak overwrites the earlier one.
- Load-spike detection needs a per-meter baseline (see ADR-0021). A meter with only spikes is not flagged.
- Live p50/p95/p99 in Grafana remain slice H. This slice reads those series when Prometheus is up and otherwise shows the benchmark.

## Requests to other slices
- (to H) No code change requested. When Prometheus is scraped, `GET /analytics/summary` already queries `filegrid_request_duration_seconds`, `filegrid_bytes_total` and `filegrid_requests_total` with labels `category`, `storage`, `operation` (`ingest` or `chunk`). Agreed? not yet — the contract is the one in CLAUDE.md, so no extra change is required.
- (to J) None. The summary is stateless and reads Postgres plus Prometheus, so extra backend replicas do not need a code change.
