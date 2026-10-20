#!/usr/bin/env bash
# Deploys both charts on the production k3s VM with images from GHCR.
# Safe to re-run: existing releases are backed up and Helm rolls back failed upgrades.
# Usage: scripts/deploy-prod.sh <git-sha> [--issuer letsencrypt-staging|letsencrypt-prod]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRETS="$ROOT/deploy/secrets/prod.secrets.yaml"
REGISTRY=ghcr.io/sajjad01-chaus
TAG="${1:-}"
ISSUER=letsencrypt-prod
[[ "${2:-}" == "--issuer" ]] && ISSUER="${3:?--issuer needs a name}"

step() { echo; echo "=== $* ($(date +%T))"; }
die() { echo "error: $*" >&2; exit 1; }

step "checking prerequisites"
[[ "$TAG" =~ ^[0-9a-f]{40}$ ]] || die "first argument must be a full 40-char commit SHA (e.g. \$(git rev-parse HEAD))"
[[ "$ISSUER" == letsencrypt-staging || "$ISSUER" == letsencrypt-prod ]] || die "unknown issuer '$ISSUER'"
for tool in kubectl helm curl; do command -v "$tool" >/dev/null || die "missing tool: $tool"; done
kubectl get crd clusterissuers.cert-manager.io >/dev/null 2>&1 || die "cert-manager is not installed (docs/vps-setup.md part 6)"
[[ -f "$SECRETS" ]] || die "missing $SECRETS; run: python3 scripts/gen_secrets.py prod"
[[ "$(stat -c %a "$SECRETS")" == 600 ]] || die "$SECRETS must be chmod 600"
grep -q REPLACE_WITH_YOUR_GROQ_KEY "$SECRETS" && die "paste the Groq key into groqApiKey in $SECRETS first"
for chart in platform woo-store; do
  grep -q CHANGE-ME "$ROOT/deploy/values/$chart/values-prod.yaml" && die "set global.domain in deploy/values/$chart/values-prod.yaml"
done
echo "ok (issuer: $ISSUER)"

platform_exists=false
store_exists=false
helm status platform -n platform >/dev/null 2>&1 && platform_exists=true
helm status store-main -n store-main >/dev/null 2>&1 && store_exists=true
if [[ "$platform_exists" != "$store_exists" ]]; then
  die "only one production release exists; resolve the partial state before deploying"
fi
if [[ "$platform_exists" == true ]]; then
  step "verified pre-deploy backup"
  bash "$ROOT/scripts/backup-prod.sh"
fi

# Fail before helm if an image is missing, instead of pods stuck in ImagePullBackOff.
step "checking images on GHCR for $TAG"
for svc in woo-mcp gateway provisioner dashboard notes-mcp fx-mcp; do
  token=$(curl -fsS "https://ghcr.io/token?scope=repository:${REGISTRY#ghcr.io/}/urumi-$svc:pull" | sed -E 's/.*"token":"([^"]+)".*/\1/')
  code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $token" \
    -H "Accept: application/vnd.oci.image.index.v1+json,application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.v2+json" \
    "https://ghcr.io/v2/${REGISTRY#ghcr.io/}/urumi-$svc/manifests/$TAG")
  [[ "$code" == 200 ]] || die "$REGISTRY/urumi-$svc:$TAG not pullable (HTTP $code): build not finished, or package not public"
  echo "urumi-$svc ok"
done

step "Let's Encrypt cluster issuers"
kubectl apply -f "$ROOT/deploy/prod/cluster-issuers.yaml"

step "store chart (store-main)"
kubectl get namespace store-main >/dev/null 2>&1 || kubectl create namespace store-main
kubectl label namespace store-main urumi.io/mcp-upstreams=true --overwrite >/dev/null
helm upgrade --install store-main "$ROOT/deploy/charts/woo-store" -n store-main --create-namespace \
  -f "$ROOT/deploy/values/woo-store/values-prod.yaml" -f "$SECRETS" \
  --set global.tls.clusterIssuer="$ISSUER" \
  --set wooMcp.image="$REGISTRY/urumi-woo-mcp:$TAG" \
  --rollback-on-failure --timeout 15m

step "platform chart (platform)"
kubectl get namespace platform >/dev/null 2>&1 || kubectl create namespace platform
kubectl label namespace platform urumi.io/mcp-upstreams=true --overwrite >/dev/null
helm upgrade --install platform "$ROOT/deploy/charts/platform" -n platform --create-namespace \
  -f "$ROOT/deploy/values/platform/values-prod.yaml" -f "$SECRETS" \
  --set global.tls.clusterIssuer="$ISSUER" \
  --set gateway.image="$REGISTRY/urumi-gateway:$TAG" \
  --set provisioner.image="$REGISTRY/urumi-provisioner:$TAG" \
  --set provisioner.wooMcpImage="$REGISTRY/urumi-woo-mcp:$TAG" \
  --set dashboard.image="$REGISTRY/urumi-dashboard:$TAG" \
  --set notesMcp.image="$REGISTRY/urumi-notes-mcp:$TAG" \
  --set fxMcp.image="$REGISTRY/urumi-fx-mcp:$TAG" \
  --rollback-on-failure --timeout 10m

step "status"
kubectl get pods -n store-main
kubectl get pods -n platform
kubectl get certificate -A
DOMAIN=$(sed -nE 's/^  domain: *"?([^"]+)"?$/\1/p' "$ROOT/deploy/values/platform/values-prod.yaml")
echo
echo "store:     https://store.$DOMAIN"
echo "dashboard: https://app.$DOMAIN"
echo "mcp:       https://mcp.$DOMAIN/mcp"
echo "certificates can take 1-2 minutes after this to become READY"
