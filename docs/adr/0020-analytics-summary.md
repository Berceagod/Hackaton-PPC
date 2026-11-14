# ADR-0020: Analytics summary from a benchmark, scenarios, and optional Prometheus
Status: accepted (slice I)

Context: The Analytics tab already calls `GET /api/v1/analytics/summary` and renders `AnalyticsSummary` (`frontend/src/types/filegrid.ts`): one storage row per category, two load bars (normal day and 10x storm), three zone cards, and two narratives. Slice H owns the Prometheus metrics and is built in parallel, so this endpoint cannot require Prometheus to be up. T1 asks which storage is best for each category and why; T2 asks how a normal day compares with a peak.

Decision:
- The shape stays `AnalyticsSummary`, plus `narrative.network` (optional for the mock) for the zone story in T3.
- `POST /api/v1/analytics/measurements` (dispecer or admin) stores one run in `analytics_measurements`, created with `CREATE TABLE IF NOT EXISTS` from the repository, not by editing `SCHEMA`. The latest row per (kind, scenario, category, storage) wins.
- `scripts/benchmark/run.sh` writes the same small workload to postgres, s3 and ftp by changing policy, records client latency, posts `kind=benchmark`, and restores the previous policy. The verdict is computed from those rows: lowest p95 for meter readings, outages and connection documents; highest throughput for drone videos. A tie falls back to the business preference (postgres / s3 / ftp / s3). The table shows the storage the **current** policy uses, so a benchmark never silently changes the mapping (ADR-0007: a justified change is a policy edit, not a code edit).
- `scripts/scenarios/normal-day.sh` posts `scenario=normal`. `peak-day.sh` (10x meters) and `storm.sh` (10x outages) both post `scenario=peak`, because the chart has one storm bar; the later run is the one displayed.
- When `PROMETHEUS_URL` answers, PromQL over `filegrid_request_duration_seconds`, `filegrid_bytes_total` and `filegrid_requests_total` (operations `ingest` and `chunk`, window `ANALYTICS_RANGE`) fills the table for the current policy. The window is restricted to `[1-9][0-9]{0,3}[smhd]` so it cannot be injected into the query. If every query fails, the table uses the benchmark and the handler still returns 200.

Consequences: Until slice H is merged the live table stays on the benchmark. p50 and p99 are kept on each measurement and quoted in the verdict; the table columns stay the ones the UI already renders (average, p95, throughput, error rate).
