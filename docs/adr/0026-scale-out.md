# ADR-0026: Capacity grows by adding backend instances, no code change
Status: accepted (slice J)

Context: PDF §4 Scalabilitate: "capacity grows by adding instances, without code changes". The backend already keeps no state of its own: files, readings, upload sessions, policies and the outbox live in Postgres and the storages (ADR-0010, ADR-0013); policy changes reach every process through Postgres NOTIFY.

Decision:
- Docker Compose: `docker compose up -d --scale backend=N`. Nothing in the repo changes: the `backend` service has no `container_name` and no published port; nginx -> toxiproxy -> `backend:8000` resolves the name per connection and Docker's DNS spreads connections across the instances (measured: 73/63/55 readings per instance in 20 s).
- Database connections: each instance holds up to `UVICORN_WORKERS x (DB_POOL + METER_DB_POOL)` metadata connections (4 x 10 = 40 by default). Postgres' default limit (100) allows 2 instances at the default worker count; for more, start them with fewer workers (`UVICORN_WORKERS=2 docker compose up -d --scale backend=3`) or raise `max_connections` (done in Kubernetes, ADR-0027). The compose `postgres-meta` service was left unchanged (owned by no slice during H/I/J).
- Proof: `scripts/demo/scale-out.sh` drives 512 KB outage uploads from 2 load generators x 32 clients running inside the Docker network (`scripts/j-loadgen.py` in the simulator image, so the host is not the bottleneck), with 1 worker per instance so one instance's capacity is the limit: 1 instance 55 uploads/s, 3 instances 121 uploads/s (x2.19), 0 errors, every instance serving, one image.
- Characterization tests (`tests/test_slice_j.py`): two app instances sharing only the database and storages: upload on A / download on B, chunks of one upload split across A and B, a reading retried on B is a duplicate, a policy change on A reaches B, either instance drains the outbox.

Consequences:
- Not linear: 3 instances give x2.2, not x3, because nginx, toxiproxy, MinIO and the disk are shared single instances. Scaling those is out of scope for the demo.
- One load-generator process tops out near 95 uploads/s; the demo runs two in parallel and sums them.
