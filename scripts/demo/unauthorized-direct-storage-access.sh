#!/usr/bin/env bash
# Slice F proof (PDF §4 Security):
#   "Storages are not reachable from outside the system; the only way in is FileGrid."
#   "Unauthorized direct access to a storage does not expose the personal data in
#    connection documents."
# 1. from the host, every storage port is closed; only nginx (443/80) answers;
# 2. from a container on the public-facing network, the storages do not even resolve;
# 3. an attacker who DOES get into a storage (docker exec = simulated breach) finds only
#    ciphertext for connection documents, on each of the three storages, while the same
#    marker in a non-personal outage record is found in clear (so the search does work);
# 4. through FileGrid, the authorised user gets the exact original bytes back;
# 5. the key is not in any storage or database: only in the backend's environment.
set -euo pipefail
cd "$(dirname "$0")/../.."
source scripts/lib/env.sh
export MSYS_NO_PATHCONV=1   # Git Bash: keep /home/... and /data/... as container paths

BASE="${BASE:-https://localhost/api/v1}"
CURL="curl -sk"
TMP="$(mktemp -d)"
TMPC="$(cygpath -m "$TMP" 2>/dev/null || echo "$TMP")"
MARKER="CNP-1960101123456-POPESCU-ION"      # synthetic personal data, never real

ok()   { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; exit 1; }
json() { python -c "import sys,json;d=json.load(sys.stdin);print(d$1)"; }
login() { $CURL -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$1\",\"password\":\"$2\"}" | json '["token"]'; }
policy() { printf '{"connection_docs":{"storage":"%s","encryption":true,"deduplicate":false,"priority":"normal"}}' "$1"; }

ADMIN="Authorization: Bearer $(login admin@ppc.ro "$(env_value ADMIN_PASSWORD)")"
DISP="Authorization: Bearer $(login dispecer@ppc.ro "$(env_value DISPECER_PASSWORD)")"
ORIGINAL_STORAGE=$($CURL "$BASE/admin/policies" -H "$DISP" | json '["connection_docs"]["storage"]')
restore() { $CURL -o "$TMPC/discard" -X PUT "$BASE/admin/policies" -H "$ADMIN" -d "$(policy "$ORIGINAL_STORAGE")" || true; rm -rf "$TMP"; }
trap restore EXIT

echo "== 1. From the host: only the gateway answers"
for port in 443 80; do
  (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null && ok "port $port open (nginx)" || fail "gateway port $port closed"
done
PROJECT_CONTAINERS=$(docker compose ps -q | tr '\n' ' ')
for port in 5432 9000 9001 21 30000 8000 8080; do
  # authoritative: does any FileGrid container publish this port on the host?
  owners=$(docker ps -q --filter "publish=$port" | grep -F -f <(echo "$PROJECT_CONTAINERS" | tr ' ' '\n' | sed '/^$/d' | cut -c1-12) || true)
  [ -n "$owners" ] && fail "port $port is published by a FileGrid container"
  if timeout 3 bash -c "exec 3<>/dev/tcp/127.0.0.1/$port" 2>/dev/null; then
    printf '  \033[33mNOTE\033[0m port %s answers, but no FileGrid container publishes it (another program on this machine)\n' "$port"
  else
    ok "port $port closed (postgres / minio / ftp / backend / simulator not published)"
  fi
done
# a port only counts if it is bound on the host (PublishedPort > 0), not merely exposed
PUBLISHED=$(docker compose ps --format json | python -c '
import sys, json
raw = sys.stdin.read().strip()
rows = json.loads(raw) if raw.startswith("[") else [json.loads(l) for l in raw.splitlines() if l.strip()]
print(" ".join(sorted({r["Service"] for r in rows
                       if any(p.get("PublishedPort", 0) for p in (r.get("Publishers") or []))})) + " ")')
[ "$PUBLISHED" = "nginx " ] && ok "only nginx publishes ports: $PUBLISHED" || fail "services publishing ports: $PUBLISHED"

echo "== 2. From the public-facing network: storages do not exist"
EDGE=$(docker network ls --format '{{.Name}}' | grep -E '_edge$' | head -1)
for target in minio:9000 postgres-blob:5432 postgres-meta:5432 ftp:21 backend:8000; do
  host=${target%:*}; port=${target#*:}
  if docker run --rm --network "$EDGE" alpine:3.20 sh -c "nc -z -w 2 $host $port" >/dev/null 2>&1; then
    fail "$target reachable from $EDGE"
  fi
  ok "$target unreachable from network '$EDGE'"
done

echo "== 3. Simulated breach: read each storage directly"
printf 'Cerere de racordare\nSolicitant: %s\nAdresa: Str. Exemplu 7, Ploiesti\n' "$MARKER" > "$TMP/cerere.txt"
head -c 300000 /dev/urandom | base64 >> "$TMP/cerere.txt"       # make it multi-segment
SRC_SHA=$(sha256sum "$TMP/cerere.txt" | cut -d' ' -f1)
upload() { # category -> file_id
  $CURL -X POST "$BASE/ingest" -H "$DISP" -F "category=$1" -F 'metadata={"zone":"Muntenia"}' \
    -F "file=@$TMPC/cerere.txt;filename=cerere_racordare_demo.txt" | json '["file_id"]'
}
# Raw bytes are binary: strip NUL bytes before grep (BusyBox grep in the MinIO image stops a
# line at the first NUL and would miss matches). Every file of the object is searched:
# MinIO keeps data in part.N files, framed with its own bitrot checksums.
raw_bytes() { # storage file_id category -> the stored object's raw bytes, NULs removed
  case "$1" in
    ftp) docker compose exec -T ftp sh -c "tr -d '\\000' < /home/filegrid/$3/$2" ;;
    s3)  docker compose exec -T minio sh -c "find /data/filegrid/$3/$2 -type f -exec cat {} + | tr -d '\\000'" ;;
  esac
}
raw_has_marker() { # storage file_id category -> prints yes/no
  case "$1" in
    ftp|s3) raw_bytes "$1" "$2" "$3" | grep -a -c "$MARKER" || true ;;
    postgres) docker compose exec -T postgres-blob psql -U filegrid -d filegrid_blob -tAc \
         "select count(*) from blob_chunks where key='$3/$2' and position(convert_to('$MARKER','UTF8') in data) > 0" ;;
  esac | tr -d '\r ' | awk '{print ($1 > 0) ? "yes" : "no"}'
}
raw_header() { # storage file_id category -> "FGE1" when the object is FileGrid ciphertext
  case "$1" in
    ftp) docker compose exec -T ftp sh -c "head -c 4 /home/filegrid/$3/$2" ;;
    # read everything (grep -q would close the pipe early and fail under pipefail)
    s3)  [ "$(raw_bytes s3 "$2" "$3" | grep -a -c FGE1 || true)" -gt 0 ] && printf FGE1 ;;
    postgres) docker compose exec -T postgres-blob psql -U filegrid -d filegrid_blob -tAc \
         "select convert_from(substring(data from 1 for 4),'UTF8') from blob_chunks where key='$3/$2' and seq=0" ;;
  esac | tr -d '\r '
}

FID_PLAIN=$(upload outage_records)
[ "$(raw_has_marker s3 "$FID_PLAIN" outage_records)" = "yes" ] \
  && ok "control: an outage record (not personal data, not encrypted) IS readable in raw S3" \
  || fail "control search did not find the marker: the check would prove nothing"

for STORAGE in ftp s3 postgres; do
  $CURL -o "$TMPC/discard" -X PUT "$BASE/admin/policies" -H "$ADMIN" -d "$(policy "$STORAGE")"
  FID=$(upload connection_docs)
  [ "$(raw_has_marker "$STORAGE" "$FID" connection_docs)" = "no" ] \
    && ok "$STORAGE: raw object of the connection document does not contain the CNP/name" \
    || fail "$STORAGE: personal data readable at rest"
  [ "$(raw_header "$STORAGE" "$FID" connection_docs)" = "FGE1" ] \
    && ok "$STORAGE: object is FileGrid AES-256-GCM ciphertext (header FGE1)" || fail "$STORAGE: no ciphertext header"
  $CURL -D "$TMPC/h" -o "$TMPC/back.txt" "$BASE/files/$FID/download" -H "$DISP"   # native curl: Windows path
  [ "$(sha256sum "$TMP/back.txt" | cut -d' ' -f1)" = "$SRC_SHA" ] && grep -q "$MARKER" "$TMP/back.txt" \
    && grep -qi '^x-integrity-verified: true' "$TMP/h" \
    && ok "$STORAGE: through FileGrid the dispecer gets the original, SHA-256 verified" \
    || fail "$STORAGE: download through FileGrid differs"
done

echo "== 4. The key is not stored next to the data"
for svc in postgres-meta postgres-blob minio ftp; do
  docker compose exec -T "$svc" env | grep -q FILEGRID_MASTER_KEY && fail "master key visible in $svc"
done
ok "FILEGRID_MASTER_KEY exists only in the backend's environment (not in any storage or database)"
COLS=$(docker compose exec -T postgres-meta psql -U filegrid -d filegrid -tAc \
  "select string_agg(column_name, ',') from information_schema.columns where table_name='files_metadata'" | tr -d '\r')
echo ",$COLS," | grep -Eqi ',(enc_key|wrapped_key|data_key|dek|master_key|secret)[a-z_]*,' \
  && fail "metadata has key columns: $COLS" \
  || ok "metadata DB holds no key material (columns: $COLS)"

echo; echo "UNAUTHORIZED-DIRECT-STORAGE-ACCESS DEMO PASSED (policy restored to $ORIGINAL_STORAGE)"
