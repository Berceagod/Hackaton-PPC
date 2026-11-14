#!/usr/bin/env bash
# Slice G proof (PDF §4 Reziliență): "on a connection with high latency and packet loss,
# transfers finish or resume without manual intervention", and retries never duplicate
# a meter reading.
#   1. degrade the network between gateway and backend (toxiproxy): 400±200 ms latency,
#      2 MB/s upload, 20% of connections cut after 0.5 s;
#   2. upload a drone video in 5 MB chunks like the UI does: a cut chunk is resent, and
#      after repeated failures the client asks the server what is missing and resumes;
#   3. meanwhile the meter simulator keeps sending: failed readings go to its backlog;
#   4. restore the network: the video is byte-identical, the backlog drains, nothing lost,
#      no reading stored twice.
# Usage: scripts/demo/slow-network.sh [size_mb]   (default 40)
set -euo pipefail
cd "$(dirname "$0")/../.."
source scripts/lib/env.sh

BASE="${BASE:-https://localhost/api/v1}"
SIZE_MB="${1:-40}"
CHUNK_MB=5
CURL="curl -sk --max-time 60"
TMP="$(mktemp -d)"
TMPC="$(cygpath -m "$TMP" 2>/dev/null || echo "$TMP")"
ok()   { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; exit 1; }
json() { python -c "import sys,json;d=json.load(open(sys.argv[1]));print($2)" "$1"; }
sim()  { bash scripts/sim.sh status | python -c "import sys,json;d=json.load(sys.stdin);print(d$1)"; }
sql()  { MSYS_NO_PATHCONV=1 docker compose exec -T postgres-meta psql -U filegrid -d filegrid -tAc "$1" | tr -d '\r '; }
cleanup() { bash scripts/chaos/network-ok.sh >/dev/null 2>&1 || true; rm -rf "$TMP"; }
trap cleanup EXIT

DISP="Authorization: Bearer $(curl -sk -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"dispecer@ppc.ro\",\"password\":\"$(env_value DISPECER_PASSWORD)\"}" \
  | python -c 'import sys,json;print(json.load(sys.stdin)["token"])')"

head -c $((SIZE_MB * 1024 * 1024)) /dev/urandom > "$TMP/video.bin"
SRC_SHA=$(sha256sum "$TMP/video.bin" | cut -d' ' -f1)
SIZE=$(stat -c %s "$TMP/video.bin")
TOTAL=$(( (SIZE + CHUNK_MB * 1024 * 1024 - 1) / (CHUNK_MB * 1024 * 1024) ))
UPLOAD_ID=$(python -c 'import uuid;print(uuid.uuid4())')
FAILED_BEFORE=$(sim '["counts"]["failed"]')
DEFERRED_BEFORE=$(sim '["counts"]["deferred"]')
RETRIES_BEFORE=$(sim '["retries"]')
DROPPED_BEFORE=$(sim '["counts"]["dropped"]')

echo "== 1. Degrade the network"
bash scripts/chaos/network-bad.sh > "$TMP/chaos.txt"   # not "| head": closing the pipe early fails under pipefail
head -1 "$TMP/chaos.txt"

send_chunk() { # index -> HTTP code (000 = connection cut / timeout)
  local i=$1
  dd if="$TMP/video.bin" of="$TMP/chunk.bin" bs=1M skip=$((i * CHUNK_MB)) count=$CHUNK_MB status=none
  $CURL -o "$TMPC/resp.json" -w '%{http_code}' -X POST "$BASE/upload/chunk" -H "$DISP" \
    -F "upload_id=$UPLOAD_ID" -F "chunk_index=$i" -F "total_chunks=$TOTAL" \
    -F "filename=inspectie_drona_retea_proasta_${SIZE_MB}MB.mp4" -F "category=drone_videos" \
    -F 'metadata={"zone":"Banat","station":"LEA-110kV-Timisoara"}' \
    -F "sha256=$SRC_SHA" -F "size_bytes=$SIZE" -F "chunk_sha256=$(sha256sum "$TMP/chunk.bin" | cut -d' ' -f1)" \
    -F "file_chunk=@$TMPC/chunk.bin;filename=chunk" 2>/dev/null || echo 000
}

echo "== 2. Upload a ${SIZE_MB} MB drone video in ${TOTAL} chunks over the bad link"
START=$(date +%s); RETRIES=0; RESUMES=0
for ((i = 0; i < TOTAL; i++)); do
  attempts=0
  while true; do
    code=$(send_chunk "$i"); code=${code: -3}
    [ "$code" = "200" ] && break
    attempts=$((attempts + 1)); RETRIES=$((RETRIES + 1))
    printf '     chunk %d: HTTP %s (connection cut or slow), resending\n' "$i" "$code"
    if [ "$attempts" -ge 3 ]; then    # resume: ask the server what it already has
      RESUMES=$((RESUMES + 1))
      curl -sk --max-time 30 -o "$TMPC/st.json" "$BASE/upload/$UPLOAD_ID" -H "$DISP" || true
      printf '     resume: server is missing %s\n' "$(json "$TMP/st.json" 'd.get("missing")' 2>/dev/null || echo '?')"
      attempts=0
      # only the answer was lost on the way back: the server already has this chunk
      if [ "$(json "$TMP/st.json" "$i not in d.get('missing', [$i])" 2>/dev/null)" = "True" ]; then
        printf '     chunk %d already stored on the server: moving on\n' "$i"
        [ "$i" -eq $((TOTAL - 1)) ] && cp "$TMP/st.json" "$TMP/resp.json"
        break
      fi
    fi
    [ "$RETRIES" -lt 60 ] || fail "too many retries"
    sleep 1
  done
done
[ "$(json "$TMP/resp.json" 'd["status"]')" = "complete" ] || fail "upload not complete: $(cat "$TMP/resp.json")"
FILE_ID=$(json "$TMP/resp.json" 'd["file_id"]')
ok "upload finished in $(( $(date +%s) - START )) s with $RETRIES resent chunks and $RESUMES resumes, no manual step"
[ "$(json "$TMP/resp.json" 'd["sha256"]')" = "$SRC_SHA" ] && ok "server SHA-256 == local SHA-256" || fail "sha mismatch"
DEFERRED=$(( $(sim '["counts"]["deferred"]') - DEFERRED_BEFORE ))
SIM_RETRIES=$(( $(sim '["retries"]') - RETRIES_BEFORE ))
ok "meanwhile the meters retried $SIM_RETRIES sends and deferred $DEFERRED readings to their backlog (kept, not lost)"

echo "== 3. Restore the network"
bash scripts/chaos/network-ok.sh
for _ in $(seq 1 60); do [ "$(sim '["backlog"]')" = "0" ] && break; sleep 2; done
[ "$(sim '["backlog"]')" = "0" ] && ok "simulator backlog drained ($(sim '["counts"]["redelivered"]') redelivered in total)" \
  || fail "backlog still $(sim '["backlog"]')"
[ "$(sim '["counts"]["failed"]')" = "$FAILED_BEFORE" ] && [ "$(sim '["counts"]["dropped"]')" = "$DROPPED_BEFORE" ] \
  && ok "no meter reading lost (failed and dropped unchanged)" \
  || fail "readings lost: failed $(sim '["counts"]["failed"]'), dropped $(sim '["counts"]["dropped"]')"
[ "$(sql "select count(*) from (select 1 from meter_readings group by meter_id, reading_ts having count(*) > 1) d")" = "0" ] \
  && ok "no reading stored twice (retries and redeliveries deduplicated)" || fail "duplicate readings"
curl -sk -D "$TMPC/h" -o "$TMPC/back.bin" "$BASE/files/$FILE_ID/download" -H "$DISP"
grep -qi '^x-integrity-verified: true' "$TMP/h" && [ "$(sha256sum "$TMP/back.bin" | cut -d' ' -f1)" = "$SRC_SHA" ] \
  && ok "video downloads byte-identical, X-Integrity-Verified: true" || fail "download differs"

echo; echo "SLOW-NETWORK DEMO PASSED"
