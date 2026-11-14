#!/usr/bin/env bash
# T2, quiet day: a short mix at the storages the current policy already uses.
# Fills the "Zi normală" bar of GET /analytics/summary. Run storm.sh afterwards
# for the other bar.
# Usage: scripts/scenarios/normal-day.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
source scripts/lib/env.sh
source scripts/benchmark/lib.sh

BASE="${BASE:-https://localhost/api/v1}"
CURL="curl -sk"
METERS="${SCENARIO_METERS:-12}"
OUTAGES="${SCENARIO_OUTAGES:-3}"
OUTAGE_BYTES=$(( ${SCENARIO_OUTAGE_KB:-64} * 1024 ))
DRONE_BYTES=$(( ${SCENARIO_DRONE_KB:-256} * 1024 ))
TMP="$(mktemp -d)"
TMPC="$(cygpath -m "$TMP" 2>/dev/null || echo "$TMP")"
trap 'rm -rf "$TMP"' EXIT

AUTH="Authorization: Bearer $(fg_login admin@ppc.ro "$(env_value ADMIN_PASSWORD)")"
$CURL -o "$TMP/policies.json" "$BASE/admin/policies" -H "$AUTH"
METER_ST=$(fg_storage meter_readings)
OUTAGE_ST=$(fg_storage outage_records)
DOC_ST=$(fg_storage connection_docs)
DRONE_ST=$(fg_storage drone_videos)
ok "policy: meters=$METER_ST outages=$OUTAGE_ST docs=$DOC_ST drone=$DRONE_ST"

echo "== Normal day: ${METERS} readings, ${OUTAGES} outages, 2 documents, 1 video"
for i in $(seq 1 "$METERS"); do
  fg_meter_job "$METER_ST" "$i" "m_${i}"
done
for i in $(seq 1 "$OUTAGES"); do
  fg_file_job outage_records "$OUTAGE_ST" "$i" "$OUTAGE_BYTES" "o_${i}"
done
fg_file_job connection_docs "$DOC_ST" 1 0 "d_1"
fg_file_job connection_docs "$DOC_ST" 2 0 "d_2"
fg_file_job drone_videos "$DRONE_ST" 1 "$DRONE_BYTES" "v_1"

fg_post scenario normal
echo
fg_summary normal
echo
echo "NORMAL-DAY PASSED"
