#!/usr/bin/env bash
# Slice G proof (PDF §4 Scalabilitate): "a 10x peak in one category does not degrade the
# other categories". Latency is measured end to end, as a client sees it:
#   phase 1  normal day          -> baseline latency of outage uploads and of meter readings
#   phase 2  meter storm (10x)    -> outage-upload latency must stay close to its baseline
#   phase 3  outage storm (10x)   -> meter-reading latency must stay close to its baseline
# Pass: p95 under the spike <= 2 x baseline p95 + 100 ms (slack for client/process noise).
set -euo pipefail
cd "$(dirname "$0")/../.."
source scripts/lib/env.sh

BASE="${BASE:-https://localhost/api/v1}"
CURL="curl -sk"
TMP="$(mktemp -d)"
TMPC="$(cygpath -m "$TMP" 2>/dev/null || echo "$TMP")"
N=${N:-30}
ok()   { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; exit 1; }
cleanup() {
  rm -f "$TMP/run"; wait 2>/dev/null || true
  bash scripts/sim.sh mode normal >/dev/null 2>&1 || true
  rm -rf "$TMP"
}
trap cleanup EXIT

DISP="Authorization: Bearer $($CURL -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"dispecer@ppc.ro\",\"password\":\"$(env_value DISPECER_PASSWORD)\"}" \
  | python -c 'import sys,json;print(json.load(sys.stdin)["token"])')"
SIMAUTH="Authorization: Bearer $(env_value SIMULATOR_TOKEN)"
head -c 1048576 /dev/urandom > "$TMP/avarie.bin"   # 1 MB outage record

outage_once() { # -> seconds
  $CURL -o "$TMPC/o_$1.json" -w '%{time_total}\n' -X POST "$BASE/ingest" -H "$DISP" \
    -F category=outage_records -F 'metadata={"zone":"Dobrogea","station":"ST-spike"}' \
    -F "file=@$TMPC/avarie.bin;filename=spike_avarie.bin"
}
meter_once() { # -> seconds
  local ts; ts=$(python -c 'import datetime;print(datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="microseconds").replace("+00:00","Z"))')
  printf '{"meter_id":"RO-SPK-%s","timestamp":"%s","zone":"Banat","instant":{"voltage_v":230.0}}' "$$" "$ts" > "$TMP/r.json"
  $CURL -o "$TMPC/m.json" -w '%{time_total}\n' -X POST "$BASE/ingest" -H "$SIMAUTH" \
    -H "Idempotency-Key: RO-SPK-$$:$ts" -F category=meter_readings -F "file=@$TMPC/r.json;filename=r.json"
}
measure() { # fn count -> "p50 p95" in ms
  for i in $(seq 1 "$2"); do "$1" "$i"; done | python -c '
import sys
v = sorted(float(x) * 1000 for x in sys.stdin if x.strip())
p = lambda q: v[min(len(v) - 1, int(q * len(v)))]
print(f"{p(0.50):.0f} {p(0.95):.0f}")'
}
check() { # label base_p95 spike_p95
  local limit=$(( $2 * 2 + 100 ))
  [ "$3" -le "$limit" ] && ok "$1: p95 ${3} ms vs baseline ${2} ms (limit ${limit} ms)" \
    || fail "$1: p95 ${3} ms vs baseline ${2} ms exceeds ${limit} ms"
}

echo "== 1. Normal day (simulator $(bash scripts/sim.sh status | python -c 'import sys,json;print(json.load(sys.stdin)["target_rate"])') readings/s)"
bash scripts/sim.sh mode normal >/dev/null; sleep 3
read -r O50 O95 <<<"$(measure outage_once "$N")"; ok "outage upload 1 MB : p50 ${O50} ms, p95 ${O95} ms"
read -r M50 M95 <<<"$(measure meter_once "$N")";  ok "meter reading      : p50 ${M50} ms, p95 ${M95} ms"

echo "== 2. Meter storm: 10x readings"
bash scripts/sim.sh mode storm >/dev/null; sleep 5
RATE=$(bash scripts/sim.sh status | python -c 'import sys,json;print(json.load(sys.stdin)["target_rate"])')
read -r S50 S95 <<<"$(measure outage_once "$N")"; ok "outage upload during ${RATE} readings/s: p50 ${S50} ms, p95 ${S95} ms"
check "outage uploads during the meter storm" "$O95" "$S95"
bash scripts/sim.sh mode normal >/dev/null

echo "== 3. Outage storm: 8 parallel uploaders of outage records"
touch "$TMP/run"
for w in 1 2 3 4 5 6 7 8; do
  ( while [ -f "$TMP/run" ]; do outage_once "w$w" >/dev/null || true; done ) &
done
sleep 5
read -r T50 T95 <<<"$(measure meter_once "$N")"; ok "meter reading during the outage storm: p50 ${T50} ms, p95 ${T95} ms"
rm -f "$TMP/run"; wait 2>/dev/null || true
check "meter readings during the outage storm" "$M95" "$T95"
SIMFAILED=$(bash scripts/sim.sh status | python -c 'import sys,json;print(json.load(sys.stdin)["counts"]["failed"])')
[ "$SIMFAILED" = "0" ] && ok "simulator lost no reading during either storm" || fail "simulator failed=$SIMFAILED"

echo; echo "SPIKE-10X DEMO PASSED"
