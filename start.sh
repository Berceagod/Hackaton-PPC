#!/usr/bin/env bash
# FileGrid — one-command start
# Linux & WSL:  ./start.sh
# Windows:      .\start.ps1   or   start.cmd
# (PowerShell cannot execute .sh — it opens the file.)
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is not installed. Install Docker (Linux) or Docker Desktop (Windows/WSL), then retry." >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker engine is not running. Start Docker Desktop / dockerd, then retry." >&2
  exit 1
fi

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
  body="$(curl -sk --max-time 3 https://localhost/api/v1/health 2>/dev/null || true)"
  if echo "$body" | grep -Eq '"status":"(ok|degraded)"'; then
    echo
    echo "FileGrid is up:  https://localhost   (self-signed certificate)"
    echo "Login:           dispecer@ppc.ro / dispecer   or   admin@ppc.ro / admin"
    echo "$body" | grep -q '"status":"degraded"' && echo "Note: /health is degraded (a storage may still be starting)."
    exit 0
  fi
  sleep 2
done
echo "Timed out waiting for health. Check: docker compose ps && docker compose logs nginx && docker compose logs backend" >&2
exit 1
