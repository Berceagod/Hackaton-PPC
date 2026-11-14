#!/usr/bin/env bash
# Slice E proof (PDF §4 "files > 1 GB processed without the service's resources growing
# with the file size" + "transfers resume"):
#   1. a synthetic drone video (default 1200 MB) is uploaded in 5 MB chunks through the gateway
#      exactly as the UI does it, each chunk with its own SHA-256;
#   2. one chunk is deliberately held back: the server reports it missing, the client
#      resends only that chunk (resume), and the file is assembled;
#   3. the backend's memory is sampled the whole time (container limit: 512 MB);
#   4. the file is downloaded again and compared byte for byte (SHA-256).
# Usage: scripts/demo/large-file-1gb.sh [size_mb]
set -euo pipefail
cd "$(dirname "$0")/../.."
source scripts/lib/env.sh

BASE="${BASE:-https://localhost/api/v1}"
SIZE_MB="${1:-1200}"
CHUNK_MB=5
HOLD_BACK=7                       # chunk sent last, after asking the server what is missing
CURL="curl -sk"
TMP="$(mktemp -d)"
SAMPLER_PID=""
cleanup() { [ -n "$SAMPLER_PID" ] && kill "$SAMPLER_PID" 2>/dev/null || true; rm -rf "$TMP"; }
trap cleanup EXIT
TMPC="$(cygpath -m "$TMP" 2>/dev/null || echo "$TMP")"   # Git Bash: Windows path for curl -F @

ok()   { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; exit 1; }
json() { python -c "import sys,json;d=json.load(open(sys.argv[1]));print($2)" "$1"; }

PW=$(env_value DISPECER_PASSWORD)
TOKEN=$($CURL -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"dispecer@ppc.ro\",\"password\":\"$PW\"}" \
  | python -c 'import sys,json;print(json.load(sys.stdin)["token"])')
unset PW
AUTH="Authorization: Bearer $TOKEN"

BACKEND=$(docker compose ps -q backend | head -1)
LIMIT=$(docker inspect -f '{{.HostConfig.Memory}}' "$BACKEND")
echo "== backend memory limit: $((LIMIT / 1024 / 1024)) MB"

echo "== 1. Generate a ${SIZE_MB} MB synthetic drone video"
head -c $((SIZE_MB * 1024 * 1024)) /dev/urandom > "$TMP/video.bin"
SRC_SHA=$(sha256sum "$TMP/video.bin" | cut -d' ' -f1)
SIZE=$(stat -c %s "$TMP/video.bin")
TOTAL=$(( (SIZE + CHUNK_MB * 1024 * 1024 - 1) / (CHUNK_MB * 1024 * 1024) ))
ok "$SIZE bytes, $TOTAL chunks, sha256 ${SRC_SHA:0:16}..."

# memory sampler: one docker stats reading per loop, in MiB
( while true; do
    docker stats --no-stream --format '{{.MemUsage}}' "$BACKEND" 2>/dev/null \
      | awk '{v=$1; if (v ~ /GiB/) {sub(/GiB/,"",v); v*=1024} else if (v ~ /MiB/) {sub(/MiB/,"",v)} else if (v ~ /KiB/) {sub(/KiB/,"",v); v/=1024}; printf "%d\n", v}'
  done ) > "$TMP/mem.txt" &
SAMPLER_PID=$!
for _ in $(seq 1 30); do [ -s "$TMP/mem.txt" ] && break; sleep 1; done   # docker stats is slow
BASELINE=$(head -1 "$TMP/mem.txt")
[ -n "$BASELINE" ] || fail "could not read the backend's memory with docker stats"

UPLOAD_ID=$(python -c 'import uuid;print(uuid.uuid4())')
send_chunk() { # index -> writes response to $TMP/resp.json, prints HTTP code
  local i=$1 attempt code
  dd if="$TMP/video.bin" of="$TMP/chunk.bin" bs=1M skip=$((i * CHUNK_MB)) count=$CHUNK_MB status=none
  local csha; csha=$(sha256sum "$TMP/chunk.bin" | cut -d' ' -f1)
  for attempt in 1 2 3 4 5; do
    code=$($CURL -o "$TMP/resp.json" -w '%{http_code}' -X POST "$BASE/upload/chunk" -H "$AUTH" \
      -F "upload_id=$UPLOAD_ID" -F "chunk_index=$i" -F "total_chunks=$TOTAL" \
      -F "filename=inspectie_drona_LEA20_${SIZE_MB}MB.mp4" -F "category=drone_videos" \
      -F 'metadata={"zone":"Muntenia","station":"LEA-20kV-Ploiesti"}' \
      -F "sha256=$SRC_SHA" -F "size_bytes=$SIZE" -F "chunk_sha256=$csha" \
      -F "file_chunk=@$TMPC/chunk.bin;filename=chunk") || code=000
    case "$code" in 200) echo "$code"; return 0 ;; 409|413|422|401|403|501) echo "$code"; return 0 ;; esac
    sleep $((attempt * 2))      # network error / 503: back off and resend the same chunk
  done
  echo "$code"
}

echo "== 2. Upload all chunks except #$HOLD_BACK (simulated dropped connection)"
START=$(date +%s)
for ((i = 0; i < TOTAL; i++)); do
  [ "$i" -eq "$HOLD_BACK" ] && continue
  code=$(send_chunk "$i"); [ "$code" = "200" ] || fail "chunk $i -> HTTP $code: $(cat "$TMP/resp.json")"
  (( i % 40 == 0 )) && printf '     chunk %d/%d\n' "$i" "$TOTAL"
done
ok "$((TOTAL - 1)) chunks staged"

echo "== 3. Resume: ask the server what is missing"
$CURL -o "$TMP/status.json" "$BASE/upload/$UPLOAD_ID" -H "$AUTH"
MISSING=$(json "$TMP/status.json" 'd["missing"]')
[ "$MISSING" = "[$HOLD_BACK]" ] && ok "server says missing: $MISSING (status $(json "$TMP/status.json" 'd["status"]'))" \
  || fail "unexpected status: $(cat "$TMP/status.json")"

echo "== 4. Send only the missing chunk -> server assembles the file"
code=$(send_chunk "$HOLD_BACK"); [ "$code" = "200" ] || fail "HTTP $code: $(cat "$TMP/resp.json")"
[ "$(json "$TMP/resp.json" 'd["status"]')" = "complete" ] || fail "not complete: $(cat "$TMP/resp.json")"
FILE_ID=$(json "$TMP/resp.json" 'd["file_id"]')
SRV_SHA=$(json "$TMP/resp.json" 'd["sha256"]')
ok "complete in $(( $(date +%s) - START )) s: file_id=$FILE_ID storage=$(json "$TMP/resp.json" 'd["storage"]')"
[ "$SRV_SHA" = "$SRC_SHA" ] && ok "server SHA-256 == local SHA-256" || fail "sha mismatch"

echo "== 5. Download it back and compare"
$CURL -D "$TMP/h" -o "$TMP/back.bin" "$BASE/files/$FILE_ID/download" -H "$AUTH"
grep -qi '^x-integrity-verified: true' "$TMP/h" && ok "X-Integrity-Verified: true" || fail "integrity header missing"
[ "$(sha256sum "$TMP/back.bin" | cut -d' ' -f1)" = "$SRC_SHA" ] && ok "downloaded file is byte-identical" \
  || fail "download differs"

kill "$SAMPLER_PID" 2>/dev/null || true; SAMPLER_PID=""
PEAK=$(sort -n "$TMP/mem.txt" | tail -1)
echo "== 6. Backend memory while moving ${SIZE_MB} MB up and down"
GROWTH=$((PEAK - BASELINE))
ok "baseline ${BASELINE} MiB, peak ${PEAK} MiB (+${GROWTH} MiB), limit $((LIMIT / 1024 / 1024)) MiB, samples $(wc -l < "$TMP/mem.txt")"
# Bounded = growth does not depend on the file size (5 MB chunks + 5 MB S3 parts + buffers).
MAX_GROWTH_MB=150
[ "$GROWTH" -lt "$MAX_GROWTH_MB" ] && ok "memory grew ${GROWTH} MiB for a ${SIZE_MB} MB file (bound: ${MAX_GROWTH_MB} MiB, independent of size)" \
  || fail "memory grew ${GROWTH} MiB: not bounded"
