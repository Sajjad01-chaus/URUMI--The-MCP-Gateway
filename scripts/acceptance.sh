#!/usr/bin/env bash
# Phase 7 acceptance: the §15 demo script (scripts/acceptance.py) + step 14 isolation (scripts/netpol-test.sh).
# Usage: scripts/acceptance.sh [domain]
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOMAIN="${1:-127-0-0-1.nip.io}"
export PYTHONIOENCODING=utf-8

uv run --quiet --frozen --project "$ROOT/services/gateway" python "$ROOT/scripts/acceptance.py" "$DOMAIN"
demo=$?
echo
echo "=== 14 isolation: scripts/netpol-test.sh"
"$ROOT/scripts/netpol-test.sh" "$DOMAIN"
netpol=$?

echo
echo "demo script: $([[ $demo -eq 0 ]] && echo PASS || echo FAIL)   isolation: $([[ $netpol -eq 0 ]] && echo PASS || echo FAIL)"
[[ $demo -eq 0 && $netpol -eq 0 ]]
