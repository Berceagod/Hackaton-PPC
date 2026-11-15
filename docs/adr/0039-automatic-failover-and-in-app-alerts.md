# ADR-0039: Automatic failover to a fallback storage, and in-app alerts
Status: accepted. Extends ADR-0013 (bulkheads, breakers, outbox) and ADR-0035 (replicas).

## Context
When a category's storage failed, new files were refused (503) or, for meter readings, queued in the outbox until the storage came back. A flux too big for the primary was queued behind the category's bulkhead. Alerts existed only in Prometheus/Grafana; the app showed an online/offline badge and error pop-ups.

## Decision
- **Policy `fallback_storage`** (per category, optional, never the primary). Set in the Policies tab ("Storage de rezervă"). An update that does not mention it keeps it; an explicit null clears it.
- **Failover is decided per request** (`failover.choose`): new files go to the fallback when (a) the primary's circuit breaker is open (`storage_down`), or (b) the category's bulkhead is at least `FAILOVER_LOAD_THRESHOLD` (default 0.6) full on this instance (`overload`, the data flux is too big). It never routes to a fallback whose own breaker is open. Because it is per request it needs no state machine and returns to the primary by itself when the primary is healthy and the load is normal.
- **A write that fails before the breaker opens** is retried once on the fallback for meter readings (they are small and held in memory). Larger uploads rely on the breaker (three failures) and are not retried mid-stream.
- Each file records the storage it was really written to, so downloads, verification, the scrubber and replicas work unchanged. Existing files stay where they are.
- **App alerts**: a table `app_alerts` for events the application knows first-hand (failover started / ended), and `GET /alerts` which merges them with Prometheus's firing and pending alerts (annotations and runbook included). The header has a bell with a count (red when critical), a list with severity, detail and "ce verific", and the browser tab title shows the count. If Prometheus is unreachable the bell says so and still shows the app's alerts.
- **Six new Prometheus rules** (27 in total): `HighDataFlux` (>5 MB/s), `HighFileRate` (>50 files/s), `FailoverActive`, `StorageSlow` (write p95 >1 s), `CategoryErrorRateHigh` (non-meter categories, >5%), `LoginFailuresHigh`; each with a runbook section. Metrics `filegrid_failover_active`, `filegrid_failover_files_total`.

## A bug found by testing this live (and fixed)
`policies_config` stored only four columns, so `replicas`, `replica_storage` (ADR-0035) and the new `fallback_storage` lived in memory until the next policy refresh overwrote them. The unit-test fake stores whole dictionaries, so only the live test exposed it. The three fields are now persisted (columns added with `IF NOT EXISTS`).

## Limits
- The overload signal is per backend instance (its own bulkhead), not a global rate.
- Chunked uploads (drone videos > 5 MB) follow the policy's primary: failover covers direct uploads and meter readings.
- Flux-based failover trades latency for availability: the fallback may be slower than the primary (see the Analytics live chart).
- Alerts are not acknowledged or silenced in the app; they clear when the cause does.

## Proof
Live: with PostgreSQL taken down through the Chaos console, all 140 readings in the next 15 s were stored on FTP, a critical in-app alert and the Prometheus `FailoverActive` appeared, and after healing readings returned to PostgreSQL and the app alert resolved. 7 unit tests (`test_failover.py`); 415 tests pass.
