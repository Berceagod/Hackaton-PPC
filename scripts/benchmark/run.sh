#!/usr/bin/env bash
# T1: same small workload on postgres, s3 and ftp, then GET /analytics/summary.
# The verdict names the best storage from these numbers. Policy is restored afterwards
# (a benchmark must not leave the live mapping on the last storage it tried).
# Files stay stored: that is the evidence. Sizes are demo-sized (BENCHMARK_*).
# Usage: scripts/benchmark/run.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
source scripts/lib/env.sh
source scripts/benchmark/lib.sh

BASE="${BASE:-https://localhost/api/v1}"
CURL="curl -sk"
SAMPLES="${BENCHMARK_SAMPLES:-5}"
OUTAGE_BYTES=$(( ${BENCHMARK_OUTAGE_KB:-64} * 1024 ))
DRONE_BYTES=$(( ${BENCHMARK_DRONE_KB:-1024} * 1024 ))
TMP="$(mktemp -d)"
TMPC="$(cygpath -m "$TMP" 2>/dev/null || echo "$TMP")"
SAVED=0

restore_policies() {
  if [ "$SAVED" = 1 ]; then
    $CURL -o /dev/null -X PUT "$BASE/admin/policies" -H "$AUTH" -H 'Content-Type: application/json' \
      --data-binary @"${TMPC}/policies.json" || true
    SAVED=0
    ok "policies restored"
  fi
}
cleanup() { restore_policies; rm -rf "$TMP"; }
trap cleanup EXIT

AUTH="Authorization: Bearer $(fg_login admin@ppc.ro "$(env_value ADMIN_PASSWORD)")"
[ -n "$AUTH" ] && ok "admin login"
$CURL -o "$TMP/policies.json" "$BASE/admin/policies" -H "$AUTH"
grep -q meter_readings "$TMP/policies.json" || fail "could not read policies"
SAVED=1

for STORAGE in postgres s3 ftp; do
  echo "== $STORAGE"
  python -c 'import json,sys
doc = json.load(open(sys.argv[1], encoding="utf-8"))
for policy in doc.values():
    policy["storage"] = sys.argv[2]
json.dump(doc, open(sys.argv[3], "w", encoding="utf-8"))
' "$TMP/policies.json" "$STORAGE" "$TMP/next.json"
  CODE=$($CURL -o "$TMP/pol.json" -w '%{http_code}' -X PUT "$BASE/admin/policies" \
    -H "$AUTH" -H 'Content-Type: application/json' --data-binary @"${TMPC}/next.json")
  [ "$CODE" = "200" ] && ok "policy -> $STORAGE" || fail "policy update -> $CODE"
  for i in $(seq 1 "$SAMPLES"); do
    fg_meter_job "$STORAGE" "$i" "m_${STORAGE}_${i}"
    fg_file_job outage_records "$STORAGE" "$i" "$OUTAGE_BYTES" "o_${STORAGE}_${i}"
    fg_file_job connection_docs "$STORAGE" "$i" 0 "d_${STORAGE}_${i}"
    fg_file_job drone_videos "$STORAGE" "$i" "$DRONE_BYTES" "v_${STORAGE}_${i}"
  done
done

restore_policies
fg_collect
python -c '
import sys
rows = {}
for line in open(sys.argv[1], encoding="utf-8"):
    category, storage, code, *_rest = line.rstrip("\n").split("\t")
    seen = rows.setdefault((category, storage), [0, 0])
    seen[0] += 1
    seen[1] += code == "202"
bad = False
for (category, storage), (n, ok_n) in sorted(rows.items()):
    print(f"  {category} on {storage}: {ok_n}/{n} stored")
    if ok_n == 0:
        bad = True
sys.exit(1 if bad else 0)
' "$TMP/samples.tsv" || fail "a category stored nothing on one of the storages"

fg_post benchmark
echo
fg_summary benchmark
echo
echo "BENCHMARK PASSED"
