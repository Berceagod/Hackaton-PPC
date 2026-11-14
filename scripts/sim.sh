#!/usr/bin/env bash
# Control the meter simulator. It publishes no host port (only nginx does), so we
# talk to its control API from inside its own container.
#
#   scripts/sim.sh status            current mode, rate and counters (JSON)
#   scripts/sim.sh mode storm        normal | storm   (storm = BASE_RATE x STORM_MULTIPLIER)
#   scripts/sim.sh rate 50           readings/second in normal mode
#   scripts/sim.sh pause | resume
#   scripts/sim.sh dupes 0.2         share of readings re-sent on purpose (0..1)
#   scripts/sim.sh burst 200         re-send the last N readings right now
set -euo pipefail
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1   # Git Bash: do not rewrite /status, /mode ... into Windows paths

call() { # METHOD PATH [JSON_BODY]
  docker compose exec -T simulator python -c '
import sys, urllib.request
method, path, body = sys.argv[1], sys.argv[2], sys.argv[3]
req = urllib.request.Request(
    "http://127.0.0.1:8080" + path,
    data=body.encode() if body else None,
    method=method,
    headers={"Content-Type": "application/json"},
)
print(urllib.request.urlopen(req, timeout=10).read().decode())
' "$1" "$2" "${3:-}"
}

case "${1:-status}" in
  status) call GET /status ;;
  mode)   call POST /mode "{\"mode\":\"${2:?normal|storm}\"}" ;;
  rate)   call PATCH /config "{\"base_rate\":${2:?readings per second}}" ;;
  pause)  call PATCH /config '{"paused":true}' ;;
  resume) call PATCH /config '{"paused":false}' ;;
  dupes)  call PATCH /config "{\"duplicate_ratio\":${2:?0..1}}" ;;
  burst)  call POST /duplicates/burst "{\"count\":${2:-100}}" ;;
  *) sed -n '2,10p' "$0"; exit 2 ;;
esac
