#!/usr/bin/env bash
# Proves the NetworkPolicies (design §9.3): throwaway pods with the right and wrong labels try every
# allowed and denied path; Traefik paths are checked from the host through the Ingress.
# Usage: scripts/netpol-test.sh [domain]   (default 127-0-0-1.nip.io). Exit code 1 if any row fails.
set -euo pipefail
export MSYS_NO_PATHCONV=1

DOMAIN="${1:-127-0-0-1.nip.io}"
SCHEME=$([[ "${URUMI_ENV:-local}" == prod ]] && echo https || echo http)
PLATFORM_NS=platform
STORE_NS=store-main
IMAGE=busybox:1.37
TEST_LABEL=urumi.io/netpol-test=true
pass=0
fail=0

cleanup() {
  kubectl -n "$PLATFORM_NS" delete pod -l "$TEST_LABEL" --wait=false >/dev/null 2>&1 || true
  kubectl -n "$STORE_NS" delete pod -l "$TEST_LABEL" --wait=false >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Test pods carry only app.kubernetes.io/name (what the policies match), never .../instance (what Services match),
# so they are treated like the real workload by the policies but never receive real traffic.
start_pod() {
  local ns=$1 name=$2 as=${3:-}
  local labels="$TEST_LABEL"
  [[ -n "$as" ]] && labels="$labels,app.kubernetes.io/name=$as"
  kubectl -n "$ns" run "$name" --image="$IMAGE" --labels="$labels" --restart=Never --command -- sleep 600 >/dev/null
}

result() {
  local expected=$1 got=$2 label=$3
  if [[ "$expected" == "$got" ]]; then pass=$((pass + 1)); status=PASS; else fail=$((fail + 1)); status=FAIL; fi
  printf '%-4s  %-58s expected %-7s got %s\n' "$status" "$label" "$expected" "$got"
}

# TCP connect from inside a pod: nc exits 0 only when the connection was accepted.
probe() {
  local ns=$1 pod=$2 host=$3 port=$4 expected=$5 got
  if kubectl -n "$ns" exec "$pod" -- sh -c "nc -w 3 $host $port </dev/null" >/dev/null 2>&1; then got=allowed; else got=denied; fi
  result "$expected" "$got" "$ns/$pod -> $host:$port"
}

# HTTP status through Traefik from the host: any answer from the backend proves the path is open.
via_ingress() {
  local url=$1 want_code=$2 label=$3 code
  code=$(curl -s -o /dev/null -m 10 -w '%{http_code}' "$url" || true)
  if [[ "$code" == "$want_code" ]]; then result allowed allowed "$label ($code)"; else result allowed "denied" "$label (HTTP $code)"; fi
}

echo "Removing leftovers from earlier runs..."
kubectl -n "$PLATFORM_NS" delete pod -l "$TEST_LABEL" --wait=true >/dev/null 2>&1 || true
kubectl -n "$STORE_NS" delete pod -l "$TEST_LABEL" --wait=true >/dev/null 2>&1 || true

echo "Starting test pods..."
start_pod "$PLATFORM_NS" np-gateway gateway
start_pod "$PLATFORM_NS" np-notes notes-mcp
start_pod "$PLATFORM_NS" np-random
start_pod "$STORE_NS" np-woo woo-mcp
start_pod "$STORE_NS" np-bootstrap store-bootstrap
start_pod "$STORE_NS" np-random
kubectl -n "$PLATFORM_NS" wait --for=condition=Ready pod -l "$TEST_LABEL" --timeout=180s >/dev/null
kubectl -n "$STORE_NS" wait --for=condition=Ready pod -l "$TEST_LABEL" --timeout=180s >/dev/null
# The policy controller needs a moment to program rules for new pods (build-log #5).
sleep 8
echo

echo "== Allowed paths"
probe "$PLATFORM_NS" np-gateway "woo-mcp.$STORE_NS" 8000 allowed
probe "$PLATFORM_NS" np-gateway notes-mcp 8000 allowed
probe "$PLATFORM_NS" np-gateway fx-mcp 8000 allowed
probe "$PLATFORM_NS" np-gateway postgres 5432 allowed
probe "$PLATFORM_NS" np-gateway api.groq.com 443 allowed
probe "$STORE_NS" np-woo wordpress 80 allowed
probe "$STORE_NS" np-bootstrap mysql 3306 allowed
via_ingress "$SCHEME://store.$DOMAIN/wp-login.php" 200 "Traefik -> wordpress"
via_ingress "$SCHEME://app.$DOMAIN/healthz" 200 "Traefik -> dashboard"
via_ingress "$SCHEME://app.$DOMAIN/api/auth/me" 401 "Traefik -> gateway (/api)"
via_ingress "$SCHEME://mcp.$DOMAIN/mcp" 401 "Traefik -> gateway (/mcp)"
echo

echo "== Denied paths"
probe "$PLATFORM_NS" np-random "woo-mcp.$STORE_NS" 8000 denied
probe "$STORE_NS" np-random woo-mcp 8000 denied
probe "$PLATFORM_NS" np-random notes-mcp 8000 denied
probe "$PLATFORM_NS" np-random fx-mcp 8000 denied
probe "$PLATFORM_NS" np-random gateway 8000 denied
probe "$PLATFORM_NS" np-random dashboard 8080 denied
probe "$PLATFORM_NS" np-random postgres 5432 denied
probe "$STORE_NS" np-random mysql 3306 denied
probe "$STORE_NS" np-random wordpress 80 denied
probe "$STORE_NS" np-woo mysql 3306 denied
probe "$STORE_NS" np-bootstrap wordpress 80 denied
probe "$PLATFORM_NS" np-notes postgres 5432 denied
probe "$PLATFORM_NS" np-notes "mysql.$STORE_NS" 3306 denied
probe "$PLATFORM_NS" np-gateway "mysql.$STORE_NS" 3306 denied
probe "$PLATFORM_NS" np-gateway "wordpress.$STORE_NS" 80 denied
probe "$PLATFORM_NS" np-gateway kubernetes.default 443 denied
probe "$PLATFORM_NS" np-gateway 169.254.169.254 80 denied
echo

echo "$pass passed, $fail failed"
[[ $fail -eq 0 ]]
