#!/usr/bin/env bash
# Slice B proof (F2 + F3): the storage of a category is changed at runtime, with NO
# restart and NO code change, and the same category is stored/retrieved byte-identically
# on each of the three separate storages: postgres, ftp, s3.
# Usage: scripts/demo/policy-change.sh [size_mb]   (default 7 MB)
set -euo pipefail
cd "$(dirname "$0")/../.."

BASE="${BASE:-https://localhost/api/v1}"
SIZE_MB="${1:-7}"
CURL="curl -sk"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
TMPC="$(cygpath -m "$TMP" 2>/dev/null || echo "$TMP")"

ok()   { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; exit 1; }
json() { python -c "import sys,json;print(json.load(sys.stdin)$1)"; }
login() { $CURL -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$1\",\"password\":\"$2\"}" | json '["token"]'; }
policy() { printf '{"outage_records":{"storage":"%s","encryption":false,"deduplicate":false,"priority":"high"}}' "$1"; }

ADMIN="Authorization: Bearer $(login admin@ppc.ro admin)"
DISP="Authorization: Bearer $(login dispecer@ppc.ro dispecer)"
STARTED=$(docker compose ps -q backend | xargs docker inspect -f '{{.State.StartedAt}}')

echo "== 1. Only an admin may change policies"
CODE=$($CURL -o /dev/null -w '%{http_code}' -X PUT "$BASE/admin/policies" -H "$DISP" -d "$(policy ftp)")
[ "$CODE" = "403" ] && ok "dispecer -> 403" || fail "expected 403, got $CODE"

echo "== 2. An invalid policy is rejected"
CODE=$($CURL -o /dev/null -w '%{http_code}' -X PUT "$BASE/admin/policies" -H "$ADMIN" -d "$(policy tape)")
[ "$CODE" = "422" ] && ok "storage=tape -> 422" || fail "expected 422, got $CODE"

head -c $((SIZE_MB * 1024 * 1024)) /dev/urandom > "$TMP/src.bin"
SRC_SHA=$(sha256sum "$TMP/src.bin" | cut -d' ' -f1)

for STORAGE in postgres ftp s3; do
  echo "== 3. outage_records -> $STORAGE (hot reload)"
  $CURL -X PUT "$BASE/admin/policies" -H "$ADMIN" -d "$(policy "$STORAGE")" | grep -q "\"storage\":\"$STORAGE\"" \
    && ok "policy updated" || fail "policy update failed"
  RESP=$($CURL -X POST "$BASE/ingest" -H "$DISP" -F category=outage_records \
    -F "file=@$TMPC/src.bin;filename=avarie_$STORAGE.bin")
  [ "$(echo "$RESP" | json '["storage"]')" = "$STORAGE" ] && ok "stored on $STORAGE" || fail "wrong storage: $RESP"
  [ "$(echo "$RESP" | json '["sha256"]')" = "$SRC_SHA" ] && ok "SHA-256 matches" || fail "sha mismatch on $STORAGE"
  FID=$(echo "$RESP" | json '["file_id"]')
  $CURL -D "$TMP/h" -o "$TMP/out.bin" "$BASE/files/$FID/download" -H "$DISP"
  cmp "$TMP/src.bin" "$TMP/out.bin" && ok "download byte-identical from $STORAGE"
  grep -qi '^x-integrity-verified: true' "$TMP/h" && ok "X-Integrity-Verified: true" || fail "integrity header missing"
  eval "FID_$STORAGE=$FID"
done

echo "== 4. Files stored before a policy change stay readable where they were written"
$CURL -o "$TMP/old.bin" "$BASE/files/$FID_postgres/download" -H "$DISP"
cmp "$TMP/src.bin" "$TMP/old.bin" && ok "postgres-stored file still served after policy moved to s3"

echo "== 5. Each storage really holds one of the files"
for STORAGE in postgres ftp s3; do
  N=$($CURL "$BASE/files?category=outage_records&storage=$STORAGE&q=avarie_$STORAGE" -H "$DISP" | python -c 'import sys,json;print(len(json.load(sys.stdin)))')
  [ "$N" -ge 1 ] && ok "$STORAGE: $N file(s) listed" || fail "$STORAGE holds nothing"
done
ROWS=$(docker compose exec -T postgres-blob psql -U filegrid -d filegrid_blob -tAc \
  "select count(*) from blob_chunks where key='outage_records/$FID_postgres'" | tr -d '\r ')
[ "$ROWS" -ge 1 ] && ok "$ROWS chunk row(s) present in postgres-blob" || fail "no rows in postgres-blob"
docker compose exec -T ftp sh -c "test -s /home/filegrid/outage_records/$FID_ftp" \
  && ok "file present on the FTP server disk" || fail "file missing on the FTP server"

echo "== 6. No restart happened"
NOW=$(docker compose ps -q backend | xargs docker inspect -f '{{.State.StartedAt}}')
[ "$STARTED" = "$NOW" ] && ok "backend container not restarted ($NOW)" || fail "backend restarted"

echo "== 7. Policy is persisted (GET returns s3, the last value set)"
$CURL "$BASE/admin/policies" -H "$DISP" | json '["outage_records"]["storage"]' | grep -q s3 \
  && ok "persisted policy = s3 (default restored)" || fail "policy not persisted"

echo; echo "POLICY-CHANGE DEMO PASSED"
