#!/usr/bin/env bash
# Slice A proof: login -> ingest outage record (S3) -> list -> download -> byte-identical,
# then verify, then tamper with the stored object and show the corrupt file is refused (409).
# Usage: scripts/demo/smoke.sh [size_mb]     (default 12 MB: exercises S3 multipart)
set -euo pipefail
cd "$(dirname "$0")/../.."
source scripts/lib/env.sh

BASE="${BASE:-https://localhost/api/v1}"
SIZE_MB="${1:-12}"
CURL="curl -sk"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
# Git Bash on Windows: native curl needs a Windows-style path inside -F "file=@..."
TMPC="$(cygpath -m "$TMP" 2>/dev/null || echo "$TMP")"

ok()   { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; exit 1; }

echo "== 1. Login"
TOKEN=$($CURL -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"dispecer@ppc.ro\",\"password\":\"$(env_value DISPECER_PASSWORD)\"}" | python -c 'import sys,json;print(json.load(sys.stdin)["token"])')
[ -n "$TOKEN" ] && ok "got JWT"
AUTH="Authorization: Bearer $TOKEN"

echo "== 2. Unauthenticated ingest is rejected"
echo x > "$TMP/x.txt"
CODE=$($CURL -o /dev/null -w '%{http_code}' -X POST "$BASE/ingest" -F category=outage_records -F "file=@$TMPC/x.txt")
[ "$CODE" = "401" ] && ok "401 without token" || fail "expected 401, got $CODE"

echo "== 3. Ingest a ${SIZE_MB} MB synthetic outage record"
head -c $((SIZE_MB * 1024 * 1024)) /dev/urandom > "$TMP/avarie.bin"
SRC_SHA=$(sha256sum "$TMP/avarie.bin" | cut -d' ' -f1)
$CURL -D "$TMP/h1" -o "$TMP/ingest.json" -X POST "$BASE/ingest" -H "$AUTH" \
  -F category=outage_records -F 'metadata={"zone":"Dobrogea","station":"ST-Constanta-Est"}' \
  -F "file=@$TMPC/avarie.bin;filename=avarie_test.bin"
grep -q '^HTTP/[0-9.]* 202' "$TMP/h1" && ok "HTTP 202" || fail "ingest did not return 202: $(grep '^HTTP' "$TMP/h1" | tail -1)"
FILE_ID=$(python -c 'import sys,json;print(json.load(open(sys.argv[1]))["file_id"])' "$TMP/ingest.json")
SRV_SHA=$(python -c 'import sys,json;print(json.load(open(sys.argv[1]))["sha256"])' "$TMP/ingest.json")
[ "$SRC_SHA" = "$SRV_SHA" ] && ok "server SHA-256 == local SHA-256" || fail "sha mismatch"
grep -qi '^x-trace-id:' "$TMP/h1" && ok "X-Trace-Id header: $(grep -i '^x-trace-id:' "$TMP/h1" | tr -d '\r' | cut -d' ' -f2)"

echo "== 4. Listed"
$CURL "$BASE/files?category=outage_records&storage=s3" -H "$AUTH" | grep -q "$FILE_ID" && ok "file visible in /files" || fail "not listed"

echo "== 5. Download is byte-identical and integrity-verified"
$CURL -D "$TMP/h2" -o "$TMP/out.bin" "$BASE/files/$FILE_ID/download" -H "$AUTH"
cmp "$TMP/avarie.bin" "$TMP/out.bin" && ok "bytes identical (cmp)"
grep -qi '^x-integrity-verified: true' "$TMP/h2" && ok "X-Integrity-Verified: true" || fail "header missing"

echo "== 6. Verify endpoint"
$CURL "$BASE/files/$FILE_ID/verify" -H "$AUTH" | grep -q '"verified":true' && ok "verify -> true"

echo "== 7. Tamper with the object inside S3, then download again"
docker compose exec -T backend python - "$FILE_ID" <<'PY'
import asyncio, os, sys
import aioboto3
async def main():
    key = f"outage_records/{sys.argv[1]}"
    async with aioboto3.Session().client("s3", endpoint_url=os.environ["S3_ENDPOINT"],
            aws_access_key_id=os.environ["S3_ACCESS_KEY"], aws_secret_access_key=os.environ["S3_SECRET_KEY"],
            region_name="us-east-1") as s3:
        await s3.put_object(Bucket=os.environ["S3_BUCKET"], Key=key, Body=b"corrupted-by-demo")
asyncio.run(main())
PY
CODE=$($CURL -o "$TMP/bad.bin" -D "$TMP/h3" -w '%{http_code}' "$BASE/files/$FILE_ID/download" -H "$AUTH")
[ "$CODE" = "409" ] && ok "HTTP 409 (corrupt file refused)" || fail "expected 409, got $CODE"
grep -qi '^x-integrity-verified: false' "$TMP/h3" && ok "X-Integrity-Verified: false"
grep -q 'corrupted-by-demo' "$TMP/bad.bin" && fail "corrupt bytes were served!" || ok "no corrupt byte was delivered"

echo; echo "SMOKE TEST PASSED"
