#!/usr/bin/env bash
# Brings the whole local system up from nothing: k3d cluster, images, store chart, platform chart.
# Safe to re-run: every step is create-if-missing or an idempotent helm upgrade.
# Usage: scripts/up.sh [--skip-build]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLUSTER=urumi
SECRETS="$ROOT/deploy/secrets/local.secrets.yaml"
DOMAIN=127-0-0-1.nip.io
SKIP_BUILD=false
[[ "${1:-}" == "--skip-build" ]] && SKIP_BUILD=true

step() { echo; echo "=== $* ($(date +%T))"; }

step "checking prerequisites"
for tool in docker k3d helm kubectl; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 1; }
done
docker info >/dev/null 2>&1 || { echo "Docker engine is not running; start Docker Desktop first" >&2; exit 1; }
[[ -f "$SECRETS" ]] || { echo "missing $SECRETS; run: python scripts/gen_secrets.py local" >&2; exit 1; }

step "k3d cluster '$CLUSTER'"
if k3d cluster list "$CLUSTER" >/dev/null 2>&1; then
  echo "exists; making sure it is started"
  k3d cluster start "$CLUSTER" >/dev/null
else
  k3d cluster create --config "$ROOT/deploy/k3d/cluster.yaml"
fi
kubectl config use-context "k3d-$CLUSTER" >/dev/null
kubectl -n kube-system rollout status deploy/coredns --timeout=300s
until kubectl -n kube-system get deploy traefik >/dev/null 2>&1; do sleep 5; done
kubectl -n kube-system rollout status deploy/traefik --timeout=300s

if $SKIP_BUILD; then
  step "importing existing images"
  k3d image import urumi/woo-mcp:dev urumi/gateway:dev urumi/notes-mcp:dev urumi/fx-mcp:dev urumi/dashboard:dev -c "$CLUSTER"
else
  step "building and importing images"
  "$ROOT/scripts/build-images.sh" --import
fi

step "preloading third-party images"
# Pulled once into the host Docker cache (survives cluster re-creation) and imported, so the cluster never
# downloads at install time; a slow pull would otherwise trip the Deployment progress deadline (build-log #38).
all_images=$( {
  helm template store-main "$ROOT/deploy/charts/woo-store" -f "$ROOT/deploy/values/woo-store/values-local.yaml" -f "$SECRETS"
  helm template platform "$ROOT/deploy/charts/platform" -f "$ROOT/deploy/values/platform/values-local.yaml" -f "$SECRETS"
} | sed -nE 's/^[[:space:]]*image:[[:space:]]*"?([^"[:space:]]+)"?.*/\1/p' | sort -u)
third_party=$(grep -v '^urumi/' <<<"$all_images")
for image in $third_party; do
  if docker image inspect "$image" >/dev/null 2>&1; then
    echo "cached: $image"
  else
    echo "pulling: $image"
    docker pull -q "$image"
  fi
done
# Single-platform export: Docker's containerd store saves a multi-arch index without the other platforms'
# layers, which the node's `ctr import` rejects (build-log #40).
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
# shellcheck disable=SC2086
docker save --platform "$(docker version --format '{{.Server.Os}}/{{.Server.Arch}}')" -o "$tmp/third-party.tar" $third_party
k3d image import "$tmp/third-party.tar" -c "$CLUSTER"

step "verifying images inside the node"
# k3d logs import errors but still exits 0, so check the node's image store directly.
present=$(docker exec "k3d-$CLUSTER-server-0" ctr -n k8s.io images ls -q)
missing=0
for image in $all_images; do
  case "$image" in
    */*/*) ref="$image" ;;
    */*) ref="docker.io/$image" ;;
    *) ref="docker.io/library/$image" ;;
  esac
  if grep -qxF "$ref" <<<"$present"; then echo "present: $ref"; else echo "MISSING: $ref" >&2; missing=1; fi
done
[[ $missing -eq 0 ]] || { echo "some images are missing in the node; aborting before helm" >&2; exit 1; }

step "store chart (store-main)"
helm upgrade --install store-main "$ROOT/deploy/charts/woo-store" -n store-main --create-namespace \
  -f "$ROOT/deploy/values/woo-store/values-local.yaml" -f "$SECRETS" --wait --timeout 15m

step "platform chart (platform)"
helm upgrade --install platform "$ROOT/deploy/charts/platform" -n platform --create-namespace \
  -f "$ROOT/deploy/values/platform/values-local.yaml" -f "$SECRETS" --wait --timeout 10m

step "ready"
kubectl get pods -n store-main
kubectl get pods -n platform
cat <<EOF

Store:      http://store.$DOMAIN
Dashboard:  http://app.$DOMAIN   (bootstrap admin: bootstrapAdminEmail / bootstrapAdminPassword in $SECRETS)
MCP:        http://mcp.$DOMAIN/mcp
EOF
