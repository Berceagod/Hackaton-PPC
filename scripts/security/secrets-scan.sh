#!/usr/bin/env bash
# Slice F proof (PDF §4 "no password or key is in the repository"):
#   1. .env and certificates are git-ignored and not tracked;
#   2. gitleaks scans the whole git history;
#   3. gitleaks scans the files that would be committed now (tracked + new, minus ignored),
#      so uncommitted work is checked too, without ever reading .env.
# Needs only Docker (gitleaks runs in a pinned container).
set -euo pipefail
cd "$(dirname "$0")/../.."
GITLEAKS="zricethezav/gitleaks:v8.21.2"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
ok()   { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; exit 1; }
REPO="$(cygpath -m "$PWD" 2>/dev/null || echo "$PWD")"
SNAP="$(cygpath -m "$TMP" 2>/dev/null || echo "$TMP")"
export MSYS_NO_PATHCONV=1

echo "== 1. Secrets files are ignored and untracked"
for f in .env certs/tls.key certs/tls.crt x.pem x.key; do
  git check-ignore -q "$f" && ok "$f is git-ignored" || fail "$f is NOT ignored"
done
TRACKED=$(git ls-files | grep -E '(^|/)\.env$|\.(pem|key)$' || true)
[ -z "$TRACKED" ] && ok "no .env / .pem / .key file is tracked" || fail "tracked secret files: $TRACKED"

echo "== 2. gitleaks: full git history"
docker run --rm -v "$REPO:/repo:ro" "$GITLEAKS" detect --source /repo --redact --no-banner \
  && ok "no secret in any commit" || fail "gitleaks found secrets in the history (see above)"

echo "== 3. gitleaks: files that would be committed now"
git ls-files -z --cached --others --exclude-standard | while IFS= read -r -d '' f; do
  [ -f "$f" ] && mkdir -p "$TMP/$(dirname "$f")" && cp "$f" "$TMP/$f"
done
[ ! -e "$TMP/.env" ] || fail ".env would be committed"
docker run --rm -v "$SNAP:/scan:ro" "$GITLEAKS" detect --source /scan --no-git --redact --no-banner \
  && ok "no secret in the working tree (tracked + new files)" || fail "gitleaks found secrets in the working tree"

echo; echo "SECRETS SCAN PASSED"
