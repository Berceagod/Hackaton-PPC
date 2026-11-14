# Sourced by k8s/up.sh and k8s/down.sh: sets KIND to the kind executable.
# PATH first; then winget's install locations (Git Bash may not see winget's PATH change
# until a new terminal is opened).
KIND="$(command -v kind 2>/dev/null || true)"
if [ -z "$KIND" ] && [ -n "${LOCALAPPDATA:-}" ]; then
  _wg="$(cygpath -u "$LOCALAPPDATA" 2>/dev/null || echo "$LOCALAPPDATA")/Microsoft/WinGet"
  for _c in "$_wg/Links/kind.exe" "$_wg"/Packages/Kubernetes.kind_*/kind.exe; do
    [ -f "$_c" ] && { KIND="$_c"; break; }
  done
fi
[ -n "$KIND" ] || { echo "kind not found: install it (winget install Kubernetes.kind, or see https://kind.sigs.k8s.io)" >&2; exit 1; }
