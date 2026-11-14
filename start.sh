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
# AES-256 master key for personal data at rest (ADR-0011). Back up .env: without this key
# encrypted documents cannot be read. Never commit it.
ensure FILEGRID_MASTER_KEY "$(rand)"
# UI accounts get random passwords (no password lives in the repository, ADR-0012).
# Installations created before this change used the literal demo passwords: replace them.
sed -i -e '/^DISPECER_PASSWORD=dispecer$/d' -e '/^ADMIN_PASSWORD=admin$/d' .env
ensure DISPECER_PASSWORD "$(rand | head -c 16)"
ensure ADMIN_PASSWORD "$(rand | head -c 16)"

# ---- slice H (observability) secrets: ensure KEY "$(rand)" (also in start.ps1): add directly below this line ----
# Login for Grafana (https://localhost/grafana/, user "admin"); read it from .env.
_graf="$(rand)"; ensure GRAFANA_ADMIN_PASSWORD "${_graf:0:20}"
# Not a secret: which compose project's container logs go to Loki (its name is the directory's).
ensure LOKI_COMPOSE_PROJECT "${COMPOSE_PROJECT_NAME:-$(basename "$PWD" | tr 'A-Z' 'a-z' | tr -cd 'a-z0-9_-')}"

# ---- slice I (analytics) secrets: ensure KEY "$(rand)" (also in start.ps1): add directly below this line ----

# ---- slice J (scale-out) secrets: ensure KEY "$(rand)" (also in start.ps1): add directly below this line ----

echo ">> Building and starting FileGrid"
docker compose up -d --build "$@"

echo ">> Waiting for FileGrid to come up"
pw() { grep -E "^$1=" .env | cut -d= -f2- | tr -d '\r'; }
for _ in $(seq 1 90); do
  # /api/v1/livez is public and reveals nothing; the storage report needs a login (slice D)
  if curl -sk --max-time 3 https://localhost/api/v1/livez 2>/dev/null | grep -q '"status":"alive"'; then
    echo
    echo "FileGrid is up:  https://localhost   (self-signed certificate)"
    echo "Login:           dispecer@ppc.ro / $(pw DISPECER_PASSWORD)"
    echo "                 admin@ppc.ro    / $(pw ADMIN_PASSWORD)"
    echo "                 (generated for this machine, stored only in .env)"
    deps="$(docker compose exec -T backend python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8000/health', timeout=10).read().decode())" 2>/dev/null || true)"
    echo "$deps" | grep -q '"status":"degraded"' && echo "Note: a storage is still starting or down: $deps"
    exit 0
  fi
  sleep 2
done
echo "Timed out waiting for FileGrid. Check: docker compose ps && docker compose logs nginx && docker compose logs backend" >&2
exit 1
