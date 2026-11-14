#!/usr/bin/env bash
# Slice J proof (PDF §4 Scalabilitate): "capacity grows by adding instances, without code
# changes".
#   1. one backend instance (1 worker, so one instance's capacity is the limit) takes a
#      fixed load of 512 KB outage uploads from 32 concurrent clients inside the network;
#   2. the SAME image is scaled to N instances with one command
#      (`docker compose up -d --scale backend=N`): nothing is rebuilt or edited;
#   3. the same load again: throughput must grow (>= 1.8x for 3 instances), every instance
#      takes part (nginx -> toxiproxy -> Docker DNS spreads the connections), no errors;
#   4. back to the normal setup (1 instance, default workers).
# Usage: scripts/demo/scale-out.sh [N]   (default 3)
set -euo pipefail
cd "$(dirname "$0")/../.."
source scripts/lib/env.sh

N="${1:-3}"
WORKERS="${SCALE_DEMO_WORKERS:-1}"
DURATION="${SCALE_DEMO_SECONDS:-20}"
CONCURRENCY="${SCALE_DEMO_CONCURRENCY:-32}"   # per generator
LOADGENS="${SCALE_DEMO_LOADGENS:-2}"
ok()   { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; exit 1; }
TMP="$(mktemp -d)"
restore() {
  docker compose up -d --scale backend=1 >/dev/null 2>&1 || true   # default workers again
  bash scripts/sim.sh resume >/dev/null 2>&1 || true
  rm -rf "$TMP"
}
trap restore EXIT

scale() { # replicas -> waits until all are healthy
  UVICORN_WORKERS="$WORKERS" docker compose up -d --scale backend="$1" >/dev/null 2>&1
  for _ in $(seq 1 60); do
    healthy=$(docker compose ps backend --format '{{.Status}}' | grep -c '(healthy)' || true)
    [ "$healthy" = "$1" ] && return 0
    sleep 2
  done
  fail "only $healthy of $1 backend instances became healthy"
}
load() { # -> one JSON line summing LOADGENS parallel generators (scripts/j-loadgen.py).
  # One Python client process tops out around 95 uploads/s (measured), so several run in
  # parallel: otherwise the client, not FileGrid, would be the limit being measured.
  local i
  for i in $(seq 1 "$LOADGENS"); do
    ( MSYS_NO_PATHCONV=1 docker compose run --rm --no-deps -T \
        -e LOGIN_PASSWORD="$(env_value DISPECER_PASSWORD)" -e DURATION="$DURATION" \
        -e CONCURRENCY="$CONCURRENCY" -e SIZE_KB=512 \
        -v "$(cygpath -m "$PWD" 2>/dev/null || echo "$PWD")/scripts:/j:ro" \
        simulator python /j/j-loadgen.py 2>/dev/null | tail -1 > "$TMP/load_$i.json" ) &
  done
  wait
  cat "$TMP"/load_*.json | python -c '
import sys, json
r = [json.loads(l) for l in sys.stdin if l.strip()]
print(json.dumps({
    "req_per_s": round(sum(x["req_per_s"] for x in r), 1),
    "mb_per_s": round(sum(x["mb_per_s"] for x in r), 1),
    "p95_ms": max(x["p95_ms"] for x in r),
    "errors": sum(x["errors"] for x in r),
    "generators": len(r)}))'
  rm -f "$TMP"/load_*.json
}
field() { python -c "import sys,json;print(json.loads(sys.argv[1])[sys.argv[2]])" "$1" "$2"; }
per_instance() { # since -> "name=count ..." of stored files per backend container
  for c in $(docker compose ps -q backend); do
    printf '%s=%s ' "$(docker inspect -f '{{.Name}}' "$c" | sed 's#^/##; s#^.*-backend-#backend-#')" \
      "$(docker logs --since "$1" "$c" 2>&1 | grep -c '"file stored"' || true)"
  done
}

bash scripts/sim.sh pause >/dev/null   # the meter simulator would add its own load: pause it
IMAGE_BEFORE=$(docker compose images backend --format '{{.ID}}' 2>/dev/null | head -1 || true)

echo "== 1. One backend instance (${WORKERS} worker), ${LOADGENS} generators x ${CONCURRENCY} clients x ${DURATION} s of 512 KB uploads"
scale 1
R1=$(load); [ -n "$R1" ] || fail "load generator produced no result"
ok "1 instance : $(field "$R1" req_per_s) uploads/s ($(field "$R1" mb_per_s) MB/s), p95 $(field "$R1" p95_ms) ms, errors $(field "$R1" errors)"

echo "== 2. Scale to ${N} instances: one command, same image, no code change"
echo "     \$ docker compose up -d --scale backend=${N}"
scale "$N"
SINCE=$(date -u +%Y-%m-%dT%H:%M:%SZ)
RN=$(load); [ -n "$RN" ] || fail "load generator produced no result"
ok "${N} instances: $(field "$RN" req_per_s) uploads/s ($(field "$RN" mb_per_s) MB/s), p95 $(field "$RN" p95_ms) ms, errors $(field "$RN" errors)"
SPREAD=$(per_instance "$SINCE")
ok "uploads served per instance: ${SPREAD}"

echo "== 3. Verdict"
GAIN=$(python -c "print(round($(field "$RN" req_per_s) / max($(field "$R1" req_per_s), 0.1), 2))")
MIN_GAIN=$(python -c "print(round(1 + 0.4 * ($N - 1), 1))")   # 1.8x for 3 instances
python -c "import sys; sys.exit(0 if $GAIN >= $MIN_GAIN else 1)" \
  && ok "throughput x${GAIN} with ${N} instances (required >= x${MIN_GAIN})" \
  || fail "throughput only x${GAIN} with ${N} instances (required >= x${MIN_GAIN})"
[ "$(field "$R1" errors)" = "0" ] && [ "$(field "$RN" errors)" = "0" ] && ok "no failed upload in either run" \
  || fail "failed uploads: $(field "$R1" errors) / $(field "$RN" errors)"
echo "$SPREAD" | grep -qE '=0( |$)' && fail "an instance took no traffic: $SPREAD" \
  || ok "every instance took part"
if docker compose ps --services --status running | grep -qx prometheus; then
  # slice H x J: Prometheus finds the replicas by DNS on `backend` (refresh 5 s)
  UP=0
  for _ in $(seq 1 15); do
    UP=$(MSYS_NO_PATHCONV=1 docker compose exec -T prometheus wget -qO- \
      'http://localhost:9090/api/v1/query?query=count(up{job="backend"}==1)' 2>/dev/null \
      | python -c 'import sys,json;r=json.load(sys.stdin)["data"]["result"];print(r[0]["value"][1] if r else 0)' || echo 0)
    [ "$UP" = "$N" ] && break; sleep 2
  done
  [ "$UP" = "$N" ] && ok "Prometheus scrapes all ${N} instances by itself (job backend, DNS discovery; Grafana: Backend instances up)" \
    || fail "Prometheus sees ${UP} backend instances, expected ${N}"
fi
IMAGES=$(docker compose ps -q backend | xargs docker inspect -f '{{.Image}}' | sort -u | wc -l)
[ "$IMAGES" = "1" ] && ok "all ${N} instances run the same image (nothing rebuilt, no code changed)" \
  || fail "instances run $IMAGES different images"

echo; echo "SCALE-OUT DEMO PASSED (restoring 1 instance with default workers)"
