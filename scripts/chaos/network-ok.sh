#!/usr/bin/env bash
# Remove every network fault added by scripts/chaos/network-bad.sh.
# Removing toxics while hundreds of connections are active can deadlock toxiproxy (it waits
# for every connection to drain), after which every request through it times out (504).
# Restarting the container is the reliable reset: it comes back from its config file as a
# plain pass-through within seconds; any connection it cuts is retried by the clients.
set -euo pipefail
cd "$(dirname "$0")/../.."
export MSYS_NO_PATHCONV=1
docker compose restart toxiproxy >/dev/null 2>&1
for _ in $(seq 1 30); do
  docker compose exec -T toxiproxy /toxiproxy-cli list >/dev/null 2>&1 && break
  sleep 1
done
if docker compose exec -T toxiproxy /toxiproxy-cli inspect backend 2>/dev/null | grep -q "type="; then
  echo "toxiproxy still has toxics after restart" >&2; exit 1
fi
echo "network OK: no faults between gateway and backend"
