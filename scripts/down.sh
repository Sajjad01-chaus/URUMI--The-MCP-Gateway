#!/usr/bin/env bash
# Deletes the local k3d cluster and ALL its data (store, orders, users, audit). Images stay in Docker.
# Usage: scripts/down.sh --yes
set -euo pipefail

CLUSTER=urumi
if [[ "${1:-}" != "--yes" ]]; then
  echo "This deletes cluster '$CLUSTER' and every database in it. Re-run with --yes to confirm." >&2
  exit 1
fi
if k3d cluster list "$CLUSTER" >/dev/null 2>&1; then
  k3d cluster delete "$CLUSTER"
else
  echo "cluster '$CLUSTER' does not exist; nothing to do"
fi
