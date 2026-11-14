# ADR-0013: Isolation and resilience with a Postgres outbox, bulkheads and breakers (no RabbitMQ)
Status: accepted (slice G; supersedes the RabbitMQ part of ADR-0008, decided with the team)

Context: PDF §4: a 10x spike in one category must not degrade the others; a storage going down must not stop other categories nor lose confirmed files; transfers must finish or resume on slow, lossy links; retries must never duplicate a meter reading. ADR-0008 planned four RabbitMQ queues with workers and a DLQ. Large bodies (up to 1 GB+) cannot go through a broker anyway (ADR-0010), and meter readings, the only high-volume category, are already persisted in `meter_readings`.

Decision:
- **Outbox for meter readings.** If the storage write fails, the reading's exact bytes go into `meter_readings.raw` with status `queued` and the sender gets `202 {status: "queued"}`: durably accepted. `meter_outbox.py` (a task in every backend worker) takes queued rows with `FOR UPDATE SKIP LOCKED`, writes them to the storage the policy names (encrypted if required), records the file and marks them `stored` (raw cleared). A resend of a queued reading is a duplicate (409). Only if the outbox write also fails is the claim released and 503 returned.
- **Circuit breaker per storage** (`storage/breaker.py`): 3 consecutive failures open it for 5 s, so requests fail at once instead of waiting on timeouts; one trial call then decides. A missing object is not a failure.
- **Bulkheads** (`bulkhead.py`): per category, a concurrency limit sized by the policy's `priority` (high 64, normal 32, low 8 per worker); no slot within 10 s -> 503 + Retry-After. Meter readings use their own connection pool (`METER_DB_POOL`).
- **Several worker processes** (`UVICORN_WORKERS`, default 4): CPU-heavy uploads (multipart parsing, SHA-256, AES) in one category cannot take all the CPU from another. Measured with `spike-10x.sh`: meter-reading p95 during an outage-upload storm went from 163 ms (1 process) to 54 ms (4), baseline 33-38 ms. Pools are per worker: 4 x (6 + 4) = 40 metadata connections per container.
- **Policy changes reach every worker at once** via Postgres `NOTIFY filegrid_policies` on save and `LISTEN` in each worker (`PolicyStore.invalidate`); the 2 s refresh stays as fallback. Needed once there is more than one worker.
- **Sender side:** the simulator keeps readings it cannot deliver (after its retries, or when 200 requests are already in flight) in a backlog and resends them until accepted (202) or recognised (409), like a meter's memory.
- **Abandoned chunked uploads** (open/failed, no activity for `UPLOAD_TTL_HOURS`, default 24) are purged with their staged chunks every 15 min.
- **Bad network demo:** toxiproxy sits permanently between nginx and the backend as a pass-through; `scripts/chaos/network-bad.sh` adds latency, an upload cap and connection cuts; `network-ok.sh` restarts toxiproxy (removing toxics under load can deadlock it).

Consequences:
- No broker to run or explain; the guarantees are proven by `kill-storage.sh`, `spike-10x.sh`, `slow-network.sh`.
- UI categories still answer 503 while their storage is down (nothing is confirmed that is not stored; the user retries); only meter readings are queued.
- The simulator's backlog is in memory: it survives FileGrid outages, not a restart of the simulator itself.
- toxiproxy is an extra hop on every request (negligible latency, one more container that must be up).
