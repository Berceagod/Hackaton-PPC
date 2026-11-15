#!/usr/bin/env bash
# Proof (improvements.md 2.3): the audit log is tamper-evident.
#
# Every audit row stores the hash of the row before it. This demo makes real activity, shows
# the chain is valid, then does what a person with database access could do:
#   A. rewrite who downloaded a file            -> GET /audit/verify names the row
#   B. delete a row from the middle of history  -> GET /audit/verify reports the break
# and restores each change, to show the check is about the change and is not simply broken.
#
# Usage: scripts/demo/audit-tamper.sh
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
sql()  { MSYS_NO_PATHCONV=1 docker compose exec -T -e PGOPTIONS="-c client_min_messages=warning" postgres-meta psql -U filegrid -d filegrid -tAc "$1" | tr -d '\r'; }
login() { $CURL -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$1\",\"password\":\"$2\"}" | python -c 'import sys,json;print(json.load(sys.stdin)["token"])'; }
[ -f "$ENV_FILE" ] || fail "no $ENV_FILE: run ./start.sh first"

ADMIN="Authorization: Bearer $(login admin@ppc.ro "$(env_value ADMIN_PASSWORD)")"
DISP="Authorization: Bearer $(login dispecer@ppc.ro "$(env_value DISPECER_PASSWORD)")"
verify() { $CURL "$BASE/audit/verify" -H "$ADMIN"; }

ROW=""; ORIG_USER=""; BACKUP=0
restore() { # put back whatever the demo changed, even when a step fails
  [ -n "$ROW" ] && [ -n "$ORIG_USER" ] && sql "update audit_logs set username='$ORIG_USER' where id='$ROW'" >/dev/null 2>&1 || true
  if [ "$BACKUP" = "1" ]; then
    sql "insert into audit_logs select * from audit_demo_backup where not exists (select 1 from audit_logs a where a.id = audit_demo_backup.id)" >/dev/null 2>&1 || true
    sql "drop table if exists audit_demo_backup" >/dev/null 2>&1 || true
  fi
  rm -rf "$TMP"
}
trap restore EXIT

echo "== 1. Real activity: upload, download and verify a file"
head -c 200000 /dev/urandom > "$TMP/f.bin"
FID=$($CURL -X POST "$BASE/ingest" -H "$DISP" -F category=outage_records -F "file=@$TMPC/f.bin;filename=audit_demo.bin" | json 'd["file_id"]')
$CURL -o /dev/null "$BASE/files/$FID/download" -H "$DISP"
$CURL -o /dev/null "$BASE/files/$FID/verify" -H "$DISP"
ok "three audit events written for $FID"

echo "== 2. The chain is valid"
V=$(verify)
[ "$(echo "$V" | json 'd["valid"]')" = "True" ] && ok "GET /audit/verify -> valid ($(echo "$V" | json 'd["checked"]') rows chained)" || fail "chain not valid before tampering: $V"
HEAD=$(echo "$V" | json 'd["head_hash"]')
note "head hash: ${HEAD:0:32}..."

echo "== 3. Someone with database access rewrites who downloaded the file"
ROW=$(sql "select id from audit_logs where file_id='$FID' and action='download' limit 1" | tr -d ' ')
[ -n "$ROW" ] || fail "no download audit row found"
ORIG_USER=$(sql "select username from audit_logs where id='$ROW'" | tr -d ' ')
sql "update audit_logs set username='intruder@example.com' where id='$ROW'" >/dev/null
V=$(verify)
[ "$(echo "$V" | json 'd["valid"]')" = "False" ] && ok "GET /audit/verify -> INVALID" || fail "tampering was not detected: $V"
[ "$(echo "$V" | json 'd["first_break"]["reason"]')" = "row_hash_mismatch" ] && ok "reason: row_hash_mismatch (the row itself was edited)" || fail "wrong reason: $V"
[ "$(echo "$V" | json 'd["first_break"]["id"]')" = "$ROW" ] && ok "it names the exact row that was changed ($ROW)" || fail "wrong row named: $V"
sql "update audit_logs set username='$ORIG_USER' where id='$ROW'" >/dev/null
[ "$(verify | json 'd["valid"]')" = "True" ] && ok "change undone -> valid again (the check measures the change)" || fail "chain did not recover"

echo "== 4. Someone deletes a row from the middle of the history"
sql "drop table if exists audit_demo_backup; create table audit_demo_backup as select * from audit_logs where id='$ROW'" >/dev/null
BACKUP=1
sql "delete from audit_logs where id='$ROW'" >/dev/null
V=$(verify)
[ "$(echo "$V" | json 'd["valid"]')" = "False" ] && ok "GET /audit/verify -> INVALID" || fail "deletion was not detected: $V"
[ "$(echo "$V" | json 'd["first_break"]["reason"]')" = "chain_broken" ] && ok "reason: chain_broken (the next row no longer fits the one before it)" || fail "wrong reason: $V"
sql "insert into audit_logs select * from audit_demo_backup" >/dev/null
sql "drop table audit_demo_backup" >/dev/null
BACKUP=0
[ "$(verify | json 'd["valid"]')" = "True" ] && ok "row put back -> valid again" || fail "chain did not recover after restoring the row"

echo "== 5. Only an administrator can run the check"
CODE=$($CURL -o /dev/null -w '%{http_code}' "$BASE/audit/verify" -H "$DISP")
[ "$CODE" = "403" ] && ok "a dispatcher gets 403" || fail "expected 403, got $CODE"

echo
note "Limit: deleting the LAST rows leaves a shorter, still-consistent chain. The head hash above is"
note "therefore logged to the log store (once an hour) as an anchor outside the database."
echo "AUDIT-TAMPER DEMO PASSED"
