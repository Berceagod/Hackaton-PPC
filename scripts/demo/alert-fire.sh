#!/usr/bin/env bash
# Slice H proof, O3 (PDF §4 Observabilitate): "the team is notified automatically when a
# business requirement of table 2.1 is at risk".
#
#   1. The alert rules are unit-tested offline (promtool): each fires on its situation and
#      stays quiet otherwise.
#   2. Then, for real, on the running system, three guarantees are put at risk:
#        - integrity ........ a stored file is damaged inside the storage  -> FileIntegrityFailure
#        - personal data ..... a connection document is stored unencrypted -> PersonalDataStoredUnencrypted
#        - video upload ...... a chunked upload receives nothing for a while -> VideoUploadStalled
#      Each alert must fire in Prometheus AND be delivered to the team webhook by Alertmanager.
#   3. The demo cleans up after itself; the two alerts that depend on state resolve and the
#      "resolved" notification is checked too.
#
# Usage: scripts/demo/alert-fire.sh [quick]     quick = integrity only (~30 s); default ~5 min
# Overrides (another stack): BASE, GRAFANA_URL, ENV_FILE. Do not run with `bash -x`.
set -euo pipefail
cd "$(dirname "$0")/../.."

MODE="${1:-all}"
BASE="${BASE:-https://localhost/api/v1}"
GRAFANA_URL="${GRAFANA_URL:-https://localhost/grafana}"
ENV_FILE="${ENV_FILE:-.env}"
CURL="curl -sk"
TMP="$(mktemp -d)"
TMPC="$(cygpath -m "$TMP" 2>/dev/null || echo "$TMP")"

ok()   { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; exit 1; }
note() { printf '       %s\n' "$*"; }
env_value() { grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '\r'; }
[ -f "$ENV_FILE" ] || fail "no $ENV_FILE: run ./start.sh first"
GRAFANA_PW="$(env_value GRAFANA_ADMIN_PASSWORD)"
[ -n "$GRAFANA_PW" ] || fail "GRAFANA_ADMIN_PASSWORD missing in $ENV_FILE: run ./start.sh"

graf() { $CURL -u "admin:$GRAFANA_PW" -G "$GRAFANA_URL/api/datasources/proxy/uid/$1$2" "${@:3}"; }
sql()  { MSYS_NO_PATHCONV=1 docker compose exec -T postgres-meta psql -U filegrid -d filegrid -tAc "$1" | tr -d '\r '; }
login() { $CURL -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$1\",\"password\":\"$2\"}" | python -c 'import sys,json;print(json.load(sys.stdin)["token"])'; }
firing() { graf prometheus /api/v1/alerts | python -c \
  'import sys,json;print(" ".join(sorted({a["labels"]["alertname"] for a in json.load(sys.stdin)["data"]["alerts"] if a["state"]=="firing"})))'; }
is_firing() { firing | tr ' ' '\n' | grep -qx "$1"; }
webhook() { MSYS_NO_PATHCONV=1 docker compose exec -T alert-webhook python -c \
  "import urllib.request,sys;print(urllib.request.urlopen('http://127.0.0.1:9099/alerts?name='+sys.argv[1],timeout=5).read().decode())" "$1"; }
not_firing() { ! is_firing "$1"; }
mark() { webhook "$1" | python -c 'import sys,json;print(len(json.load(sys.stdin)))' > "$TMP/n0.$1"; }
delivered() { # a NEW notification (after mark) with that status: never a leftover of an earlier run
  local n0; n0=$(cat "$TMP/n0.$1" 2>/dev/null || echo 0)
  webhook "$1" | python -c "import sys,json; r=json.load(sys.stdin)[${n0}:]; sys.exit(0 if any(a['status']=='$2' for a in r) else 1)"
}
wait_until() { # seconds, then a command; polls every 4 s
  local deadline=$(( $(date +%s) + $1 )); shift
  until "$@"; do [ "$(date +%s)" -ge "$deadline" ] && return 1; sleep 4; done
}

ADMIN="Authorization: Bearer $(login admin@ppc.ro "$(env_value ADMIN_PASSWORD)")"
DISP="Authorization: Bearer $(login dispecer@ppc.ro "$(env_value DISPECER_PASSWORD)")"
ORIG_DOCS_POLICY="$($CURL "$BASE/admin/policies" -H "$ADMIN" | python -c 'import sys,json;print(json.dumps({"connection_docs": json.load(sys.stdin)["connection_docs"]}))')"
DOC_ID=""; UPLOAD_ID=""; FID=""

cleanup() {
  # put everything the demo touched back (best effort: this also runs when a step fails)
  $CURL -o /dev/null -X PUT "$BASE/admin/policies" -H "$ADMIN" -d "$ORIG_DOCS_POLICY" || true
  if [ -n "$DOC_ID" ]; then
    MSYS_NO_PATHCONV=1 docker compose exec -T ftp sh -c "rm -f /home/filegrid/connection_docs/$DOC_ID" >/dev/null 2>&1 || true
    # the file's audit rows stay: an audit trail is never edited, and deleting them would break
    # the tamper-evident chain (GET /audit/verify)
    sql "delete from files_metadata where id='$DOC_ID'" >/dev/null 2>&1 || true
  fi
  if [ -n "$UPLOAD_ID" ]; then
    sql "delete from upload_sessions where id='$UPLOAD_ID'" >/dev/null 2>&1 || true
    MSYS_NO_PATHCONV=1 docker compose exec -T -e UID_="$UPLOAD_ID" backend python -c '
import asyncio, os, aioboto3
async def main():
    async with aioboto3.Session().client("s3", endpoint_url=os.environ["S3_ENDPOINT"],
            aws_access_key_id=os.environ["S3_ACCESS_KEY"], aws_secret_access_key=os.environ["S3_SECRET_KEY"],
            region_name="us-east-1") as s3:
        r = await s3.list_objects_v2(Bucket=os.environ["S3_BUCKET"], Prefix="_staging/" + os.environ["UID_"] + "/")
        for o in r.get("Contents", []):
            await s3.delete_object(Bucket=os.environ["S3_BUCKET"], Key=o["Key"])
asyncio.run(main())' >/dev/null 2>&1 || true
  fi
  rm -rf "$TMP"
}
trap cleanup EXIT

echo "== 1. The alert rules, unit-tested offline (promtool)"
if MSYS_NO_PATHCONV=1 docker run --rm -v "$(cygpath -m "$PWD" 2>/dev/null || echo "$PWD")/infrastructure/prometheus:/t:ro" \
     --entrypoint promtool prom/prometheus:v3.5.0 test rules /t/alerts_test.yml > "$TMP/promtool.txt" 2>&1; then
  ok "promtool test rules: every alert fires on its situation and stays quiet otherwise"
else
  cat "$TMP/promtool.txt"; fail "alert rule unit tests failed"
fi

echo "== 2. The alerting pipeline is up"
RULES=$(graf prometheus /api/v1/rules | python -c 'import sys,json;print(sum(len(g["rules"]) for g in json.load(sys.stdin)["data"]["groups"]))')
[ "$RULES" -ge 10 ] && ok "Prometheus has loaded $RULES alert rules" || fail "Prometheus has only $RULES rules"
graf alertmanager /api/v2/status | grep -q '"cluster"' && ok "Alertmanager is reachable" || fail "Alertmanager not reachable"
MSYS_NO_PATHCONV=1 docker compose exec -T alert-webhook python -c \
  "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:9099/healthz',timeout=5).read().decode())" | grep -q ok \
  && ok "the team webhook receiver is up" || fail "webhook receiver not reachable"
BASELINE="$(firing)"
note "alerts already firing before the demo: ${BASELINE:-none}"
WANT="FileIntegrityFailure"
[ "$MODE" != "quick" ] && WANT="$WANT PersonalDataStoredUnencrypted VideoUploadStalled"
for a in $WANT; do
  if is_firing "$a"; then   # e.g. the demo was run a minute ago: FileIntegrityFailure looks back 5 minutes
    note "$a is still firing from an earlier run; waiting for it to resolve (up to 7 min) so the demo starts clean"
    wait_until 420 not_firing "$a" || fail "$a did not resolve"
  fi
  mark "$a"   # only notifications received after this point count
done

echo "== 3. Put the integrity guarantee at risk: damage a stored file inside the storage"
OUT_STORAGE=$($CURL "$BASE/admin/policies" -H "$DISP" | python -c 'import sys,json;print(json.load(sys.stdin)["outage_records"]["storage"])')
[ "$OUT_STORAGE" = "s3" ] || fail "this step damages an S3 object; outage_records points to '$OUT_STORAGE' (set it back to s3)"
head -c 262144 /dev/urandom > "$TMP/o.bin"
FID=$($CURL -X POST "$BASE/ingest" -H "$DISP" -F category=outage_records -F "file=@$TMPC/o.bin;filename=alert_demo_integrity.bin" \
  | python -c 'import sys,json;print(json.load(sys.stdin)["file_id"])')
MSYS_NO_PATHCONV=1 docker compose exec -T -e FID="$FID" backend python -c '
import asyncio, os, aioboto3
async def main():
    key = "outage_records/" + os.environ["FID"]
    async with aioboto3.Session().client("s3", endpoint_url=os.environ["S3_ENDPOINT"],
            aws_access_key_id=os.environ["S3_ACCESS_KEY"], aws_secret_access_key=os.environ["S3_SECRET_KEY"],
            region_name="us-east-1") as s3:
        body = bytearray(await (await s3.get_object(Bucket=os.environ["S3_BUCKET"], Key=key))["Body"].read())
        body[10] ^= 0xFF
        await s3.put_object(Bucket=os.environ["S3_BUCKET"], Key=key, Body=bytes(body))
asyncio.run(main())'
CODE=$($CURL -o /dev/null -w '%{http_code}' "$BASE/files/$FID/download" -H "$DISP")
[ "$CODE" = "409" ] && ok "one byte changed in S3; the download is refused (409), nothing corrupt is served" || fail "expected 409, got $CODE"

if [ "$MODE" != "quick" ]; then
  echo "== 4. Put personal-data protection at risk: store a connection document UNENCRYPTED"
  # the demo switches the policy off the way a careless administrator could
  $CURL -o /dev/null -X PUT "$BASE/admin/policies" -H "$ADMIN" \
    -d '{"connection_docs":{"storage":"ftp","encryption":false,"deduplicate":false,"priority":"normal"}}'
  head -c 65536 /dev/urandom > "$TMP/d.bin"
  DOC_ID=$($CURL -X POST "$BASE/ingest" -H "$DISP" -F category=connection_docs -F "file=@$TMPC/d.bin;filename=alert_demo_doc.bin" \
    | python -c 'import sys,json;print(json.load(sys.stdin)["file_id"])')
  ENC=$($CURL "$BASE/files?category=connection_docs&q=alert_demo_doc" -H "$DISP" \
    | python -c 'import sys,json;r=json.load(sys.stdin);print(r[0]["encrypted"] if r else "missing")')
  [ "$ENC" = "False" ] && ok "a connection document is now stored with encrypted=false (the alert's cause)" || fail "document encrypted flag: $ENC"
  $CURL -o /dev/null -X PUT "$BASE/admin/policies" -H "$ADMIN" -d "$ORIG_DOCS_POLICY"
  ok "policy restored right away; the one stored file is what the alert is about"

  echo "== 5. Put the video guarantee at risk: a chunked upload that stops receiving data"
  UPLOAD_ID=$(python -c 'import uuid;print(uuid.uuid4())')
  head -c 100000 /dev/urandom > "$TMP/v.bin"
  CODE=$($CURL -o /dev/null -w '%{http_code}' -X POST "$BASE/upload/chunk" -H "$DISP" \
    -F "upload_id=$UPLOAD_ID" -F chunk_index=0 -F total_chunks=2 -F filename=alert_demo_video.mp4 \
    -F category=drone_videos -F 'metadata={}' -F "file_chunk=@$TMPC/v.bin")
  [ "$CODE" = "200" ] && ok "chunk 1 of 2 uploaded; the second never comes" || fail "chunk upload: HTTP $CODE"
  sql "update upload_sessions set updated_at = now() - interval '1 hour' where id='$UPLOAD_ID'" >/dev/null
  ok "the upload's last activity is moved back one hour (a link that died long ago)"
fi

echo "== 6. The alerts fire in Prometheus and are DELIVERED to the team webhook"
for a in $WANT; do
  wait_until 180 is_firing "$a" && ok "$a is firing in Prometheus" || fail "$a did not fire within 3 min"
done
for a in $WANT; do
  wait_until 60 delivered "$a" firing && ok "$a reached the team webhook (Alertmanager -> webhook)" || fail "$a was not delivered to the webhook"
done
echo
for a in $WANT; do
  webhook "$a" | python -c '
import sys, json
last = [r for r in json.load(sys.stdin) if r["status"] == "firing"][-1]
print("   [%s] %-8s %s" % (last["status"].upper(), last["severity"], last["summary"]))
print("            guarantee at risk: %s" % last["guarantee"])'
done
echo

if [ "$MODE" != "quick" ]; then
  echo "== 7. Clean up and watch the state-based alerts resolve"
  cleanup_done=0
  $CURL -o /dev/null -X PUT "$BASE/admin/policies" -H "$ADMIN" -d "$ORIG_DOCS_POLICY"
  MSYS_NO_PATHCONV=1 docker compose exec -T ftp sh -c "rm -f /home/filegrid/connection_docs/$DOC_ID" >/dev/null 2>&1 || true
  sql "delete from files_metadata where id='$DOC_ID'" >/dev/null   # audit rows stay (see cleanup above)
  DOC_ID=""
  sql "delete from upload_sessions where id='$UPLOAD_ID'" >/dev/null   # (staging chunk removed by the exit trap)
  for a in PersonalDataStoredUnencrypted VideoUploadStalled; do
    wait_until 150 delivered "$a" resolved && ok "$a resolved and the 'resolved' notification was delivered" \
      || fail "$a did not resolve within 2.5 min"
  done
  note "FileIntegrityFailure keeps firing for 5 minutes (it looks at the last 5 minutes of failures), then resolves by itself"
fi

echo
echo "ALERT-FIRE DEMO PASSED"
