#!/usr/bin/env bash
# Slice J proof on Kubernetes (after k8s/up.sh):
#   1. only the gateway is reachable from outside the cluster (NodePort); all else ClusterIP;
#   2. FileGrid works end to end through https://localhost:8443 (login, upload, encrypted
#      connection document, byte-identical download);
#   3. NetworkPolicy: a pod that is not the backend cannot open a connection to any storage
#      nor to the backend; the backend can (positive control); the gateway is open;
#   4. the meter simulator delivers readings, none lost;
#   5. more backend pods = more instances serving: raise the HPA minimum, every pod serves;
#   6. autoscaling: under load the HorizontalPodAutoscaler adds pods by itself
#      (skip with K8S_SKIP_LOAD=1).
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/lib/env.sh

K="kubectl --context kind-filegrid -n filegrid"
BASE="https://localhost:8443/api/v1"
CURL="curl -sk --max-time 60"
TMP="$(mktemp -d)"
TMPW="$(cygpath -m "$TMP" 2>/dev/null || echo "$TMP")"   # native curl/kubectl on Windows
ok()   { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; exit 1; }
cleanup() {
  $K patch hpa backend --type=merge -p '{"spec":{"minReplicas":2}}' >/dev/null 2>&1 || true
  $K delete job j-loadgen --ignore-not-found >/dev/null 2>&1 || true
  $K delete configmap j-loadgen --ignore-not-found >/dev/null 2>&1 || true
  rm -rf "$TMP"
}
trap cleanup EXIT
probe() { # pod-name host port -> open|blocked   (throwaway busybox pod, no app label)
  $K run "$1" --rm -i --restart=Never --image=busybox:1.36 --command -- \
    sh -c "nc -z -w 3 $2 $3 && echo open || echo blocked" 2>/dev/null | grep -E '^(open|blocked)$' | tail -1
}
sim_status() { $K exec deploy/simulator -- python -c \
  "import urllib.request,json;d=json.load(urllib.request.urlopen('http://127.0.0.1:8080/status'));print(json.dumps({'ok':d['counts']['ok'],'failed':d['counts']['failed'],'dropped':d['counts']['dropped'],'backlog':d['backlog']}))"; }
jf() { python -c "import sys,json;print(json.loads(sys.argv[1])[sys.argv[2]])" "$1" "$2"; }

echo "== 1. Only the gateway is exposed"
EXPOSED=$($K get svc -o jsonpath='{range .items[*]}{.metadata.name}={.spec.type} {end}')
echo "     $EXPOSED"
[ "$($K get svc -o jsonpath='{range .items[?(@.spec.type!="ClusterIP")]}{.metadata.name} {end}')" = "nginx " ] \
  && ok "only 'nginx' is reachable from outside (NodePort -> https://localhost:8443); storages and backend are ClusterIP" \
  || fail "unexpected externally reachable services: $EXPOSED"

echo "== 2. End to end through https://localhost:8443"
TOKEN=$($CURL -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"dispecer@ppc.ro\",\"password\":\"$(env_value DISPECER_PASSWORD)\"}" \
  | python -c 'import sys,json;print(json.load(sys.stdin)["token"])')
AUTH="Authorization: Bearer $TOKEN"
for cat in outage_records connection_docs; do
  head -c 1048576 /dev/urandom > "$TMP/$cat.bin"
  SHA=$(sha256sum "$TMP/$cat.bin" | cut -d' ' -f1)
  CODE=$($CURL -o "$TMPW/r.json" -w '%{http_code}' -X POST "$BASE/ingest" -H "$AUTH" \
    -F "category=$cat" -F 'metadata={"zone":"Banat"}' -F "file=@$TMPW/$cat.bin;filename=k8s_$cat.bin")
  [ "$CODE" = "202" ] || fail "$cat upload: HTTP $CODE $(cat "$TMP/r.json")"
  FID=$(python -c 'import sys,json;print(json.load(open(sys.argv[1]))["file_id"])' "$TMP/r.json")
  STORAGE=$(python -c 'import sys,json;print(json.load(open(sys.argv[1]))["storage"])' "$TMP/r.json")
  $CURL -D "$TMPW/h" -o "$TMPW/back.bin" "$BASE/files/$FID/download" -H "$AUTH"
  [ "$(sha256sum "$TMP/back.bin" | cut -d' ' -f1)" = "$SHA" ] && grep -qi '^x-integrity-verified: true' "$TMP/h" \
    && ok "$cat -> $STORAGE: 202, downloaded byte-identical, integrity verified" || fail "$cat download differs"
done

echo "== 3. NetworkPolicy: storages reachable only by the backend"
for target in postgres-meta:5432 postgres-blob:5432 minio:9000 ftp:21 backend:8000; do
  r=$(probe "np-probe-${target%%:*}" "${target%%:*}" "${target##*:}")
  [ "$r" = "blocked" ] && ok "pod outside the policy -> $target: blocked" || fail "$target reachable from an arbitrary pod ($r)"
done
[ "$(probe np-probe-gateway nginx 443)" = "open" ] && ok "pod -> nginx:443: open (the single entry point)" \
  || fail "gateway not reachable inside the cluster"
for target in postgres-meta:5432 minio:9000 ftp:21; do
  $K exec deploy/backend -- python -c "import socket;socket.create_connection(('${target%%:*}', ${target##*:}), 3)" \
    && ok "backend -> $target: open (positive control)" || fail "backend cannot reach $target"
done

echo "== 4. The meter simulator delivers through the gateway"
S1=$(sim_status); sleep 10; S2=$(sim_status)
DELTA=$(( $(jf "$S2" ok) - $(jf "$S1" ok) ))
[ "$DELTA" -gt 0 ] && [ "$(jf "$S2" failed)" = "0" ] && [ "$(jf "$S2" dropped)" = "0" ] \
  && ok "$DELTA readings stored in 10 s, failed 0, dropped 0, backlog $(jf "$S2" backlog)" \
  || fail "simulator: $S2"

echo "== 5. More instances: raise the autoscaler's minimum to 3 (configuration only)"
$K patch hpa backend --type=merge -p '{"spec":{"minReplicas":3}}' >/dev/null
for _ in $(seq 1 60); do
  [ "$($K get deploy backend -o jsonpath='{.status.readyReplicas}')" = "3" ] && break; sleep 3
done
[ "$($K get deploy backend -o jsonpath='{.status.readyReplicas}')" = "3" ] || fail "3 backend pods did not become ready"
sleep 20   # let the simulator's readings spread over the new pod
SPREAD=""
for p in $($K get pods -l app=backend -o jsonpath='{.items[*].metadata.name}'); do
  SPREAD+="${p##*-}=$($K logs "$p" --since=20s 2>/dev/null | grep -c '"reading stored"' || true) "
done
echo "$SPREAD" | grep -qE '=0( |$)' && fail "a pod served nothing: $SPREAD" \
  || ok "3 pods, every one serving readings in the last 20 s: $SPREAD"

if [ "${K8S_SKIP_LOAD:-0}" = "1" ]; then
  echo "== 6. Autoscaling under load: skipped (K8S_SKIP_LOAD=1)"
else
  echo "== 6. Autoscaling: load the backend, the HorizontalPodAutoscaler adds pods"
  $K patch hpa backend --type=merge -p '{"spec":{"minReplicas":2}}' >/dev/null
  for _ in $(seq 1 40); do   # metrics-server needs a minute after install
    $K get hpa backend -o jsonpath='{.status.currentMetrics[0].resource.current.averageUtilization}' 2>/dev/null \
      | grep -qE '^[0-9]+$' && break; sleep 3
  done
  BEFORE=$($K get deploy backend -o jsonpath='{.status.replicas}')
  $K create configmap j-loadgen --from-file=j-loadgen.py=scripts/j-loadgen.py --dry-run=client -o yaml | $K apply -f - >/dev/null
  cat > "$TMP/job.yaml" <<EOF
apiVersion: batch/v1
kind: Job
metadata: {name: j-loadgen}
spec:
  parallelism: 2
  completions: 2
  backoffLimit: 0
  template:
    metadata: {labels: {app: j-loadgen}}
    spec:
      restartPolicy: Never
      containers:
        - name: load
          image: filegrid/simulator:k8s
          imagePullPolicy: Never
          command: ["python", "/j/j-loadgen.py"]
          env:
            - {name: API_BASE, value: "https://nginx/api/v1"}
            - {name: VERIFY_TLS, value: /certs/tls.crt}
            - {name: DURATION, value: "150"}
            - {name: CONCURRENCY, value: "32"}
            - {name: SIZE_KB, value: "512"}
            - name: LOGIN_PASSWORD
              valueFrom: {secretKeyRef: {name: filegrid-backend, key: DISPECER_PASSWORD}}
          volumeMounts:
            - {name: script, mountPath: /j}
            - {name: ca, mountPath: /certs}
      volumes:
        - name: script
          configMap: {name: j-loadgen}
        - name: ca
          secret: {secretName: filegrid-tls, items: [{key: tls.crt, path: tls.crt}]}
EOF
  $K apply -f "$TMPW/job.yaml" >/dev/null
  MAX=$BEFORE
  for _ in $(seq 1 50); do
    sleep 4
    NOW=$($K get deploy backend -o jsonpath='{.status.replicas}')
    [ "$NOW" -gt "$MAX" ] && MAX=$NOW
    UTIL=$($K get hpa backend -o jsonpath='{.status.currentMetrics[0].resource.current.averageUtilization}' 2>/dev/null || echo "?")
    printf '     cpu %s%% of request, backend pods %s\n' "$UTIL" "$NOW"
    [ "$MAX" -ge 4 ] && break
  done
  [ "$MAX" -gt "$BEFORE" ] && ok "the autoscaler grew the backend from $BEFORE to $MAX pods under load, no manual step" \
    || fail "the autoscaler did not add pods (still $MAX)"
  $K wait --for=condition=complete job/j-loadgen --timeout=240s >/dev/null 2>&1 || true
  ERRS=$($K logs -l app=j-loadgen --tail=1 2>/dev/null | python -c '
import sys, json
r = [json.loads(l) for l in sys.stdin if l.strip().startswith("{")]
print(sum(x["errors"] for x in r), round(sum(x["req_per_s"] for x in r), 1))' || echo "? ?")
  ok "load generators: $(echo "$ERRS" | cut -d' ' -f2) uploads/s, $(echo "$ERRS" | cut -d' ' -f1) failed (the HPA scales back down after 60 s of low load)"
fi

echo; echo "KUBERNETES VERIFY PASSED"
