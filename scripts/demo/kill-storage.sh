#!/usr/bin/env bash
# Slice G proof (PDF §4 Reziliență): "a storage going down does not stop the other
# categories and does not lose files already confirmed".
#   A. postgres-blob (where meter readings go) is stopped while the simulator runs:
#      readings keep being accepted (202, kept in the outbox), an outage record goes to S3
#      as usual, and the simulator loses nothing. When the storage is back, every queued
#      reading is written out exactly once and is downloadable, integrity-verified.
#   B. ftp (connection documents) is stopped: those uploads get an honest 503 (nothing is
#      confirmed that is not stored), outage records on S3 keep working.
set -euo pipefail
cd "$(dirname "$0")/../.."
source scripts/lib/env.sh

BASE="${BASE:-https://localhost/api/v1}"
CURL="curl -sk"
TMP="$(mktemp -d)"
TMPC="$(cygpath -m "$TMP" 2>/dev/null || echo "$TMP")"
OUTAGE_S=${1:-20}
ok()   { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; exit 1; }
sql()  { MSYS_NO_PATHCONV=1 docker compose exec -T postgres-meta psql -U filegrid -d filegrid -tAc "$1" | tr -d '\r '; }
sim()  { bash scripts/sim.sh status | python -c "import sys,json;d=json.load(sys.stdin);print(d$1)"; }
restore() {
  docker compose start postgres-blob ftp >/dev/null 2>&1 || true
  bash scripts/sim.sh resume >/dev/null 2>&1 || true
  rm -rf "$TMP"
}
trap restore EXIT

DISP="Authorization: Bearer $($CURL -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"dispecer@ppc.ro\",\"password\":\"$(env_value DISPECER_PASSWORD)\"}" \
  | python -c 'import sys,json;print(json.load(sys.stdin)["token"])')"
upload() { # category -> http code
  head -c 200000 /dev/urandom > "$TMP/f.bin"
  $CURL -o "$TMPC/resp.json" -w '%{http_code}' -X POST "$BASE/ingest" -H "$DISP" \
    -F "category=$1" -F 'metadata={"zone":"Dobrogea"}' -F "file=@$TMPC/f.bin;filename=kill_storage_$1.bin"
}

METER_STORAGE=$($CURL "$BASE/admin/policies" -H "$DISP" | python -c 'import sys,json;print(json.load(sys.stdin)["meter_readings"]["storage"])')
[ "$METER_STORAGE" = "postgres" ] || fail "meter_readings policy points to '$METER_STORAGE', this demo stops postgres-blob (set it back to postgres)"

echo "== A. Stop the meter storage (postgres-blob) for ${OUTAGE_S} s while the simulator runs"
FAILED_BEFORE=$(sim '["counts"]["failed"]')
MARK=$(sql "select extract(epoch from now())")   # epoch: sql() strips spaces
docker compose stop postgres-blob >/dev/null
ok "postgres-blob stopped"
sleep "$OUTAGE_S"
QUEUED=$(sql "select count(*) from meter_readings where status='queued'")
[ "$QUEUED" -gt 0 ] && ok "$QUEUED readings accepted durably during the outage (status queued, 202 to the meters)" \
  || fail "no reading was queued"
CODE=$(upload outage_records); [ "$CODE" = "202" ] && ok "outage record -> S3 meanwhile: HTTP 202 (other categories unaffected)" \
  || fail "outage upload got $CODE"
[ "$(sim '["counts"]["failed"]')" = "$FAILED_BEFORE" ] && ok "simulator lost no reading (failed still $FAILED_BEFORE)" \
  || fail "simulator reports lost readings"
SAMPLE=$(sql "select file_id from meter_readings where status='queued' order by claimed_at limit 1")

echo "== B. Bring it back: the outbox drains"
docker compose start postgres-blob >/dev/null
for _ in $(seq 1 90); do
  LEFT=$(sql "select count(*) from meter_readings where status='queued'")
  [ "$LEFT" = "0" ] && break; sleep 2
done
[ "$LEFT" = "0" ] && ok "all queued readings written to postgres-blob" || fail "$LEFT readings still queued"
bash scripts/sim.sh pause >/dev/null; sleep 3   # count a stable state, not readings in flight
DRAINED=$(sql "select count(*) from meter_readings r join files_metadata f on f.id = r.file_id where r.status='stored' and r.created_at >= to_timestamp($MARK)")
ROWS=$(sql "select count(*) from meter_readings where created_at >= to_timestamp($MARK)")
[ "$DRAINED" = "$ROWS" ] && ok "every reading since the outage began has its file ($ROWS readings, 0 missing)" \
  || fail "readings without a file: $ROWS rows vs $DRAINED files"
[ "$(sql "select count(*) from meter_readings where status <> 'stored' and claimed_at < now() - interval '30 seconds'")" = "0" ] \
  && ok "no reading left pending or queued" || fail "stuck readings remain"
$CURL -D "$TMP/h" -o "$TMP/r.json" "$BASE/files/$SAMPLE/download" -H "$DISP"
grep -qi '^x-integrity-verified: true' "$TMP/h" && ok "a reading queued during the outage downloads with X-Integrity-Verified: true" \
  || fail "queued reading not downloadable: $(head -c 200 "$TMP/r.json")"

echo "== C. Stop FTP (connection documents) for a moment"
docker compose stop ftp >/dev/null
CODE=$(upload connection_docs)
[ "$CODE" = "503" ] && ok "connection document -> 503 (nothing confirmed that is not stored; the user retries)" \
  || fail "connection_docs got $CODE with FTP down"
CODE=$(upload outage_records); [ "$CODE" = "202" ] && ok "outage record -> S3: HTTP 202 while FTP is down" || fail "outage got $CODE"
docker compose start ftp >/dev/null; sleep 6
CODE=$(upload connection_docs)
[ "$CODE" = "202" ] && ok "FTP back: connection document -> 202" || fail "connection_docs after restart: $CODE"

echo; echo "KILL-STORAGE DEMO PASSED"
