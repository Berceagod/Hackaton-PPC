#!/usr/bin/env bash
# Start FileGrid on a local Kubernetes cluster (kind = Kubernetes in Docker). Slice J.
# Needs docker, kind, kubectl and the .env that ./start.sh creates: every secret is created
# from .env at deploy time, nothing secret is in the repo. Idempotent: run it again to update.
#   https://localhost:8443   (the compose stack keeps https://localhost)
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/lib/env.sh

CLUSTER=filegrid
NS=filegrid
METRICS_SERVER_VERSION=v0.7.2
source k8s/find-kind.sh
command -v kubectl >/dev/null || { echo "kubectl not found" >&2; exit 1; }
[ -f .env ] || { echo "No .env: run ./start.sh first (it generates the secrets)" >&2; exit 1; }
KUBECTL="kubectl --context kind-$CLUSTER"

echo ">> 1. cluster"
if ! "$KIND" get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  "$KIND" create cluster --config k8s/kind-cluster.yaml --wait 120s
fi

echo ">> 2. images (the same Dockerfiles as docker-compose), loaded into the cluster"
docker build -q -t filegrid/backend:k8s backend >/dev/null
docker build -q -t filegrid/simulator:k8s simulator >/dev/null
docker build -q -t filegrid/nginx:k8s -f infrastructure/nginx/Dockerfile --build-arg VITE_USE_MOCK=false . >/dev/null
docker image inspect filegrid-minio:local >/dev/null 2>&1 || docker build -q -t filegrid-minio:local infrastructure/minio >/dev/null
for img in filegrid/backend:k8s filegrid/simulator:k8s filegrid/nginx:k8s filegrid-minio:local; do
  "$KIND" load docker-image "$img" --name "$CLUSTER" >/dev/null
done

echo ">> 3. metrics-server (CPU metrics for the HorizontalPodAutoscaler)"
$KUBECTL apply -f "https://github.com/kubernetes-sigs/metrics-server/releases/download/${METRICS_SERVER_VERSION}/components.yaml" >/dev/null
# kind's kubelets use self-signed certificates
if ! $KUBECTL -n kube-system get deploy metrics-server -o jsonpath='{.spec.template.spec.containers[0].args}' | grep -q kubelet-insecure-tls; then
  $KUBECTL -n kube-system patch deployment metrics-server --type=json \
    -p '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]' >/dev/null
fi

echo ">> 4. namespace, secrets (from .env) and policies"
$KUBECTL apply -f k8s/base/namespace.yaml >/dev/null
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
# kubectl and openssl may be native Windows programs (Git Bash): give them Windows paths
TMPW="$(cygpath -m "$TMP" 2>/dev/null || echo "$TMP")"
v() { env_value "$1"; }
apply_secret() { # name env-file
  $KUBECTL -n "$NS" create secret generic "$1" --from-env-file="$2" --dry-run=client -o yaml | $KUBECTL apply -f - >/dev/null
}
umask 077
cat > "$TMP/storage.env" <<EOF
POSTGRES_META_PASSWORD=$(v POSTGRES_META_PASSWORD)
POSTGRES_BLOB_PASSWORD=$(v POSTGRES_BLOB_PASSWORD)
MINIO_ROOT_USER=$(v MINIO_ROOT_USER)
MINIO_ROOT_PASSWORD=$(v MINIO_ROOT_PASSWORD)
FTP_USER=$(v FTP_USER)
FTP_PASSWORD=$(v FTP_PASSWORD)
EOF
cat > "$TMP/backend.env" <<EOF
DATABASE_URL=postgresql://filegrid:$(v POSTGRES_META_PASSWORD)@postgres-meta:5432/filegrid
BLOB_DATABASE_URL=postgresql://filegrid:$(v POSTGRES_BLOB_PASSWORD)@postgres-blob:5432/filegrid_blob
S3_ACCESS_KEY=$(v MINIO_ROOT_USER)
S3_SECRET_KEY=$(v MINIO_ROOT_PASSWORD)
FTP_USER=$(v FTP_USER)
FTP_PASSWORD=$(v FTP_PASSWORD)
JWT_SECRET=$(v JWT_SECRET)
SIMULATOR_TOKEN=$(v SIMULATOR_TOKEN)
DISPECER_PASSWORD=$(v DISPECER_PASSWORD)
ADMIN_PASSWORD=$(v ADMIN_PASSWORD)
FILEGRID_MASTER_KEY=$(v FILEGRID_MASTER_KEY)
EOF
echo "API_TOKEN=$(v SIMULATOR_TOKEN)" > "$TMP/simulator.env"
apply_secret filegrid-storage "$TMPW/storage.env"
apply_secret filegrid-backend "$TMPW/backend.env"
apply_secret filegrid-simulator "$TMPW/simulator.env"
# TLS: created once and kept (the simulator trusts this certificate)
if ! $KUBECTL -n "$NS" get secret filegrid-tls >/dev/null 2>&1; then
  MSYS_NO_PATHCONV=1 openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
    -keyout "$TMPW/tls.key" -out "$TMPW/tls.crt" -subj "/CN=filegrid.local/O=FileGrid" \
    -addext "subjectAltName=DNS:localhost,DNS:nginx,DNS:nginx.filegrid.svc.cluster.local,IP:127.0.0.1" >/dev/null 2>&1
  $KUBECTL -n "$NS" create secret tls filegrid-tls --cert="$TMPW/tls.crt" --key="$TMPW/tls.key" >/dev/null
fi
$KUBECTL -n "$NS" create configmap filegrid-policies --from-file=policies.yaml \
  --dry-run=client -o yaml | $KUBECTL apply -f - >/dev/null

echo ">> 5. FileGrid"
$KUBECTL apply -k k8s/base >/dev/null
$KUBECTL -n "$NS" rollout restart deployment/backend deployment/nginx deployment/simulator >/dev/null  # pick up new images/secrets
for r in statefulset/postgres-meta statefulset/postgres-blob statefulset/minio statefulset/ftp \
         deployment/backend deployment/nginx deployment/simulator; do
  $KUBECTL -n "$NS" rollout status "$r" --timeout=300s >/dev/null
done
$KUBECTL -n "$NS" get pods -o wide
echo
echo "FileGrid on Kubernetes: https://localhost:8443   (self-signed certificate)"
echo "Login: dispecer@ppc.ro / admin@ppc.ro with the passwords from .env (printed by ./start.sh)"
echo "Verify: k8s/verify.sh    Remove: k8s/down.sh"
