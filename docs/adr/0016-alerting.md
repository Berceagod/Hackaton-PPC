# ADR-0016: Alerts for the business guarantees, delivered to a team webhook
Status: accepted (slice H)

## Context
PDF §4: the team is notified automatically when a requirement of table 2.1 is at risk. "At risk" must mean something checkable for each row of the table.

## Decision
Prometheus evaluates `infrastructure/prometheus/alerts.yml` every 5 s; Alertmanager groups the alerts and posts them to a webhook receiver (`alert-webhook`, our own, standard library only) that logs each alert (so it appears in Loki) and keeps the last 200 (`GET /alerts`). Swapping in Slack, Teams or e-mail means adding one receiver to `alertmanager.yml`. Every alert has `severity` and a `guarantee` label naming the promise at risk. Prometheus finds every backend replica by DNS service discovery (`dns_sd_configs` on the service name `backend`), so `--scale backend=N` needs no change.

| Guarantee (table 2.1) | Alerts | Signal |
|---|---|---|
| Meter readings never lost | MeterReadingsNotArriving, MeterReadingServerErrors, MeterOutboxBacklog / Growing, MeterReadingsLostAtSender, MeterReadingsWaitingAtSender | no reading accepted for 2 min after flowing in the last 30; > 5% server errors; readings accepted but not yet in their storage; meters dropping or buffering readings (the simulator's `sim_*` metrics) |
| Meter readings never duplicated | MeterDuplicateReadingsHigh | > 15% of incoming readings are duplicates. A duplicate can never be stored twice (UNIQUE constraint, 409); this signals a sender retrying wildly. The simulator's normal 5% does not fire it |
| Outage records available immediately | OutageRecordIngestSlow, OutageRecordDownloadSlow | p95 save > 2 s, p95 open > 3 s |
| Connection documents: personal data protected | PersonalDataStoredUnencrypted | any `connection_docs` file with `encrypted = false` (a database count, so it also catches an administrator switching the policy off) |
| Integrity (all categories) | FileIntegrityFailure | any download or verify that found a stored file damaged or missing (reasons `checksum_mismatch`, `object_missing`, `decryption_failed`, `staging_corrupted`); corruption in transit is retried by the client and does not page |
| Drone videos survive bad links | VideoUploadStalled | a chunked upload with no chunk for 5 min (`PROMETHEUS_UPLOAD_STALL_SECONDS`) |
| Resilience / scalability / availability | StorageCircuitOpen, StorageErrorRateHigh, CategoryAtCapacity, BackendInstanceDown, NoBackendInstances | breaker open, storage error ratio, bulkhead refusals, scrape failures |

**Tested twice.** `infrastructure/prometheus/alerts_test.yml` feeds synthetic series to the real rules (`promtool test rules`): each alert fires on its situation and stays quiet otherwise, including the negative cases (a slow drone video does not trip the outage alert; a system that never had readings has no "gap"). `scripts/demo/alert-fire.sh` then breaks three guarantees on the running system and checks that each alert fires AND arrives at the webhook, and that the two state-based ones resolve and the resolution is delivered.

## Consequences
- Thresholds are plain numbers in the rules; changing one is an edit and a Prometheus reload, not a code change.
- Alerts that look at `increase()` depend on series that start at 0 (ADR-0014).
- `FileIntegrityFailure` keeps firing for 5 minutes after the last failure (its window), then resolves.
- The simulator-based alerts exist only while the simulator runs; real meters would expose their own equivalents.
