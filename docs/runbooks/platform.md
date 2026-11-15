# Runbook: platform (storages, capacity, instances)

## StorageCanaryFailing
**Means:** the synthetic check (write a tiny object, read it back, compare, delete) fails repeatedly on a storage. It runs every minute with no user traffic, so this finds a broken storage that metrics alone would not.
1. `docker compose ps`; is the storage container up and healthy? `docker compose logs <storage> --since 10m`.
2. Disk full? Check the volume (`docker system df -v`).
3. Is it a deliberate experiment? The chaos console (`GET /api/v1/admin/chaos`) lists active faults; `DELETE /api/v1/admin/chaos` clears them.
4. Other categories keep working while one storage is down; meter readings wait in the outbox.

## StorageCircuitOpen
**Means:** a storage kept failing, so its circuit breaker opened: calls to it fail fast instead of waiting. Same investigation as the canary. The breaker closes by itself after a successful trial call.

## StorageErrorRateHigh
**Means:** a large share of operations on a storage fail. See *StorageCanaryFailing*; the *Operations* dashboard shows which operation (put, get, delete) fails.

## CategoryAtCapacity
**Means:** one category's own concurrency limit is full and requests get 503 (bulkhead). The other categories are unaffected. Add backend instances (`docker compose up -d --scale backend=3`, see `scripts/demo/scale-out.sh`) or raise that category's limit.

## BackendInstanceDown / NoBackendInstances
**Means:** Prometheus cannot scrape a backend instance, or any. `docker compose ps backend`, `docker compose logs backend`. With several instances the others keep serving; with none, the app is down. `curl -k https://localhost/api/v1/livez` tells you from the outside.

## HighDataFlux / HighFileRate
**Means:** a category is receiving a lot of data (over 5 MB/s) or a lot of files (over 50 per second, more than five times a normal day). It is an early warning, not yet a failure.
1. Is it expected? A meter storm or a bulk upload of drone videos. The Testing overlay can also cause it (check it is off).
2. Does the category have a fallback storage? `GET /api/v1/admin/policies`: with `fallback_storage` set, new files are routed there automatically when the primary is overloaded (see *FailoverActive*).
3. If it is a runaway sender, find it in the log by trace id and stop it at the source.

## FailoverActive
**Means:** new files of a category are going to its fallback storage because the primary is down (circuit open) or overloaded. Nothing is lost and no action is needed for the data. Existing files stay on the primary.
1. Which and why? The alert in the app says `storage_down` or `overload`.
2. `storage_down`: see *StorageCanaryFailing*. The category returns to its primary by itself once the circuit closes.
3. `overload`: see *HighDataFlux*. Consider more backend instances or a faster primary.

## StorageSlow
**Means:** writes to a storage take over 1 s at p95 (2-minute window). Compare the three storages on the Analytics live chart or in Grafana -> Operations. A slow FTP under many parallel uploads is a connection-limit effect: lower the load or move the category.

## CategoryErrorRateHigh
**Means:** over 5% of requests of a non-meter category fail with a server error. Check the storage of that category (Chaos tab, Grafana storage error rate) and `docker compose logs backend`.

## LoginFailuresHigh
**Means:** many failed or blocked logins in 5 minutes. Accounts lock automatically (30 s doubling to 15 min). Check which accounts in the audit trail (`login_locked` events) and whether it is one source (nginx log).
