# ADR-0027: Kubernetes manifests validated on a local kind cluster
Status: accepted (slice J)

Context: the recommended stack lists Kubernetes; PDF §4 asks that capacity grows by adding instances and that storages are reachable only through FileGrid. The demo machine runs Windows + Docker Desktop.

Decision:
- `k8s/` holds plain manifests applied with kustomize (`kubectl apply -k k8s/base`): StatefulSets with volumes for postgres-meta, postgres-blob (both `max_connections=200`), MinIO and FTP; Deployments for the backend (2 workers per pod), the nginx gateway and the meter simulator; ClusterIP Services everywhere except the gateway (NodePort 30443).
- The same images as Docker Compose (built from the same Dockerfiles by `k8s/up.sh` and loaded with `kind load`). The gateway uses its own copy of the nginx config (`k8s/base/nginx.conf`: cluster DNS resolver, `/api/` straight to the backend Service, no toxiproxy).
- HorizontalPodAutoscaler on the backend: 2 to 6 pods at 70% of the CPU request (250m); metrics-server v0.7.2 installed by `k8s/up.sh` (with `--kubelet-insecure-tls`, needed on kind).
- NetworkPolicy: default deny ingress; the gateway open on 443/80; the backend only from the gateway; every storage (`tier: storage`) only from the backend, including the FTP passive range. kind's default CNI (kindnet) enforces NetworkPolicy, checked by `k8s/verify.sh` with a throwaway pod.
- Secrets are never in the repo: `k8s/up.sh` creates `filegrid-storage`, `filegrid-backend` (URLs with passwords, JWT secret, master key), `filegrid-simulator` and `filegrid-tls` from the machine's `.env`. The simulator gets only the public certificate.
- Local cluster: kind, one node, published on `https://localhost:8443` so it can run next to the compose stack on 443.

Consequences:
- Proven live by `k8s/verify.sh`: only the gateway exposed; upload/download of an outage record (S3) and an encrypted connection document (FTP) through the gateway; arbitrary pod blocked from all 4 storages and the backend, the backend allowed (positive control); simulator delivering, 0 lost; 3 pods all serving; under load the HPA grew the backend from 2 to 6 pods with 0 failed uploads.
- Not production: a single node, single-replica storages (no HA), plain Kubernetes Secrets, self-signed TLS, no toxiproxy (the chaos demos stay on compose), `stilliard/pure-ftpd:latest` unpinned (as in compose).
- `k8s/base/nginx.conf` must be kept in sync with `infrastructure/nginx/default.conf` (owned by slice H) when that changes, e.g. Grafana under `/grafana/`.
- Observability (slice H) is not in Kubernetes yet: Prometheus will need its own NetworkPolicy allow rule to scrape `backend:8000` and Kubernetes service discovery instead of Docker DNS.
