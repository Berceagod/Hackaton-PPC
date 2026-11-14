#!/usr/bin/env bash
# Proof for F4 + R3: a file corrupted INSIDE a storage is detected on retrieval and never
# served, on each of the three storages. For postgres, ftp and s3 in turn:
#   upload -> download OK -> flip bytes directly in the storage -> download = 409,
#   verify = false, audit trail records the blocked download.
# Usage: scripts/demo/corrupt-file.sh
set -euo pipefail
cd "$(dirname "$0")/../.."

BASE="${BASE:-https://localhost/api/v1}"
CURL="curl -sk"
TMP="$(mktemp -d)"
TMPC="$(cygpath -m "$TMP" 2>/dev/null || echo "$TMP")"

ok()   { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; exit 1; }
json() { python -c "import sys,json;print(json.load(sys.stdin)$1)"; }
login() { $CURL -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$1\",\"password\":\"$2\"}" | json '["token"]'; }
policy() { printf '{"outage_records":{"storage":"%s","encryption":false,"deduplicate":false,"priority":"high"}}' "$1"; }

ADMIN="Authorization: Bearer $(login admin@ppc.ro admin)"
DISP="Authorization: Bearer $(login dispecer@ppc.ro dispecer)"
restore() { $CURL -o /dev/null -X PUT "$BASE/admin/policies" -H "$ADMIN" -d "$(policy s3)" || true; rm -rf "$TMP"; }
trap restore EXIT

tamper() { # STORAGE FILE_ID : change the stored bytes behind FileGrid's back
  case "$1" in
    postgres)
      docker compose exec -T postgres-blob psql -U filegrid -d filegrid_blob -qc \
        "update blob_chunks set data = set_byte(data, 0, (get_byte(data, 0) + 1) % 256) where key='outage_records/$2' and seq=0" ;;
    ftp)
      MSYS_NO_PATHCONV=1 docker compose exec -T ftp sh -c \
        "printf 'X' | dd of=/home/filegrid/outage_records/$2 bs=1 seek=10 conv=notrunc 2>/dev/null" ;;
    s3)
      docker compose exec -T -e FID="$2" backend python -c '
import asyncio, os, aioboto3
async def main():
    key = "outage_records/" + os.environ["FID"]
    async with aioboto3.Session().client("s3", endpoint_url=os.environ["S3_ENDPOINT"],
            aws_access_key_id=os.environ["S3_ACCESS_KEY"],
            aws_secret_access_key=os.environ["S3_SECRET_KEY"], region_name="us-east-1") as s3:
        body = bytearray(await (await s3.get_object(Bucket=os.environ["S3_BUCKET"], Key=key))["Body"].read())
        body[10] ^= 0xFF
        await s3.put_object(Bucket=os.environ["S3_BUCKET"], Key=key, Body=bytes(body))
asyncio.run(main())' ;;
  esac
}

head -c 2097152 /dev/urandom > "$TMP/src.bin"
SRC_SHA=$(sha256sum "$TMP/src.bin" | cut -d' ' -f1)

for STORAGE in postgres ftp s3; do
  echo "== $STORAGE"
  $CURL -o /dev/null -X PUT "$BASE/admin/policies" -H "$ADMIN" -d "$(policy "$STORAGE")"
  RESP=$($CURL -X POST "$BASE/ingest" -H "$DISP" -F category=outage_records \
    -F "file=@$TMPC/src.bin;filename=corrupt_demo_$STORAGE.bin")
  FID=$(echo "$RESP" | json '["file_id"]')
  [ "$(echo "$RESP" | json '["storage"]')" = "$STORAGE" ] && ok "2 MB file stored on $STORAGE" || fail "not stored on $STORAGE: $RESP"

  CODE=$($CURL -o "$TMP/good.bin" -w '%{http_code}' "$BASE/files/$FID/download" -H "$DISP")
  [ "$CODE" = "200" ] && cmp -s "$TMP/src.bin" "$TMP/good.bin" && ok "before tampering: 200, byte-identical" || fail "clean download failed ($CODE)"

  tamper "$STORAGE" "$FID"
  ok "one byte changed directly in $STORAGE (bypassing FileGrid)"

  CODE=$($CURL -D "$TMP/h" -o "$TMP/bad.bin" -w '%{http_code}' "$BASE/files/$FID/download" -H "$DISP")
  [ "$CODE" = "409" ] && ok "download -> HTTP 409" || fail "expected 409, got $CODE"
  grep -qi '^x-integrity-verified: false' "$TMP/h" && ok "X-Integrity-Verified: false" || fail "header missing"
  [ "$(wc -c < "$TMP/bad.bin")" -lt 2000 ] && ok "no file content delivered (only the JSON error)" || fail "file bytes were delivered"
  [ "$(json '["reason"]' < "$TMP/bad.bin")" = "checksum_mismatch" ] && ok "reason: checksum_mismatch" || fail "wrong reason"
  [ "$(json '["expected_sha256"]' < "$TMP/bad.bin")" = "$SRC_SHA" ] && ok "expected SHA-256 = the one recorded at ingest" || fail "expected sha wrong"

  $CURL "$BASE/files/$FID/verify" -H "$DISP" | grep -q '"verified":false' && ok "verify -> false" || fail "verify did not fail"
  $CURL "$BASE/files/$FID/audit" -H "$DISP" | grep -q '"event":"download_blocked"' \
    && ok "audit trail records the blocked download" || fail "audit entry missing"
done

echo; echo "CORRUPT-FILE DEMO PASSED (policy restored to s3)"
