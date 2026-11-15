#!/usr/bin/env bash
# The whole presentation in one command (improvements.md 2.9): every proof script, in the
# order of the PDF checklist, each introduced by what it proves, with a pass/fail summary.
# A rehearsed demo is a stronger demo; run this the evening before.
#
# Usage: scripts/demo/run-all.sh [options]
#   --fast             no pauses between steps (default pauses when run in a terminal)
#   --heavy            also run the 1.2 GB flat-memory upload and the scale-out demo (slow)
#   --only a,b         run only these steps (names as printed in the plan)
#   --skip a,b         skip these steps
#   --list             print the plan and exit
# Overrides (another stack): BASE, ENV_FILE are passed through to every script.
# A failing step does not stop the run; the exit code is the number of failed steps.
set -uo pipefail
cd "$(dirname "$0")/../.."

FAST=0; HEAVY=0; ONLY=""; SKIP=""; LIST=0
while [ $# -gt 0 ]; do
  case "$1" in
    --fast) FAST=1 ;;
    --heavy) HEAVY=1 ;;
    --only) ONLY=",$2,"; shift ;;
    --skip) SKIP=",$2,"; shift ;;
    --list) LIST=1 ;;
    -h|--help) sed -n 2,13p "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
[ -t 0 ] || FAST=1   # nobody to press Enter

# name | script and arguments | requirement it answers | what it proves (one line, for the audience)
PLAN=(
"smoke|scripts/demo/smoke.sh|PDF 3: store and retrieve, byte-identical|Login, upload, download, verify; a tampered file is refused (409)."
"policy-change|scripts/demo/policy-change.sh|PDF 3: policy without code changes|The same category lands on Postgres, FTP and S3 by changing a policy; no restart."
"corrupt-file|scripts/demo/corrupt-file.sh|PDF 3 + 4: provable integrity|A byte is changed inside each of the three storages; each download is refused."
"audit-tamper|scripts/demo/audit-tamper.sh|PDF 2.1: personal data auditable|The audit log is a hash chain: editing or deleting a row is detected."
"receipt|scripts/demo/receipt.sh|PDF 3: FileGrid can prove it|A signed receipt anyone can verify with only a public key."
"scrubber|scripts/demo/scrubber.sh|PDF 4: corrupt file detected|The system finds a damaged file by itself, before anyone asks."
"self-heal|scripts/demo/self-heal.sh|PDF 4: a damaged file is repaired|With a second copy on another storage, damage is repaired on download and by the scrubber; both copies damaged = nothing served."
"erase-document|scripts/demo/erase-document.sh|PDF 2.1 + 4: personal data protected and auditable|A document is erased by destroying its key (a restored backup stays unreadable); the auditor sees the trail but not the content; repeated bad logins lock an account."
"duplicate-reading|scripts/demo/duplicate-reading.sh|PDF 2.1 + 4: never duplicated|200 duplicates and 40 parallel copies of one reading: exactly one row."
"spike-10x|scripts/demo/spike-10x.sh|PDF 4: a 10x spike does not hurt the others|A meter storm; outage uploads keep their latency."
"kill-storage|scripts/demo/kill-storage.sh|PDF 4: a storage down loses nothing|A storage is stopped; readings are queued, nothing is lost, the others keep working."
"slow-network|scripts/demo/slow-network.sh|PDF 4: slow, unstable links|A video upload over a bad network completes or resumes by itself."
"trace-one-request|scripts/demo/trace-one-request.sh|PDF 4: follow one operation|One trace id from the gateway to the storage, found in the logs."
"chaos|scripts/demo/chaos.sh|PDF 4: failures on demand|Chaos console: S3 down (others keep working), meter storage down (readings queued, none lost), bad network, self-expiring faults. Skipped unless CHAOS_ENABLED=true."
"alert-fire|scripts/demo/alert-fire.sh quick|PDF 4: the team is alerted|A guarantee is broken; the alert fires and reaches the team webhook."
"unauthorized-access|scripts/demo/unauthorized-direct-storage-access.sh|PDF 4: storages not reachable|Direct access to a storage fails or yields only ciphertext."
)
HEAVY_PLAN=(
"large-file-1gb|scripts/demo/large-file-1gb.sh|PDF 4: 1 GB+ with flat resources|A 1.2 GB upload through a 512 MB container: memory stays flat."
"scale-out|scripts/demo/scale-out.sh|PDF 4: capacity by adding instances|Backend replicas are added with no code change; Prometheus sees them."
)
[ "$HEAVY" = "1" ] && PLAN+=("${HEAVY_PLAN[@]}")

wanted() { # name -> 0 when the step should run
  [ -n "$ONLY" ] && [[ "$ONLY" != *",$1,"* ]] && return 1
  [[ "$SKIP" == *",$1,"* ]] && return 1
  return 0
}

if [ "$LIST" = "1" ]; then
  printf '%-20s %-44s %s\n' STEP REQUIREMENT PROVES
  for row in "${PLAN[@]}"; do IFS='|' read -r n _ r p <<<"$row"; printf '%-20s %-44s %s\n' "$n" "$r" "$p"; done
  [ "$HEAVY" = "0" ] && echo "(add --heavy for large-file-1gb and scale-out)"
  exit 0
fi

LOGDIR="$(mktemp -d)"
trap 'rm -rf "$LOGDIR"' EXIT
RESULTS=(); FAILS=0; TOTAL=0
bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m' "$*"; }
red()   { printf '\033[31m%s\033[0m' "$*"; }

for row in "${PLAN[@]}"; do
  IFS='|' read -r name cmd req proves <<<"$row"
  wanted "$name" || continue
  TOTAL=$((TOTAL + 1))
  echo
  bold "━━ [$TOTAL] $name  ·  $req"
  echo "   $proves"
  [ "$FAST" = "1" ] || { read -r -p "   Enter to run... " _; }
  started=$(date +%s)
  # shellcheck disable=SC2086
  bash $cmd 2>&1 | tee "$LOGDIR/$name.log" | sed 's/^/   /'
  code=${PIPESTATUS[0]}
  took=$(( $(date +%s) - started ))
  if [ "$code" -eq 0 ]; then RESULTS+=("PASS|$name|${took}s|$req")
  elif [ "$code" -eq 77 ]; then RESULTS+=("SKIP|$name|${took}s|$req")   # precondition off (e.g. chaos console)
  else RESULTS+=("FAIL|$name|${took}s|$req"); FAILS=$((FAILS + 1)); fi
done

echo
bold "━━ Summary"
printf '   %-6s %-20s %-7s %s\n' RESULT STEP TIME REQUIREMENT
for r in "${RESULTS[@]}"; do
  IFS='|' read -r res name took req <<<"$r"
  if [ "$res" = "PASS" ]; then mark="$(green PASS)  "; elif [ "$res" = "SKIP" ]; then mark="SKIP  "; else mark="$(red FAIL)  "; fi
  printf '   %b %-20s %-7s %s\n' "$mark" "$name" "$took" "$req"
done
echo
if [ "$FAILS" -eq 0 ]; then green "   All $TOTAL steps passed."; echo
else red "   $FAILS of $TOTAL steps failed."; echo; echo "   Re-run one with: bash scripts/demo/<step>.sh"; fi
exit "$FAILS"
