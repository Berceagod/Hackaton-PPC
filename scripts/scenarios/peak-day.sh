#!/usr/bin/env bash
# T2, meter peak: 10x the normal-day meter count, while outages, documents and a
# video are ingested at the quiet volume. Their p95 is the "during the peak" number.
# Writes scenario=peak (the "Vârf furtună (10x)" bar). storm.sh writes the same bar
# with the spike on outages instead; whichever ran last is what the chart shows.
# Usage: scripts/scenarios/peak-day.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
source scripts/lib/env.sh
source scripts/benchmark/lib.sh

BASE="${BASE:-https://localhost/api/v1}"
CURL="curl -sk"
METERS="${SCENARIO_METERS:-12}"
OUTAGES="${SCENARIO_OUTAGES:-3}"
PEAK=$((METERS * 10))
OUTAGE_BYTES=$(( ${SCENARIO_OUTAGE_KB:-64} * 1024 ))
DRONE_BYTES=$(( ${SCENARIO_DRONE_KB:-256} * 1024 ))
TMP="$(mktemp -d)"
TMPC="$(cygpath -m "$TMP" 2>/dev/null || echo "$TMP")"
FLOOD=""
trap 'if [ -n "$FLOOD" ]; then kill "$FLOOD" 2>/dev/null || true; wait "$FLOOD" 2>/dev/null || true; fi; rm -rf "$TMP"' EXIT

AUTH="Authorization: Bearer $(fg_login admin@ppc.ro "$(env_value ADMIN_PASSWORD)")"
$CURL -o "$TMP/policies.json" "$BASE/admin/policies" -H "$AUTH"
METER_ST=$(fg_storage meter_readings)
OUTAGE_ST=$(fg_storage outage_records)
DOC_ST=$(fg_storage connection_docs)
DRONE_ST=$(fg_storage drone_videos)

echo "== Peak: ${PEAK} readings, other categories at the quiet volume"
(
  for i in $(seq 1 "$PEAK"); do
    ( fg_meter_job "$METER_ST" "$i" "m_${i}" || true ) &
    if (( i % 8 == 0 )); then wait; fi
  done
  wait
) &
FLOOD=$!
for i in $(seq 1 "$OUTAGES"); do
  fg_file_job outage_records "$OUTAGE_ST" "$i" "$OUTAGE_BYTES" "o_${i}"
done
fg_file_job connection_docs "$DOC_ST" 1 0 "d_1"
fg_file_job connection_docs "$DOC_ST" 2 0 "d_2"
fg_file_job drone_videos "$DRONE_ST" 1 "$DRONE_BYTES" "v_1"
wait "$FLOOD"
FLOOD=""

fg_post scenario peak
echo
fg_summary peak
echo
echo "PEAK-DAY PASSED"
