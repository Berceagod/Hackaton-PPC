# Slice J — scale-out and Kubernetes
Branch: feat/slice-j-scale (from `main` @ dabf884) · Machine owner: Gabriel

## Status
- DONE, uncommitted on `feat/slice-j-scale`. Merge order: after H (CLAUDE.md section 6). Before the pull request: merge `main` (with H) into this branch and rerun the full demo list, plus the H x J check below.

## Built (what / why / how)
- **Scale-out on Docker Compose** (S2): no file change needed. `docker compose up -d --scale backend=N`; nginx -> toxiproxy -> `backend:8000` spreads connections over the instances through Docker DNS (measured before writing anything: 73/63/55 readings per instance in 20 s). The backend was already stateless (slices E-G).
- `scripts/demo/scale-out.sh`: 1 instance vs N instances of the same image under the same load (512 KB outage uploads, 2 in-network load generators x 32 clients, 1 worker per instance so one instance is the limit), checks gain, errors, that every instance serves, one image; restores 1 instance with default workers.
- `scripts/j-loadgen.py`: async load generator run inside the Docker network (simulator image: httpx + the gateway CA) or as a Kubernetes Job. Named with the slice prefix like the `scripts/<x>-*` helpers.
- `backend/tests/test_slice_j.py`: 5 characterization tests with two app instances sharing only the DB and storages (upload A / download B; chunks split across A and B; reading retried on B = duplicate; policy change on A reaches B; either instance drains the outbox). They passed on the first run: they lock in statelessness rather than drive new code.
- **Kubernetes** (S2 + X1 on k8s): `k8s/` manifests (kustomize), `k8s/up.sh` (kind cluster, images from the same Dockerfiles, metrics-server v0.7.2, secrets from `.env`, TLS, policies ConfigMap), `k8s/verify.sh`, `k8s/down.sh`, `k8s/find-kind.sh`, `k8s/kind-cluster.yaml`, `k8s/README.md` (RO). Backend HPA 2-6 pods at 70% CPU; NetworkPolicy default-deny + gateway open + backend only from gateway + storages only from backend; postgres `max_connections=200`.

## Proof (exact commands and their result)
- Backend tests (all slices): `docker run --rm -v "<repo>/backend:/src:ro" python:3.12-slim sh -c "cp -r /src /app && cd /app && pip install -q -r requirements-dev.txt && python -m pytest -q tests"` -> 146 passed (141 + 5 new).
- Baseline before starting (rule 0): tests 141 passed, `smoke.sh` and `kill-storage.sh` PASSED.
- `scripts/demo/scale-out.sh 3` -> PASSED: 1 instance 55.2 uploads/s (p95 2247 ms), 3 instances 120.9 uploads/s (p95 1441 ms), x2.19 (required >= x1.8), 0 errors, per instance 850/833/796, one image.
- `k8s/up.sh` then `k8s/verify.sh` -> KUBERNETES VERIFY PASSED: only nginx NodePort, all else ClusterIP; outage_records -> s3 and connection_docs -> ftp uploaded and downloaded byte-identical with integrity verified through https://localhost:8443; throwaway pod blocked from postgres-meta, postgres-blob, minio, ftp, backend; pod -> nginx:443 open; backend -> postgres-meta/minio/ftp open; simulator 94 readings in 10 s, failed 0, dropped 0; HPA min 3 -> 3 pods all serving (59/60/70 readings in 20 s); under load the HPA grew the backend 2 -> 6 pods (CPU 247% of request), 160.9 uploads/s, 0 failed.
- `scripts/security/secrets-scan.sh` -> PASSED.

## SPEC rows (status line to paste into docs/SPEC.md)
- S2: DONE (`scripts/demo/scale-out.sh`: same image 1 -> 3 instances with one command, x2.19 uploads/s, 0 errors, all instances serving; Kubernetes HPA 2 -> 6 pods under load, `k8s/verify.sh`; ADR-0026, ADR-0027)
- X1: DONE (compose: `unauthorized-direct-storage-access.sh`; Kubernetes: only the gateway is NodePort, NetworkPolicy blocks any pod but the backend from every storage, `k8s/verify.sh` step 3)

## Shared-file edits (each one under this slice's marker)
- None. No shared file was edited: scaling needed no change to `docker-compose.yml`, `main.py`, `config.py` or the others.

## ADRs written
- `docs/adr/0026-scale-out.md`, `docs/adr/0027-kubernetes.md`

## Known gaps
- Compose scale-out is x2.2 for 3 instances, not x3: nginx, toxiproxy, MinIO and the disk are shared single instances.
- Compose `postgres-meta` keeps the default 100 connections: 3 instances need `UVICORN_WORKERS=2` (the demo uses 1). Raising `max_connections` in compose needs an edit to a service no slice owns (left as is; see requests).
- Kubernetes: single kind node, single-replica storages (no HA), plain Secrets, self-signed TLS, no toxiproxy (chaos demos stay on compose), `stilliard/pure-ftpd:latest` unpinned (same as compose), metrics-server downloaded from GitHub by `up.sh`.
- `k8s/base/nginx.conf` is a copy of `infrastructure/nginx/default.conf` (H-owned): keep in sync when H changes it (e.g. `/grafana/`).
- The demo machine needs `kind` and `kubectl` for the Kubernetes part.

## Requests to other slices
- To H (after both merged): add Prometheus/Grafana to Kubernetes or accept compose-only observability. If added, it needs a NetworkPolicy allowing Prometheus -> `backend:8000` and Kubernetes service discovery instead of Docker DNS. Agreed: no (to discuss at integration).
- To H: when `/grafana/` (or anything else) is added to `infrastructure/nginx/default.conf`, tell J so `k8s/base/nginx.conf` is updated. Agreed: no.
- To the integrator: after H and J are both in `main`, run `scripts/demo/scale-out.sh` and check Prometheus lists N `backend` targets (CLAUDE.md section 6). Consider raising compose `postgres-meta` `max_connections` (unowned service) so 3 instances also work with the default 4 workers.
- Note for CLAUDE.md (integrator): `backend/Dockerfile` has no owner; H likely needs it for the prometheus multiprocess directory cleanup at container start.
