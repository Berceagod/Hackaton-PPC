#!/usr/bin/env bash
# Proof (improvements.md 2.2): the system finds damaged files on its own, before anyone asks.
#
#   1. Show what the background scrubber is doing (GET /admin/scrubber).
#   2. Upload a file, then damage one byte of it inside the storage, behind FileGrid's back.
#   3. Do NOT download it. Wait: the scrubber reaches the file by itself, notices the SHA-256
#      no longer matches, writes a `scrub_failed` entry to the file's audit trail and moves
#      the integrity counter (so the FileIntegrityFailure alert fires).
#   4. A download of that file is refused (409) as well: the two checks agree.
#
# The file is checked in its turn, so on a stack with a long backlog it can take a few
# minutes; lower SCRUB_INTERVAL_SECONDS in .env to speed it up.
#
# Usage: scripts/demo/scrubber.sh [max_wait_seconds]     (default 300)
# Overrides (another stack): BASE, ENV_FILE (default .env). Do not run with `bash -x`.
set -euo pipefail
cd "$(dirname "$0")/../.."

MAX_WAIT="${1:-300}"
BASE="${BASE:-https://localhost/api/v1}"
ENV_FILE="${ENV_FILE:-.env}"
CURL="curl -sk"
TMP="$(mktemp -d)"
TMPC="$(cygpath -m "$TMP" 2>/dev/null || echo "$TMP")"

ok()   { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; exit 1; }
note() { printf '       %s\n' "$*"; }
env_value() { grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '\r'; }
json() { python -c "import sys,json;d=json.load(sys.stdin);print($1)"; }
sql()  { MSYS_NO_PATHCONV=1 docker compose exec -T postgres-meta psql -U filegrid -d filegrid -tAc "$1" | tr -d '\r'; }
login() { $CURL -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$1\",\"password\":\"$2\"}" | python -c 'import sys,json;print(json.load(sys.stdin)["token"])'; }
[ -f "$ENV_FILE" ] || fail "no $ENV_FILE: run ./start.sh first"

ADMIN="Authorization: Bearer $(login admin@ppc.ro "$(env_value ADMIN_PASSWORD)")"
DISP="Authorization: Bearer $(login dispecer@ppc.ro "$(env_value DISPECER_PASSWORD)")"
status() { $CURL "$BASE/admin/scrubber" -H "$ADMIN"; }
FID=""
cleanup() {  # the damaged demo file goes away; its audit rows stay (an audit is never edited)
  [ -n "$FID" ] && sql "delete from files_metadata where id='$FID'" >/dev/null 2>&1 || true
  [ -n "$FID" ] && MSYS_NO_PATHCONV=1 docker compose exec -T -e FID="$FID" backend python -c '
import asyncio, os, aioboto3
async def main():
    async with aioboto3.Session().client("s3", endpoint_url=os.environ["S3_ENDPOINT"],
            aws_access_key_id=os.environ["S3_ACCESS_KEY"], aws_secret_access_key=os.environ["S3_SECRET_KEY"],
            region_name="us-east-1") as s3:
        await s3.delete_object(Bucket=os.environ["S3_BUCKET"], Key="outage_records/" + os.environ["FID"])
asyncio.run(main())' >/dev/null 2>&1 || true
  rm -rf "$TMP"
}
trap cleanup EXIT

echo "== 1. The scrubber is running"
S=$(status)
[ "$(echo "$S" | json 'd["enabled"]')" = "True" ] && ok "enabled: a batch of $(echo "$S" | json 'd["batch"]') files per worker every $(echo "$S" | json 'd["interval_seconds"]') s, each file re-checked at most every $(echo "$S" | json 'd["recheck_seconds"]') s" || fail "scrubber disabled (SCRUB_ENABLED)"
note "categories: $(echo "$S" | json '", ".join(d["categories"])'); checked ok so far: $(echo "$S" | json 'd["verified_ok"]'), failed: $(echo "$S" | json 'd["failed"]'), still to check: $(echo "$S" | json 'd["never_checked"]')"
FAILED_BEFORE=$(echo "$S" | json 'd["failed"]')

echo "== 2. Upload a file, then damage one byte of it inside the storage"
POLICY=$($CURL "$BASE/admin/policies" -H "$DISP" | json 'd["outage_records"]["storage"]')
[ "$POLICY" = "s3" ] || fail "this step damages an S3 object; outage_records is on '$POLICY'"
head -c 400000 /dev/urandom > "$TMP/f.bin"
FID=$($CURL -X POST "$BASE/ingest" -H "$DISP" -F category=outage_records -F "file=@$TMPC/f.bin;filename=scrubber_demo.bin" | json 'd["file_id"]')
MSYS_NO_PATHCONV=1 docker compose exec -T -e FID="$FID" backend python -c '
import asyncio, os, aioboto3
async def main():
    key = "outage_records/" + os.environ["FID"]
    async with aioboto3.Session().client("s3", endpoint_url=os.environ["S3_ENDPOINT"],
            aws_access_key_id=os.environ["S3_ACCESS_KEY"], aws_secret_access_key=os.environ["S3_SECRET_KEY"],
            region_name="us-east-1") as s3:
        body = bytearray(await (await s3.get_object(Bucket=os.environ["S3_BUCKET"], Key=key))["Body"].read())
        body[100] ^= 0xFF
        await s3.put_object(Bucket=os.environ["S3_BUCKET"], Key=key, Body=bytes(body))
asyncio.run(main())'
ok "file $FID stored, then one byte changed in S3. Nobody has downloaded it."
# A fast scrubber may already have checked the file between the upload and the damage (state
# 'ok'): a verified file is re-checked only after SCRUB_RECHECK_SECONDS (an hour). In real life
# damage that happens later is found at that next round; here we fast-forward to it. Give any
# check already in flight a moment to finish first, so it cannot overwrite the result.
sleep 6
if [ "$(sql "select coalesce(verify_status,'-') from files_metadata where id='$FID'" | tr -d ' ')" = "ok" ]; then
  sql "update files_metadata set verified_at = now() - interval '2 hours' where id='$FID' and verify_status = 'ok'" >/dev/null
  note "(it had been checked before the damage: its next round is brought forward, as if an hour had passed)"
fi

echo "== 3. Wait for the scrubber to find it by itself (up to ${MAX_WAIT} s)"
START=$(date +%s)
while true; do
  STATE=$(sql "select coalesce(verify_status,'-') from files_metadata where id='$FID'" | tr -d ' ')
  [ "$STATE" = "failed" ] && break
  ELAPSED=$(( $(date +%s) - START ))
  [ "$ELAPSED" -ge "$MAX_WAIT" ] && fail "the scrubber had not reached the file after ${MAX_WAIT} s (state: $STATE; still to check: $(status | json 'd["never_checked"]'))"
  printf '       waiting... %3ss (state: %s, files still queued: %s)\r' "$ELAPSED" "$STATE" "$(status | json 'd["never_checked"]')"
  sleep 5
done
echo
ok "found after $(( $(date +%s) - START )) s, with no download and no human involved"
EVENTS=$($CURL "$BASE/files/$FID/audit" -H "$DISP")
echo "$EVENTS" | grep -q scrub_failed && ok "the file's audit trail has a scrub_failed entry" || fail "no scrub_failed audit entry"
echo "$EVENTS" | json '[e["details"] for e in d if e["event"]=="scrub_failed"][0]' | sed 's/^/       /'
[ "$(status | json 'd["failed"]')" -gt "$FAILED_BEFORE" ] && ok "GET /admin/scrubber: failed went $FAILED_BEFORE -> $(status | json 'd["failed"]')" || fail "failed count did not increase"
note "the integrity counter moved too, so the FileIntegrityFailure alert fires (see alert-fire.sh)"

echo "== 4. A download is refused as well: both checks agree"
CODE=$($CURL -o /dev/null -w '%{http_code}' "$BASE/files/$FID/download" -H "$DISP")
[ "$CODE" = "409" ] && ok "download -> HTTP 409, nothing damaged is served" || fail "expected 409, got $CODE"

echo
echo "SCRUBBER DEMO PASSED"
