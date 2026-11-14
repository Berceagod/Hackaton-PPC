#!/usr/bin/env bash
# Degrade the network between the gateway and FileGrid's backend (toxiproxy), for the
# "slow and unstable connection" demo (PDF §4 Reziliență). Affects every client: the UI,
# chunked uploads and the meter simulator. Undo with scripts/chaos/network-ok.sh.
#   latency  400 ms +- 200 ms each way
#   bandwidth 2 MB/s upstream (client -> server)
#   20% of connections are cut after 0.5 s (reset_peer): uploads and readings get interrupted
set -euo pipefail
cd "$(dirname "$0")/../.."
export MSYS_NO_PATHCONV=1   # Git Bash: keep /toxiproxy-cli as a container path
cli() { docker compose exec -T toxiproxy /toxiproxy-cli "$@"; }

"$(dirname "$0")/network-ok.sh" >/dev/null 2>&1 || true   # start from a clean state
cli toxic add -n latency_down -t latency -a latency=400 -a jitter=200 backend >/dev/null
cli toxic add -n latency_up -t latency -a latency=400 -a jitter=200 --upstream backend >/dev/null
cli toxic add -n bandwidth_up -t bandwidth -a rate=2048 --upstream backend >/dev/null
cli toxic add -n cut_connections -t reset_peer -a timeout=500 --tox 0.2 backend >/dev/null
echo "network DEGRADED: 400±200 ms latency each way, 2 MB/s upload, 20% of connections cut after 0.5 s"
cli inspect backend | tail -n +2
