#!/usr/bin/env bash
# Proof (improvements.md 2.2): the system repairs damage by itself.
#
# The outage_records policy is switched to keep a second copy on another storage (S3 primary,
# FTP copy; a policy change, no code, no restart). Then:
#   1. Upload a file: the copy is written and verified.
#   2. Flip one byte of the PRIMARY inside S3 and download: it is repaired from the copy and
#      served byte-identical (X-Repaired: true). Without a copy this would be a 409.
#   3. Damage the primary again and do NOT download: the background scrubber repairs it.
#   4. Damage the COPY: the scrubber rewrites it from the primary.
#   5. Damage BOTH copies -> nothing damaged is served (409).
# Every repair is in the file's audit trail and counted in filegrid_repairs_total.
#
# Usage: scripts/demo/self-heal.sh [max_wait_seconds]   (default 120, for the scrubber steps)
# Overrides (another stack): BASE, ENV_FILE (default .env). Do not run with `bash -x`.
set -euo pipefail
cd "$(dirname "$0")/../.."

MAX_WAIT="${1:-120}"
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
sql()  { MSYS_NO_PATHCONV=1 docker compose exec -T -e PGOPTIONS="-c client_min_messages=warning" postgres-meta psql -U filegrid -d filegrid -tAc "$1" | tr -d '\r'; }
login() { $CURL -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$1\",\"password\":\"$2\"}" | python -c 'import sys,json;print(json.load(sys.stdin)["token"])'; }
[ -f "$ENV_FILE" ] || fail "no $ENV_FILE: run ./start.sh first"

ADMIN="Authorization: Bearer $(login admin@ppc.ro "$(env_value ADMIN_PASSWORD)")"
POLICY_BEFORE=$($CURL "$BASE/admin/policies" -H "$ADMIN")
[ "$(echo "$POLICY_BEFORE" | json 'd["outage_records"]["storage"]')" = "s3" ] || fail "this demo expects outage_records on s3"
policy() { echo "$POLICY_BEFORE" | python -c "
import sys,json
p=json.load(sys.stdin)['outage_records']; p['replicas']=$1; p.pop('replica_storage',None)
print(json.dumps({'outage_records':p}))"; }

FID=""
# touch one stored object inside a storage, behind FileGrid's back
s3op() { # $1 = flip | delete ; $2 = object key
  MSYS_NO_PATHCONV=1 docker compose exec -T -e OP="$1" -e KEY="$2" backend python -c '
import asyncio, os, aioboto3
async def main():
    async with aioboto3.Session().client("s3", endpoint_url=os.environ["S3_ENDPOINT"],
            aws_access_key_id=os.environ["S3_ACCESS_KEY"], aws_secret_access_key=os.environ["S3_SECRET_KEY"],
            region_name="us-east-1") as s3:
        b, k = os.environ["S3_BUCKET"], os.environ["KEY"]
        if os.environ["OP"] == "delete":
            await s3.delete_object(Bucket=b, Key=k); return
        body = bytearray(await (await s3.get_object(Bucket=b, Key=k))["Body"].read())
        body[100] ^= 0xFF
        await s3.put_object(Bucket=b, Key=k, Body=bytes(body))
asyncio.run(main())'
}
ftpflip() { # flip a byte of the copy held on FTP
  MSYS_NO_PATHCONV=1 docker compose exec -T -e KEY="$1" backend python -c '
import asyncio, os
from storage.ftp_storage import FtpStorage
async def main():
    s = FtpStorage(os.environ["FTP_HOST"], int(os.environ["FTP_PORT"]), os.environ["FTP_USER"], os.environ["FTP_PASSWORD"], 2)
    data = bytearray(b"".join([c async for c in s.get_stream(os.environ["KEY"])]))
    data[100] ^= 0xFF
    async def one():
        yield bytes(data)
    await s.put_stream(os.environ["KEY"], one())
asyncio.run(main())'
}
cleanup() {
  $CURL -o /dev/null -X PUT "$BASE/admin/policies" -H "$ADMIN" -d "$(policy 0)" || true
  if [ -n "$FID" ]; then
    sql "delete from file_replicas where file_id='$FID'; delete from files_metadata where id='$FID'" >/dev/null 2>&1 || true
    s3op delete "outage_records/$FID" >/dev/null 2>&1 || true
  fi
  rm -rf "$TMP"
}
trap cleanup EXIT
wait_for() { # $1 description, $2 sql returning the wanted value, $3 wanted
  local start=$(date +%s)
  while true; do
    [ "$(sql "$2" | tr -d ' ')" = "$3" ] && return 0
    [ $(( $(date +%s) - start )) -ge "$MAX_WAIT" ] && fail "$1 did not happen within ${MAX_WAIT} s"
    sleep 3
  done
}
due_now() { sql "update files_metadata set verified_at = now() - interval '2 hours' where id='$FID'" >/dev/null; }

echo "== 1. Keep a second copy of outage records: a policy change, no code, no restart"
$CURL -o /dev/null -X PUT "$BASE/admin/policies" -H "$ADMIN" -d "$(policy 1)"
ok "outage_records: replicas = 1 (copy goes to the first storage that is not the primary: ftp)"
head -c 300000 /dev/urandom > "$TMP/f.bin"
SRC=$(sha256sum "$TMP/f.bin" | cut -d' ' -f1)
FID=$($CURL -X POST "$BASE/ingest" -H "$ADMIN" -F category=outage_records -F "file=@$TMPC/f.bin;filename=self_heal_demo.bin" | json 'd["file_id"]')
[ "$(sql "select storage || ':' || status from file_replicas where file_id='$FID'")" = "ftp:ok" ] && ok "the copy was written to FTP and verified (file_replicas: ftp / ok)" || fail "no verified copy"

echo "== 2. Damage the primary inside S3, then download"
s3op flip "outage_records/$FID"
$CURL -D "$TMP/h.txt" -o "$TMP/down.bin" "$BASE/files/$FID/download" -H "$ADMIN"
head -1 "$TMP/h.txt" | grep -q " 200" && ok "download -> 200 (without a copy this is a 409)" || fail "download not 200: $(head -1 "$TMP/h.txt")"
grep -qi '^x-repaired: true' "$TMP/h.txt" && ok "X-Repaired: true" || fail "missing X-Repaired"
[ "$(sha256sum "$TMP/down.bin" | cut -d' ' -f1)" = "$SRC" ] && ok "the bytes received are identical to the bytes uploaded (SHA-256)" || fail "downloaded bytes differ"
[ "$($CURL "$BASE/files/$FID/verify" -H "$ADMIN" | json 'd["verified"]')" = "True" ] && ok "the primary in S3 itself is healthy again (verify)" || fail "primary still damaged"

echo "== 3. Damage the primary again and do NOT download: the scrubber repairs it"
s3op flip "outage_records/$FID"
sleep 6; due_now                    # a fast scrubber may be mid-check; bring this file's next round forward
wait_for "the scrubber's repair" "select count(*) from audit_logs where file_id='$FID' and action='repaired' and username='scrubber'" 1
ok "found and repaired by the scrubber (audit: repaired by 'scrubber'), no download involved"
[ "$($CURL "$BASE/files/$FID/verify" -H "$ADMIN" | json 'd["verified"]')" = "True" ] && ok "primary healthy" || fail "primary still damaged"

echo "== 4. Damage the COPY on FTP: the scrubber rewrites it from the primary"
ftpflip "outage_records/$FID"
sleep 6; due_now
wait_for "the copy's repair" "select count(*) from audit_logs where file_id='$FID' and action='replica_repaired'" 1
ok "audit: replica_repaired. Both copies are good again."

echo "== 5. Damage BOTH copies: nothing damaged is ever served"
s3op flip "outage_records/$FID"
ftpflip "outage_records/$FID"
CODE=$($CURL -o /dev/null -w '%{http_code}' "$BASE/files/$FID/download" -H "$ADMIN")
[ "$CODE" = "409" ] && ok "download -> 409: with no good copy left, nothing is served" || fail "expected 409, got $CODE"

echo "== The trail"
$CURL "$BASE/files/$FID/audit" -H "$ADMIN" | python -c '
import sys, json
for e in reversed(json.load(sys.stdin)):
    print("       %-17s %-9s %s" % (e["event"], e["user"], e["details"]))'
echo
echo "SELF-HEAL DEMO PASSED"
