#!/usr/bin/env bash
# Proof (improvements.md 2.1): break things on purpose and watch the system survive.
# Uses the chaos console API (admin only, audited, every fault expires by itself). It needs
#   CHAOS_ENABLED=true   (off in a default install):   CHAOS_ENABLED=true docker compose up -d backend
#
#   1. A storage "goes down" (S3): outage uploads are refused cleanly (503), another category
#      (connection documents, on FTP) keeps working, and S3 comes back by itself afterwards.
#   2. The meter storage (Postgres blob) goes down: readings are still accepted (202) and
#      queued, then drained when it returns: none lost, none duplicated.
#   3. The network degrades (400 ms latency, cut connections): requests visibly slow down.
#   4. A forgotten fault expires on its own.
# This simulates the outage inside the application; scripts/demo/kill-storage.sh stops the
# real containers. Exit code 77 = skipped (chaos console is off).
#
# Usage: scripts/demo/chaos.sh      Overrides (another stack): BASE, ENV_FILE. No `bash -x`.
set -euo pipefail
cd "$(dirname "$0")/../.."

BASE="${BASE:-https://localhost/api/v1}"
ENV_FILE="${ENV_FILE:-.env}"
CURL="curl -sk --max-time 20"   # a cut connection must never hang the demo
TMP="$(mktemp -d)"
TMPC="$(cygpath -m "$TMP" 2>/dev/null || echo "$TMP")"

ok()   { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; exit 1; }
note() { printf '       %s\n' "$*"; }
env_value() { grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '\r'; }
json() { python -c "import sys,json;d=json.load(sys.stdin);print($1)"; }
sql()  { MSYS_NO_PATHCONV=1 docker compose exec -T -e PGOPTIONS="-c client_min_messages=warning" postgres-meta psql -U filegrid -d filegrid -tAc "$1" | tr -d '\r'; }
[ -f "$ENV_FILE" ] || fail "no $ENV_FILE: run ./start.sh first"
login() { $CURL -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$1\",\"password\":\"$2\"}" | python -c 'import sys,json;print(json.load(sys.stdin)["token"])'; }
ADMIN="Authorization: Bearer $(login admin@ppc.ro "$(env_value ADMIN_PASSWORD)")"
SIM="Authorization: Bearer $(env_value SIMULATOR_TOKEN)"

ENABLED=$($CURL "$BASE/admin/chaos" -H "$ADMIN" | json 'd["enabled"]')
if [ "$ENABLED" != "True" ]; then
  echo "Chaos console is OFF (default). Enable it, then re-run:"
  echo "    CHAOS_ENABLED=true docker compose up -d backend     (and restart toxiproxy: docker compose restart toxiproxy)"
  exit 77
fi

chaos() { $CURL -o /dev/null -w '%{http_code}' -X "$1" "$BASE/admin/chaos$2" -H "$ADMIN" -H 'Content-Type: application/json' ${3:+-d "$3"}; }
panic() { for _ in 1 2 3 4 5 6; do [ "$(chaos DELETE "")" = "200" ] && return 0; sleep 2; done; return 1; }
trap 'panic || true; rm -rf "$TMP"' EXIT
head -c 50000 /dev/urandom > "$TMP/f.bin"
upload() { # $1 category -> prints the HTTP status
  $CURL -o /dev/null -w '%{http_code}' -X POST "$BASE/ingest" -H "$ADMIN" -F "category=$1" -F "file=@$TMPC/f.bin;filename=chaos_demo.bin"; }
wait_code() { # $1 category $2 wanted $3 max seconds
  local start=$(date +%s) code
  while true; do
    code=$(upload "$1"); [ "$code" = "$2" ] && return 0
    [ $(( $(date +%s) - start )) -ge "$3" ] && { echo "last status: $code"; return 1; }
    sleep 2
  done
}

echo "== 0. Baseline"
[ "$(upload outage_records)" = "202" ] && ok "outage upload works: 202" || fail "baseline upload failed"

echo "== 1. S3 goes down (simulated, 60 s): the other categories keep working"
[ "$(chaos PUT /storage/s3 '{"kind":"down","duration_seconds":60}')" = "200" ] && ok "S3 taken down through the console" || fail "console refused"
sleep 2
C=$(upload outage_records); [ "$C" = "503" ] && ok "outage upload: 503, refused cleanly (nothing half-stored)" || fail "expected 503, got $C"
C=$(upload connection_docs); [ "$C" = "202" ] && ok "connection documents (FTP) still accepted: 202. One storage down does not stop another category" || fail "FTP category affected: $C"
[ "$(chaos DELETE /storage/s3)" = "200" ] && ok "S3 brought back"
wait_code outage_records 202 40 && ok "outage uploads work again (circuit breaker closed by itself)" || fail "S3 did not recover"

echo "== 2. The meter readings' storage goes down: readings are queued, then delivered"
METER_STORE=$($CURL "$BASE/admin/policies" -H "$ADMIN" | json 'd["meter_readings"]["storage"]')   # whatever the policy says today
[ "$(chaos PUT /storage/$METER_STORE '{"kind":"down","duration_seconds":60}')" = "200" ] && ok "the meter readings' storage ($METER_STORE) taken down" || fail "console refused"
sleep 2
TS="$(date -u +%Y-%m-%dT%H:%M:%S)"; TAG="MTR-CHAOS-$RANDOM"; ACCEPTED=0
for i in 1 2 3 4 5; do
  printf '{"meter_id":"%s-%s","timestamp":"%s.%03dZ","zone":"Banat","instant":{"voltage_v":230.1},"events":[],"sim_label":null}' "$TAG" "$i" "$TS" "$i" > "$TMP/r$i.json"
  C=$($CURL -o /dev/null -w '%{http_code}' -X POST "$BASE/ingest" -H "$SIM" -H "Idempotency-Key: $TAG-$i:$TS.00${i}Z" \
        -F category=meter_readings -F 'metadata={}' -F "file=@$TMPC/r$i.json;type=application/json")
  [ "$C" = "202" ] && ACCEPTED=$((ACCEPTED + 1)) || note "reading $i answered $C"
done
[ "$ACCEPTED" = "5" ] && ok "5 of 5 readings accepted (202) while the storage is down" || fail "only $ACCEPTED of 5 accepted"
QUEUED=$(sql "select count(*) from meter_readings where meter_id like '$TAG-%' and status <> 'stored'")
[ "$QUEUED" = "5" ] && ok "all 5 are waiting in the outbox: acknowledged to the meter, held until the storage returns" || fail "expected 5 queued readings, found $QUEUED (is the storage really unavailable?)"
chaos DELETE /storage/$METER_STORE >/dev/null
START=$(date +%s)
until [ "$(sql "select count(*) from meter_readings where meter_id like '$TAG-%' and status = 'stored'")" = "5" ]; do
  [ $(( $(date +%s) - START )) -ge 90 ] && fail "readings were not delivered after the storage returned"
  sleep 3
done
ok "all 5 readings delivered after the storage returned ($(( $(date +%s) - START )) s), none lost"
[ "$(sql "select count(*) from (select 1 from meter_readings where meter_id like '$TAG-%' group by meter_id, reading_ts having count(*)>1) d")" = "0" ] && ok "none duplicated" || fail "duplicates found"

echo "== 3. The network degrades: requests slow down visibly"
BASE_T=$($CURL -o /dev/null -w '%{time_total}' "$BASE/livez")
[ "$(chaos PUT /network '{"mode":"bad","duration_seconds":30}')" = "200" ] && ok "network degraded (400 ms latency each way, 20% of connections cut)"
SLOW=0
for _ in 1 2 3 4 5 6; do
  T=$($CURL -o /dev/null -w '%{time_total}' --max-time 10 "$BASE/livez" || echo 0)
  python -c "import sys; sys.exit(0 if float('$T') > 0.5 else 1)" && { SLOW=$T; break; }
done
[ "$SLOW" != "0" ] && ok "a plain request took ${SLOW}s instead of ${BASE_T}s" || fail "no slowdown observed"
for _ in 1 2 3 4 5 6; do [ "$(chaos PUT /network '{"mode":"ok"}')" = "200" ] && break; sleep 1; done
sleep 1
T=$($CURL -o /dev/null -w '%{time_total}' "$BASE/livez"); ok "network restored: ${T}s"

echo "== 4. A forgotten fault expires by itself"
chaos PUT /storage/s3 '{"kind":"down","duration_seconds":5}' >/dev/null
sleep 2; C=$(upload outage_records); [ "$C" = "503" ] && ok "S3 is down (503)" || fail "expected 503, got $C"
wait_code outage_records 202 40 && ok "after the 5 s were up it recovered with nobody touching it" || fail "fault did not expire"

echo "== Every use is in the audit trail (and the chain still verifies)"
$CURL "$BASE/admin/chaos" -H "$ADMIN" | python -c '
import sys, json
for e in reversed(json.load(sys.stdin)["recent"][:8]):
    print("       %-13s %s" % (e["user"], e["details"]))'
[ "$($CURL "$BASE/audit/verify" -H "$ADMIN" | json 'd["valid"]')" = "True" ] && ok "audit chain valid" || fail "audit chain broken"
echo
echo "CHAOS DEMO PASSED"
