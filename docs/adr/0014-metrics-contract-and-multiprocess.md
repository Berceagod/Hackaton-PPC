# ADR-0014: Metrics contract, multiprocess mode, and how operations are measured
Status: accepted (slice H)

## Context
PDF §4: for every category AND every storage, latency (p50/p95/p99), throughput and error rate must be visible. Slice I reads these numbers through the Prometheus API, so names and labels are a contract (CLAUDE.md section 4). The backend runs 4 Uvicorn workers per container; a scrape reaches one of them.

## Decision
**Contract.** `filegrid_requests_total`, `filegrid_request_duration_seconds`, `filegrid_bytes_total`, `filegrid_storage_operation_duration_seconds`, `filegrid_integrity_failures_total`, `filegrid_bulkhead_rejections_total`, `filegrid_outbox_queued`, `filegrid_breaker_open`, with the labels, label values and the 13 histogram buckets (5 ms to 60 s) fixed by the contract. Every label value is an enumeration chosen by the code, never client text (unknown categories are ignored; an unknown storage becomes `none`), so cardinality is bounded and a client cannot inject anything.

**Multiprocess mode.** `PROMETHEUS_MULTIPROC_DIR=/tmp/prom_multiproc` (set in docker-compose) makes every worker write its metrics to shared memory-mapped files; `GET /metrics` aggregates them, so a scrape sees the whole container whichever worker answers. Gauges use `livemax`. Gauge files of workers that died are removed (`reap_dead_workers`, at scrape time and in the sampler); counters and histograms keep their history. Without the variable (unit tests, local runs) the normal registry is used. `/metrics` is on the internal root path; nginx answers 404 for it and proxies only `/api/`.

**What one "operation" is.** Every request handler runs inside `metrics.observe(category, operation)`; the outcome comes from the exception, from the response status, or from an explicit note (`queued`, `integrity_error`). A download ends when its LAST BYTE is sent (the observation is deferred and finished by a counting stream), not when the handler returns. A chunked upload is recorded twice on purpose: each chunk is a `chunk` on the staging storage (S3), and the finished file is one `ingest` on the storage the policy chose, with the file's size. Do not add `chunk` and `ingest` bytes together for throughput.

**Outcomes are what the client experienced.** A meter reading that arrives while the same reading is still being stored by another request is answered `503 + Retry-After` (it will get 409 on the retry); it is counted `duplicate`, not `server_error`, so a duplicate burst against a slow storage cannot trip the error-rate alert. A reading accepted into the outbox while its storage is down is `queued` (the meter gets 202). Failed *drain* attempts (the outbox retrying every few seconds while a storage is down) are their own `drain` operation and are left out of the failure-rate panels and alerts: a retry loses nothing. This was found by looking at the dashboards after a storage outage, where the first version mixed 31 drain retries into 6 real refusals.

**Storage timing is storage time.** It is measured in `BreakerStorage`, which wraps every storage: for a write, the time the storage spent minus the time it waited for its input (the client's upload pace, hashing, encryption); for a read, only the time spent waiting for the storage to produce data (not the downloader's pace). A circuit-open refusal counts as an error of 0 s. So a slow client does not make a storage look slow.

**Series that alerts depend on start at 0.** `increase()`/`rate()` need two samples; a counter that first appears already at 1 hides its first event. This was measured, not assumed: with lazily created series the first corrupt file left `increase(...[5m]) = 0` and the alert never fired. The integrity and bulkhead counters and the meter-ingest outcomes are therefore created at 0 at start-up.

**State gauges** (`filegrid_outbox_queued`, `filegrid_breaker_open`, `filegrid_pii_unencrypted_files`, `filegrid_uploads_in_progress`, `filegrid_uploads_stalled`) come from a sampler that every worker runs every `PROMETHEUS_SAMPLE_SECONDS` (default 10 s); a failing dependency leaves the old value and never stops the sampler.

## Consequences
- Metrics are accurate across workers and replicas (Prometheus discovers each replica by DNS, ADR-0016).
- Counter history survives a worker restart inside a container, and resets with the container (Prometheus handles resets).
- Gauges can be up to one sample interval stale.
- Exemplars are not available (see ADR-0015).
