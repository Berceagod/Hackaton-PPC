#!/usr/bin/env bash
# Slice C proof (F7 + R3): retries and duplicates never create a second meter reading.
#   A. simulator burst: re-send the last N readings -> row count must not grow
#   B. race: the SAME reading sent 40x in parallel -> exactly one 202, exactly one row
#   C. the stored reading downloads byte-identical
# Usage: scripts/demo/duplicate-reading.sh [burst_size]   (default 200)
set -euo pipefail
cd "$(dirname "$0")/../.."

BASE="${BASE:-https://localhost/api/v1}"
BURST="${1:-200}"
CURL="curl -sk"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"; scripts/sim.sh resume >/dev/null 2>&1 || true' EXIT
TMPC="$(cygpath -m "$TMP" 2>/dev/null || echo "$TMP")"

ok()   { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; exit 1; }
sql()  { docker compose exec -T postgres-meta psql -U filegrid -d filegrid -tAc "$1" | tr -d '\r '; }
stat() { scripts/sim.sh status | python -c "import sys,json;d=json.load(sys.stdin);print(d$1)"; }
drain() { for _ in $(seq 1 60); do [ "$(stat '["in_flight"]')" = "0" ] && return 0; sleep 0.5; done; fail "simulator did not drain"; }
rows() { sql "select count(*) from meter_readings"; }

echo "== A. Simulator duplicate burst"
[ "$(stat '["counts"]["ok"]')" -gt 0 ] && ok "simulator is ingesting through https://nginx (ok=$(stat '["counts"]["ok"]'))" \
  || fail "simulator has not stored any reading yet"
scripts/sim.sh pause >/dev/null; drain
BEFORE_ROWS=$(rows); BEFORE_FILES=$(sql "select count(*) from files_metadata where category='meter_readings'")
BEFORE_DUP=$(stat '["counts"]["duplicate_rejected"]'); BEFORE_OK=$(stat '["counts"]["ok"]')
BEFORE_PENDING=$(sql "select count(*) from meter_readings where status<>'stored'")
ok "paused; rows in meter_readings = $BEFORE_ROWS"
SENT=$(scripts/sim.sh burst "$BURST" | python -c 'import sys,json;print(json.load(sys.stdin)["sent"])')
sleep 1; drain
AFTER_ROWS=$(rows); AFTER_FILES=$(sql "select count(*) from files_metadata where category='meter_readings'")
REJECTED=$(( $(stat '["counts"]["duplicate_rejected"]') - BEFORE_DUP ))
ACCEPTED=$(( $(stat '["counts"]["ok"]') - BEFORE_OK ))
ok "re-sent $SENT already-stored readings"
[ "$REJECTED" = "$SENT" ] && ok "all $SENT answered 409 (duplicate rejected)" || fail "$REJECTED of $SENT rejected"
[ "$ACCEPTED" = "0" ] && ok "0 accepted as new" || fail "$ACCEPTED duplicates were accepted"
[ "$AFTER_ROWS" = "$BEFORE_ROWS" ] && ok "meter_readings rows unchanged: $AFTER_ROWS" || fail "rows grew $BEFORE_ROWS -> $AFTER_ROWS"
[ "$AFTER_FILES" = "$BEFORE_FILES" ] && ok "files_metadata rows unchanged: $AFTER_FILES" || fail "files grew"
AFTER_PENDING=$(sql "select count(*) from meter_readings where status<>'stored'")
[ "$AFTER_PENDING" = "$BEFORE_PENDING" ] && ok "the burst left no reading pending (pending: $AFTER_PENDING before and after)" \
  || fail "pending readings changed $BEFORE_PENDING -> $AFTER_PENDING"
[ "$(sql "select count(*) from (select 1 from meter_readings group by meter_id, reading_ts having count(*)>1) d")" = "0" ] \
  && ok "no (meter_id, timestamp) appears twice" || fail "duplicate rows found"

echo "== B. Race: one brand-new reading sent 40 times in parallel"
SIM_TOKEN=$(grep '^SIMULATOR_TOKEN=' .env | cut -d= -f2)
TS="$(date -u +%Y-%m-%dT%H:%M:%S).000Z"; METER="MTR-RACE-$RANDOM"
printf '{"meter_id":"%s","timestamp":"%s","zone":"Banat","instant":{"voltage_v":229.7},"events":[],"sim_label":null}' "$METER" "$TS" > "$TMP/r.json"
for i in $(seq 1 40); do
  $CURL -o /dev/null -w '%{http_code}\n' -X POST "$BASE/ingest" -H "Authorization: Bearer $SIM_TOKEN" \
    -H "Idempotency-Key: $METER:$TS" -F category=meter_readings -F 'metadata={}' \
    -F "file=@$TMPC/r.json;type=application/json" > "$TMP/code.$i" &
done; wait
cat "$TMP"/code.* > "$TMP/codes"   # one file per request: parallel appends to one file lose lines
C202=$(grep -c '^202$' "$TMP/codes" || true); C409=$(grep -c '^409$' "$TMP/codes" || true); C503=$(grep -c '^503$' "$TMP/codes" || true)
ok "responses: 202 x$C202, 409 x$C409, 503(in flight, retry) x$C503"
[ "$C202" = "1" ] && ok "exactly one request was accepted" || fail "$C202 requests accepted"
[ $((C202 + C409 + C503)) = "40" ] && ok "every other request was refused (409/503), none errored" \
  || fail "unexpected status codes: $(sort "$TMP/codes" | uniq -c | tr '\n' ' ')"
[ "$(sql "select count(*) from meter_readings where meter_id='$METER'")" = "1" ] && ok "exactly one row for $METER" || fail "row count wrong"
CODE=$($CURL -o /dev/null -w '%{http_code}' -X POST "$BASE/ingest" -H "Authorization: Bearer $SIM_TOKEN" \
  -H "Idempotency-Key: $METER:$TS" -F category=meter_readings -F "file=@$TMPC/r.json")
[ "$CODE" = "409" ] && ok "a later retry gets 409" || fail "later retry got $CODE"

echo "== C. The stored reading is retrievable and byte-identical"
FID=$(sql "select file_id from meter_readings where meter_id='$METER'")
CODE=$($CURL -o /dev/null -w '%{http_code}' "$BASE/files/$FID/download" -H "Authorization: Bearer $SIM_TOKEN")
[ "$CODE" = "403" ] && ok "the simulator token cannot download (403): it may only ingest" || fail "service token download got $CODE"
USER_TOKEN=$($CURL -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
  -d '{"email":"dispecer@ppc.ro","password":"dispecer"}' | python -c 'import sys,json;print(json.load(sys.stdin)["token"])')
$CURL -D "$TMP/h" -o "$TMP/out.json" "$BASE/files/$FID/download" -H "Authorization: Bearer $USER_TOKEN"
cmp "$TMP/r.json" "$TMP/out.json" && ok "download byte-identical"
grep -qi '^x-integrity-verified: true' "$TMP/h" && ok "X-Integrity-Verified: true" || fail "integrity header missing"
ok "stored on: $(sql "select storage from files_metadata where id='$FID'") (policy for meter_readings)"

scripts/sim.sh resume >/dev/null; ok "simulator resumed"
echo; echo "DUPLICATE-READING DEMO PASSED"
