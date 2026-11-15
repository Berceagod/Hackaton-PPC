#!/usr/bin/env bash
# Proof (improvements.md 2.3): a signed integrity receipt FileGrid can be held to.
#
#   1. Upload a file and ask for its receipt: SHA-256, size, storage, trace id, and a fresh
#      re-verification of the stored bytes, signed with Ed25519.
#   2. Check it the way an OUTSIDE party would: only the published public key and the
#      `cryptography` library, no FileGrid code and no login.
#   3. Change one number in the receipt: the signature no longer matches (server and outsider).
#   4. Damage the file inside the storage: no receipt is issued (409), exactly like a download.
#
# Usage: scripts/demo/receipt.sh
# Overrides (another stack): BASE, ENV_FILE (default .env). Do not run with `bash -x`.
set -euo pipefail
cd "$(dirname "$0")/../.."

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
[ -f "$ENV_FILE" ] || fail "no $ENV_FILE: run ./start.sh first"

TOKEN=$($CURL -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"dispecer@ppc.ro\",\"password\":\"$(env_value DISPECER_PASSWORD)\"}" | python -c 'import sys,json;print(json.load(sys.stdin)["token"])')
AUTH="Authorization: Bearer $TOKEN"
FID=""
cleanup() {  # the demo's damaged file is removed; its audit rows stay (an audit is never edited)
  [ -n "$FID" ] && sql "delete from files_metadata where id='$FID'" >/dev/null 2>&1 || true
  rm -rf "$TMP"
}
trap cleanup EXIT

# An independent verifier: nothing from FileGrid, only the public key and `cryptography`.
outsider() { # $1 public key (base64), stdin: receipt JSON -> prints VALID / INVALID
  MSYS_NO_PATHCONV=1 docker compose exec -T -e PUB="$1" backend python -c '
import base64, json, os, sys
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
r = json.load(sys.stdin)
canon = json.dumps(r["payload"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
try:
    Ed25519PublicKey.from_public_bytes(base64.b64decode(os.environ["PUB"])).verify(base64.b64decode(r["signature"]), canon)
    print("VALID")
except InvalidSignature:
    print("INVALID")'
}

echo "== 1. Upload a file and ask for its receipt"
head -c 300000 /dev/urandom > "$TMP/f.bin"
SRC_SHA=$(sha256sum "$TMP/f.bin" | cut -d' ' -f1)
FID=$($CURL -X POST "$BASE/ingest" -H "$AUTH" -F category=outage_records -F "file=@$TMPC/f.bin;filename=chitanta_demo.bin" | json 'd["file_id"]')
$CURL -o "$TMP/receipt.json" -w '' "$BASE/files/$FID/receipt" -H "$AUTH"
[ "$(json 'd["payload"]["file"]["sha256"]' < "$TMP/receipt.json")" = "$SRC_SHA" ] && ok "the receipt carries the SHA-256 of the bytes we uploaded" || fail "sha mismatch in receipt"
[ "$(json 'd["payload"]["verification"]["ok"]' < "$TMP/receipt.json")" = "True" ] && ok "the stored bytes were re-verified just before signing" || fail "receipt without verification"
note "storage: $(json 'd["payload"]["file"]["storage"]' < "$TMP/receipt.json"), trace id: $(json 'd["payload"]["file"]["trace_id"]' < "$TMP/receipt.json"), signed with $(json 'd["alg"]' < "$TMP/receipt.json") key $(json 'd["key_id"]' < "$TMP/receipt.json")"

echo "== 2. Verify it like an outside party: public key only, no login, no FileGrid code"
PUB=$($CURL "$BASE/receipts/public-key" | json 'd["public_key"]')
[ "$(outsider "$PUB" < "$TMP/receipt.json" | tr -d '\r')" = "VALID" ] && ok "independent Ed25519 check -> VALID" || fail "independent check failed"
[ "$($CURL -X POST "$BASE/receipts/verify" --data-binary @"$TMPC/receipt.json" | json 'd["valid"]')" = "True" ] && ok "POST /receipts/verify (no login) -> valid" || fail "server verification failed"

echo "== 3. Change one number in the receipt (size 1 byte instead of the real one)"
python -c "
import json,sys
r = json.load(open(sys.argv[1])); r['payload']['file']['size_bytes'] = 1
json.dump(r, open(sys.argv[2], 'w'))" "$TMP/receipt.json" "$TMP/forged.json"
[ "$(outsider "$PUB" < "$TMP/forged.json" | tr -d '\r')" = "INVALID" ] && ok "independent check -> INVALID" || fail "forged receipt accepted by the outsider"
FORGED=$($CURL -X POST "$BASE/receipts/verify" --data-binary @"$TMPC/forged.json")
[ "$(echo "$FORGED" | json 'd["valid"]')" = "False" ] && ok "server -> not valid ($(echo "$FORGED" | json 'd["reason"]'))" || fail "forged receipt accepted by the server"

echo "== 4. Damage the file inside the storage: no receipt is issued"
STORAGE=$(json 'd["payload"]["file"]["storage"]' < "$TMP/receipt.json")
[ "$STORAGE" = "s3" ] || fail "this step damages an S3 object; outage_records is on '$STORAGE'"
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
CODE=$($CURL -o "$TMP/refused.json" -w '%{http_code}' "$BASE/files/$FID/receipt" -H "$AUTH")
[ "$CODE" = "409" ] && ok "HTTP 409: a damaged file gets no receipt" || fail "expected 409, got $CODE"
grep -q '"payload"' "$TMP/refused.json" && fail "a receipt was issued for a damaged file" || ok "the response contains no signed statement"
$CURL "$BASE/files/$FID/audit" -H "$AUTH" | grep -q receipt_refused && ok "the refusal is in the file's audit trail" || fail "no receipt_refused audit entry"

echo
echo "RECEIPT DEMO PASSED"
