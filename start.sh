#!/usr/bin/env bash
# One-command start: generates secrets on first run, builds and starts everything.
set -euo pipefail
cd "$(dirname "$0")"

rand() { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }

# Generate any missing secret (idempotent: existing values are never overwritten)
touch .env; chmod 600 .env
ensure() { grep -q "^$1=" .env || { echo ">> generating $1"; echo "$1=$2" >> .env; }; }
ensure POSTGRES_META_PASSWORD "$(rand)"
ensure POSTGRES_BLOB_PASSWORD "$(rand)"
ensure MINIO_ROOT_USER "fg$(rand | head -c 10)"
ensure MINIO_ROOT_PASSWORD "$(rand)"
ensure FTP_USER filegrid
ensure FTP_PASSWORD "$(rand)"
ensure JWT_SECRET "$(rand)$(rand)"
ensure SIMULATOR_TOKEN "$(rand)"
ensure DISPECER_PASSWORD dispecer
ensure ADMIN_PASSWORD admin

echo ">> Building and starting FileGrid"
docker compose up -d --build "$@"

echo ">> Waiting for the gateway to become healthy"
for _ in $(seq 1 90); do
  if curl -sk --max-time 3 https://localhost/api/v1/health 2>/dev/null | grep -q '"status":"ok"'; then
    echo
    echo "FileGrid is up:  https://localhost   (self-signed certificate)"
    echo "Login:           dispecer@ppc.ro / dispecer   or   admin@ppc.ro / admin"
    exit 0
  fi
  sleep 2
done
echo "Timed out waiting for health. Check: docker compose ps && docker compose logs backend" >&2
exit 1
