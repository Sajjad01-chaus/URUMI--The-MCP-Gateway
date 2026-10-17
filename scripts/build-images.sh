#!/usr/bin/env bash
# Builds every service image as urumi/<service>:<tag> and, with --import, loads them into the k3d cluster.
# Usage: scripts/build-images.sh [--import] [--tag TAG] [service ...]   (default: all services, tag "dev")
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLUSTER="${CLUSTER:-urumi}"
ALL_SERVICES=(woo-mcp gateway notes-mcp fx-mcp dashboard)
TAG=dev
IMPORT=false
services=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --import) IMPORT=true ;;
    --tag) TAG="$2"; shift ;;
    -h|--help) sed -n '2,3p' "$0"; exit 0 ;;
    *) services+=("$1") ;;
  esac
  shift
done
[[ ${#services[@]} -eq 0 ]] && services=("${ALL_SERVICES[@]}")

images=()
for svc in "${services[@]}"; do
  [[ -f "$ROOT/services/$svc/Dockerfile" ]] || { echo "unknown service '$svc' (no services/$svc/Dockerfile)" >&2; exit 2; }
  image="urumi/$svc:$TAG"
  echo "== building $image"
  start=$SECONDS
  # pipefail makes a failed build fail the script even though its output goes through tail (build-log #33).
  docker build -t "$image" "$ROOT/services/$svc" 2>&1 | tail -n 3
  echo "   done in $((SECONDS - start)) s"
  images+=("$image")
done

if $IMPORT; then
  echo "== importing ${#images[@]} image(s) into k3d cluster '$CLUSTER'"
  k3d image import "${images[@]}" -c "$CLUSTER"
fi
