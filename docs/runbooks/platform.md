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
