#!/usr/bin/env bash
# Delete the local FileGrid Kubernetes cluster (and its volumes). The compose stack is untouched.
set -euo pipefail
cd "$(dirname "$0")/.."
source k8s/find-kind.sh
"$KIND" delete cluster --name filegrid
