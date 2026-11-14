# Shared by scripts/benchmark and scripts/scenarios. Source it from the repo root.
# Does not print secrets. Each sample is its own file: parallel appends drop lines on Git Bash.

ok()   { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; exit 1; }

fg_login() {
  $CURL -sS -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
    -d "{\"email\":\"$1\",\"password\":\"$2\"}" \
    | python -c 'import sys,json; print(json.load(sys.stdin)["token"])'
}

# fg_ingest CATEGORY STORAGE RELATIVE_FILE FILENAME METADATA TAG
# RELATIVE_FILE lives in $TMP. TAG names the timing line ($TMP/line_TAG).
fg_ingest() {
  local category="$1" storage="$2" file="$3" filename="$4" meta="$5" tag="$6"
  local out code secs bytes
  out=$($CURL -o "$TMP/resp_${tag}.json" -w '%{http_code} %{time_total}' \
    -X POST "$BASE/ingest" -H "$AUTH" \
    -F "category=${category}" -F "metadata=${meta}" \
    -F "file=@${TMPC}/${file};filename=${filename}" || echo "000 0")
  code="${out%% *}"
  secs="${out##* }"
  bytes=$(wc -c < "$TMP/$file" | tr -d '[:space:]')
  printf '%s\t%s\t%s\t%s\t%s\n' "$category" "$storage" "$code" "$secs" "$bytes" > "$TMP/line_${tag}"
}

fg_collect() {
  cat "$TMP"/line_* > "$TMP/samples.tsv"
}

fg_post() {
  local kind="$1" scenario="${2:-}"
  fg_collect
  if [ -n "$scenario" ]; then
    python scripts/benchmark/post_measurements.py "$kind" "$scenario" < "$TMP/samples.tsv" > "$TMP/body.json"
  else
    python scripts/benchmark/post_measurements.py "$kind" < "$TMP/samples.tsv" > "$TMP/body.json"
  fi
  local code
  code=$($CURL -o "$TMP/post.out" -w '%{http_code}' -X POST "$BASE/analytics/measurements" \
    -H "$AUTH" -H 'Content-Type: application/json' \
    --data-binary @"${TMPC}/body.json")
  [ "$code" = "204" ] && ok "measurements stored ($kind ${scenario:-run})" || fail "POST measurements -> $code"
}

fg_summary() {
  $CURL -o "$TMP/summary.json" "$BASE/analytics/summary" -H "$AUTH"
  python scripts/benchmark/show_summary.py "$TMP/summary.json" "${1:-}"
}

fg_storage() {
  python -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))[sys.argv[2]]["storage"])' \
    "$TMP/policies.json" "$1"
}

# write_meter METER_ID ZONE RELATIVE_FILE
fg_write_meter() {
  python -c 'import datetime, json, sys
ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
json.dump({
    "meter_id": sys.argv[1], "timestamp": ts, "zone": sys.argv[2],
    "instant": {"voltage_v": 230.0, "active_power_kw": 0.8},
    "events": [], "sim_label": None,
}, open(sys.argv[4], "w", encoding="utf-8"))' "$1" "$2" "$3" "$TMP/$3"
}

# fg_write_blob RELATIVE_FILE BYTES SUFFIX  (suffix keeps dedup from collapsing samples)
fg_write_blob() {
  head -c "$2" /dev/urandom > "$TMP/$1"
  printf '%s' "$3" >> "$TMP/$1"
}

fg_write_doc() {
  python -c 'import json,sys; json.dump({
    "titular": "Persoana Sintetica", "adresa": "Strada Demo " + sys.argv[2],
    "cnp": "0000000000000", "note": sys.argv[2],
}, open(sys.argv[1], "w", encoding="utf-8"))' "$TMP/$1" "$2"
}

FG_ZONES=(Muntenia Banat Dobrogea)
fg_zone() { echo "${FG_ZONES[$(($1 % 3))]}"; }

# fg_meter_job STORAGE INDEX TAG   (safe to run in the background: never aborts the caller)
fg_meter_job() {
  local storage="$1" i="$2" tag="$3" zone
  zone=$(fg_zone "$i")
  fg_write_meter "RO-ANL-${tag}" "$zone" "reading_${tag}.json"
  fg_ingest meter_readings "$storage" "reading_${tag}.json" "r.json" "{}" "$tag"
}

# fg_file_job CATEGORY STORAGE INDEX BYTES TAG
fg_file_job() {
  local category="$1" storage="$2" i="$3" bytes="$4" tag="$5"
  local zone meta name
  zone=$(fg_zone "$i")
  meta="{\"zone\":\"${zone}\",\"station\":\"ST-analiza\"}"
  if [ "$category" = "connection_docs" ]; then
    fg_write_doc "doc_${tag}.json" "$tag"
    fg_ingest "$category" "$storage" "doc_${tag}.json" "racordare.json" "$meta" "$tag"
    return
  fi
  name="avarie.bin"
  [ "$category" = "drone_videos" ] && name="inspectie.bin"
  fg_write_blob "blob_${tag}.bin" "$bytes" "$tag"
  fg_ingest "$category" "$storage" "blob_${tag}.bin" "$name" "$meta" "$tag"
}
