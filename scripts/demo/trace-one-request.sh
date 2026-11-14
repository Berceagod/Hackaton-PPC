#!/usr/bin/env bash
# Slice H proof, O2 (PDF §4 Observabilitate): "one operation can be followed from the initial
# request all the way to the storage".
#
# Uploads one outage record with a trace id of our own, then reads back, from Loki (through
# Grafana), every line that carries that id: the gateway access line, the backend request, the
# storage call (which storage, which operation, how long) and the "file stored" confirmation.
# It also shows the same upload in the Prometheus metrics, labelled by category and storage.
#
# Usage: scripts/demo/trace-one-request.sh
# Overrides (to point at another stack): BASE, GRAFANA_URL, ENV_FILE (default .env).
# Do not run with `bash -x`: it would print the Grafana password.
set -euo pipefail
cd "$(dirname "$0")/../.."

BASE="${BASE:-https://localhost/api/v1}"
GRAFANA_URL="${GRAFANA_URL:-https://localhost/grafana}"
ENV_FILE="${ENV_FILE:-.env}"
CURL="curl -sk"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
TMPC="$(cygpath -m "$TMP" 2>/dev/null || echo "$TMP")"

ok()   { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; exit 1; }
env_value() { grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '\r'; }
[ -f "$ENV_FILE" ] || fail "no $ENV_FILE: run ./start.sh first"
GRAFANA_PW="$(env_value GRAFANA_ADMIN_PASSWORD)"
[ -n "$GRAFANA_PW" ] || fail "GRAFANA_ADMIN_PASSWORD missing in $ENV_FILE: run ./start.sh"

# Grafana's datasource proxy is the only way in to Prometheus and Loki (they publish no port).
graf() { $CURL -u "admin:$GRAFANA_PW" -G "$GRAFANA_URL/api/datasources/proxy/uid/$1$2" "${@:3}"; }
prom() { graf prometheus /api/v1/query --data-urlencode "query=$1" \
  | python -c 'import sys,json;r=json.load(sys.stdin)["data"]["result"];print(sum(float(x["value"][1]) for x in r) if r else 0)'; }

echo "== 1. Observability stack is up"
CODE=$($CURL -o /dev/null -w '%{http_code}' -u "admin:$GRAFANA_PW" "$GRAFANA_URL/api/health")
[ "$CODE" = "200" ] && ok "Grafana answers through the gateway (/grafana/)" || fail "Grafana: HTTP $CODE (is the stack up? ./start.sh)"
graf loki /loki/api/v1/labels | grep -q '"status":"success"' && ok "Loki is reachable" || fail "Loki not reachable"

echo "== 2. Upload one outage record with a known trace id"
TOKEN=$($CURL -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"dispecer@ppc.ro\",\"password\":\"$(env_value DISPECER_PASSWORD)\"}" \
  | python -c 'import sys,json;print(json.load(sys.stdin)["token"])')
AUTH="Authorization: Bearer $TOKEN"
POLICY_STORAGE=$($CURL "$BASE/admin/policies" -H "$AUTH" | python -c 'import sys,json;print(json.load(sys.stdin)["outage_records"]["storage"])')
TRACE=$(python -c 'import uuid;print(uuid.uuid4())')
head -c 1048576 /dev/urandom > "$TMP/avarie.bin"
REQ_BEFORE=$(prom "sum(filegrid_requests_total{category=\"outage_records\",storage=\"$POLICY_STORAGE\",operation=\"ingest\",outcome=\"ok\"})")
PUT_BEFORE=$(prom "sum(filegrid_storage_operation_duration_seconds_count{storage=\"$POLICY_STORAGE\",op=\"put\",outcome=\"ok\"})")
$CURL -D "$TMP/h" -o "$TMP/resp.json" -X POST "$BASE/ingest" -H "$AUTH" -H "X-Trace-Id: $TRACE" \
  -F category=outage_records -F 'metadata={"zone":"Dobrogea","station":"ST-Constanta-Est"}' \
  -F "file=@$TMPC/avarie.bin;filename=trasabilitate_demo.bin"
grep -q '^HTTP/[0-9.]* 202' "$TMP/h" && ok "HTTP 202 (stored on $POLICY_STORAGE)" || fail "ingest: $(grep '^HTTP' "$TMP/h" | tail -1)"
HDR=$(grep -i '^x-trace-id:' "$TMP/h" | tr -d '\r' | awk '{print $2}')
BODY=$(python -c 'import sys,json;print(json.load(open(sys.argv[1]))["trace_id"])' "$TMP/resp.json")
FID=$(python -c 'import sys,json;print(json.load(open(sys.argv[1]))["file_id"])' "$TMP/resp.json")
[ "$HDR" = "$TRACE" ] && ok "response header X-Trace-Id = the id we sent" || fail "header $HDR != $TRACE"
[ "$BODY" = "$TRACE" ] && ok "response body trace_id = the id we sent" || fail "body $BODY != $TRACE"
echo "       trace id: $TRACE"

echo "== 3. Download it under its own trace id (a second operation)"
TRACE2=$(python -c 'import uuid;print(uuid.uuid4())')
$CURL -o "$TMP/out.bin" "$BASE/files/$FID/download" -H "$AUTH" -H "X-Trace-Id: $TRACE2"
cmp -s "$TMP/avarie.bin" "$TMP/out.bin" && ok "download byte-identical" || fail "download differs"

echo "== 4. Read the whole story of the upload back from Loki, by trace id"
query_trace() { # $1 trace id -> time-ordered lines "ts|service|summary", one per log line
  graf loki /loki/api/v1/query_range \
    --data-urlencode "query={service=~\"backend|nginx\"} |= \"$1\"" --data-urlencode "limit=500" \
    --data-urlencode "direction=forward" \
    --data-urlencode "start=$(python -c 'import time;print(int((time.time()-900)*1e9))')" \
    --data-urlencode "end=$(python -c 'import time;print(int((time.time()+60)*1e9))')" \
  | python -c '
import sys, json, datetime
rows = []
for s in json.load(sys.stdin)["data"]["result"]:
    svc = s["stream"].get("service", "?")
    for ts, line in s["values"]:
        try: d = json.loads(line)
        except ValueError: d = {"msg": line.strip()}
        if d.get("msg") == "storage op":
            what = "storage call: %s %s -> %s in %s ms (category %s)" % (d.get("storage"), d.get("op"), d.get("outcome"), d.get("duration_ms"), d.get("category"))
        elif d.get("logger") == "filegrid.http":
            what = "backend request: %s %s -> %s in %s ms" % (d.get("method"), d.get("path"), d.get("status"), d.get("duration_ms"))
        elif svc == "nginx":
            what = "gateway: %s %s -> %s in %s s" % (d.get("method"), d.get("path"), d.get("status"), d.get("duration_s"))
        else:
            what = d.get("msg", "")
        rows.append((int(ts), svc, what))
for ts, svc, what in sorted(rows):
    t = datetime.datetime.fromtimestamp(ts / 1e9).strftime("%H:%M:%S.%f")[:-3]
    print("%s|%s|%s" % (t, svc, what))
'
}
STORY=""
for _ in $(seq 1 30); do   # Alloy ships logs within a few seconds
  STORY="$(query_trace "$TRACE")"
  echo "$STORY" | grep -q 'gateway:' && echo "$STORY" | grep -q 'storage call:' \
    && echo "$STORY" | grep -q 'backend request:' && echo "$STORY" | grep -q 'file stored' && break
  sleep 3
done
echo
echo "$STORY" | awk -F'|' '{ printf "   %s  %-8s %s\n", $1, $2, $3 }'
echo
echo "$STORY" | grep -q 'gateway: POST /api/v1/ingest -> 202'      && ok "gateway access line (nginx) carries the trace id" || fail "no gateway line for the trace"
echo "$STORY" | grep -q 'backend request: POST /api/v1/ingest -> 202' && ok "backend request line carries the trace id"        || fail "no backend request line"
echo "$STORY" | grep -q "storage call: $POLICY_STORAGE put -> ok"    && ok "the call into the $POLICY_STORAGE storage carries the trace id" || fail "no storage-call line for the trace"
echo "$STORY" | grep -q 'file stored'                                && ok "'file stored' confirmation carries the trace id"   || fail "no 'file stored' line"
N=$(echo "$STORY" | wc -l | tr -d ' ')
K=$(echo "$STORY" | awk -F'|' '{print $2}' | sort -u | wc -l | tr -d ' ')
ok "$N log lines from $K services (gateway and backend; the storage hop is logged by the backend), all found with the one trace id"

echo "== 5. The same upload in the metrics (labelled by category and storage)"
sleep 6   # one scrape interval
REQ_AFTER=$(prom "sum(filegrid_requests_total{category=\"outage_records\",storage=\"$POLICY_STORAGE\",operation=\"ingest\",outcome=\"ok\"})")
PUT_AFTER=$(prom "sum(filegrid_storage_operation_duration_seconds_count{storage=\"$POLICY_STORAGE\",op=\"put\",outcome=\"ok\"})")
python -c "import sys; sys.exit(0 if float('$REQ_AFTER') >= float('$REQ_BEFORE') + 1 else 1)" \
  && ok "filegrid_requests_total{outage_records,$POLICY_STORAGE,ingest,ok}: $REQ_BEFORE -> $REQ_AFTER" \
  || fail "request counter did not move ($REQ_BEFORE -> $REQ_AFTER)"
python -c "import sys; sys.exit(0 if float('$PUT_AFTER') >= float('$PUT_BEFORE') + 1 else 1)" \
  && ok "filegrid_storage_operation_duration_seconds_count{$POLICY_STORAGE,put,ok}: $PUT_BEFORE -> $PUT_AFTER" \
  || fail "storage-call histogram did not move ($PUT_BEFORE -> $PUT_AFTER)"

echo
echo "Open it in Grafana:  ${GRAFANA_URL}/d/filegrid-trace?var-trace_id=$TRACE   (user admin, password in $ENV_FILE)"
echo "TRACE-ONE-REQUEST DEMO PASSED"
