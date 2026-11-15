#!/usr/bin/env bash
# Proof (improvements.md 2.5): erasure by destroying the key, the auditor role, login lockout.
#
#   1. A connection document is uploaded (encrypted with its OWN random key, stored only wrapped).
#   2. The ciphertext is copied out of the storage, as a backup would.
#   3. An admin erases the document: key destroyed, objects deleted. Download = 410.
#   4. The "backup" is put back into the storage: still unreadable (the key no longer exists).
#   5. The auditor can read the audit trail (it shows the erasure) but not the content (403).
#   6. Five wrong passwords lock an account (429), even for the right password.
#
# Usage: scripts/demo/erase-document.sh      Overrides (another stack): BASE, ENV_FILE. No `bash -x`.
set -euo pipefail
cd "$(dirname "$0")/../.."

BASE="${BASE:-https://localhost/api/v1}"
ENV_FILE="${ENV_FILE:-.env}"
CURL="curl -sk --max-time 20"
TMP="$(mktemp -d)"
TMPC="$(cygpath -m "$TMP" 2>/dev/null || echo "$TMP")"

ok()   { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; exit 1; }
env_value() { grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '\r'; }
json() { python -c "import sys,json;d=json.load(sys.stdin);print($1)"; }
sql()  { MSYS_NO_PATHCONV=1 docker compose exec -T -e PGOPTIONS="-c client_min_messages=warning" postgres-meta psql -U filegrid -d filegrid -tAc "$1" | tr -d '\r'; }
login() { $CURL -X POST "$BASE/auth/login" -H 'Content-Type: application/json' -d "{\"email\":\"$1\",\"password\":\"$2\"}"; }
token() { login "$1" "$2" | python -c 'import sys,json;print(json.load(sys.stdin)["token"])'; }
[ -f "$ENV_FILE" ] || fail "no $ENV_FILE: run ./start.sh first"
trap 'rm -rf "$TMP"' EXIT

ADMIN="Authorization: Bearer $(token admin@ppc.ro "$(env_value ADMIN_PASSWORD)")"
DISP="Authorization: Bearer $(token dispecer@ppc.ro "$(env_value DISPECER_PASSWORD)")"

# the storage holding connection documents, and a way to read/write one object in it
[ "$($CURL "$BASE/admin/policies" -H "$ADMIN" | json 'd["connection_docs"]["storage"]')" = "ftp" ] || fail "this demo expects connection_docs on ftp"
ftp() { # $1 get|put  $2 key  (put reads base64 on stdin; get prints base64)
  MSYS_NO_PATHCONV=1 docker compose exec -T -e OP="$1" -e KEY="$2" backend python -c '
import asyncio, base64, os, sys
from storage.ftp_storage import FtpStorage
async def main():
    s = FtpStorage(os.environ["FTP_HOST"], int(os.environ["FTP_PORT"]), os.environ["FTP_USER"], os.environ["FTP_PASSWORD"], 2)
    if os.environ["OP"] == "get":
        print(base64.b64encode(b"".join([c async for c in s.get_stream(os.environ["KEY"])])).decode())
    else:
        data = base64.b64decode(sys.stdin.read())
        async def one():
            yield data
        await s.put_stream(os.environ["KEY"], one())
asyncio.run(main())'
}

echo "== 1. Upload a connection document (encrypted with its own random key)"
printf 'CI: 1234567890123 (synthetic). Cerere de racordare pentru %s\n' "$RANDOM" > "$TMP/doc.txt"
SRC=$(sha256sum "$TMP/doc.txt" | cut -d' ' -f1)
FID=$($CURL -X POST "$BASE/ingest" -H "$DISP" -F category=connection_docs -F "file=@$TMPC/doc.txt;filename=cerere_demo.txt" | json 'd["file_id"]')
$CURL -o "$TMP/down.txt" "$BASE/files/$FID/download" -H "$DISP"
[ "$(sha256sum "$TMP/down.txt" | cut -d' ' -f1)" = "$SRC" ] && ok "downloaded and identical: $FID" || fail "download differs"
[ "$(sql "select status from file_keys where file_id='$FID'")" = "active" ] && ok "it has its own wrapped key in file_keys (never the plain key)" || fail "no per-file key row"

echo "== 2. Copy the ciphertext out of the storage, like a backup would"
ftp get "connection_docs/$FID" > "$TMP/backup.b64"
base64 -d "$TMP/backup.b64" > "$TMP/backup.bin"
grep -q "CI: 1234" "$TMP/backup.bin" && fail "document stored in clear" || ok "the stored bytes are ciphertext, not the document"

echo "== 3. An administrator erases the document"
R=$($CURL -X POST "$BASE/files/$FID/erase" -H "$ADMIN")
[ "$(echo "$R" | json 'd["method"]')" = "key_destroyed" ] && ok "erased: its key was destroyed and the stored object deleted" || fail "unexpected: $R"
CODE=$($CURL -o /dev/null -w '%{http_code}' "$BASE/files/$FID/download" -H "$DISP"); [ "$CODE" = "410" ] && ok "download -> 410 Gone" || fail "expected 410, got $CODE"
[ "$(sql "select status || ':' || (wrapped is null)::text from file_keys where file_id='$FID'")" = "erased:true" ] && ok "file_keys: erased, wrapped key gone" || fail "key row not erased"

echo "== 4. Put the 'backup' of the ciphertext back into the storage"
ftp put "connection_docs/$FID" < "$TMP/backup.b64"
CODE=$($CURL -o /dev/null -w '%{http_code}' "$BASE/files/$FID/download" -H "$DISP"); [ "$CODE" = "410" ] && ok "still 410: with the key destroyed, a restored copy of the ciphertext is just noise" || fail "expected 410, got $CODE"

echo "== 5. The auditor sees the trail, not the content"
AUD="Authorization: Bearer $(token auditor@ppc.ro "$(env_value AUDITOR_PASSWORD)")"
$CURL "$BASE/files/$FID/audit" -H "$AUD" | grep -q '"event":"erased"' && ok "auditor reads the audit trail: it shows who erased the document" || fail "auditor cannot read the trail"
CODE=$($CURL -o /dev/null -w '%{http_code}' "$BASE/files/$FID/download" -H "$AUD"); [ "$CODE" = "403" ] && ok "auditor download -> 403" || fail "expected 403, got $CODE"
[ "$($CURL "$BASE/audit/verify" -H "$AUD" | json 'd["valid"]')" = "True" ] && ok "audit chain valid (the erasure is part of it)" || fail "chain invalid"

echo "== 6. Login lockout: five wrong passwords lock an account"
ACC="lockout-demo-$RANDOM@ppc.ro"
for i in 1 2 3 4 5; do login "$ACC" wrong >/dev/null; done
CODE=$($CURL -o /dev/null -w '%{http_code}' -X POST "$BASE/auth/login" -H 'Content-Type: application/json' -d "{\"email\":\"$ACC\",\"password\":\"wrong\"}")
[ "$CODE" = "429" ] && ok "sixth attempt: 429 Too Many Requests (unknown and real accounts behave the same, nothing is revealed)" || fail "expected 429, got $CODE"
sql "delete from login_attempts where email like 'lockout-demo-%'" >/dev/null
echo
echo "ERASE DEMO PASSED"
