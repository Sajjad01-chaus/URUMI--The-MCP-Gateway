#!/usr/bin/env bash
# Creates one private, verified production restore set before an upgrade.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRETS="$ROOT/deploy/secrets/prod.secrets.yaml"
BACKUP_ROOT="${1:-${URUMI_BACKUP_ROOT:-$HOME/urumi-backups}}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FINAL="$BACKUP_ROOT/$STAMP"
WORK="$FINAL.incomplete"

step() { echo "=== $*"; }
die() { echo "error: $*" >&2; exit 1; }

umask 077
for tool in kubectl helm gzip sha256sum git; do
  command -v "$tool" >/dev/null || die "missing tool: $tool"
done
[[ -f "$SECRETS" ]] || die "missing $SECRETS"
[[ "$(stat -c %a "$SECRETS")" == 600 ]] || die "$SECRETS must be chmod 600"
[[ ! -e "$FINAL" && ! -e "$WORK" ]] || die "backup path already exists: $FINAL"

mkdir -p -m 700 "$BACKUP_ROOT"
mkdir -m 700 "$WORK"
trap 'echo "backup failed; incomplete files retained at $WORK" >&2' ERR

step "database dumps"
kubectl exec -n platform postgres-0 -- pg_dump -U gateway -d gateway | gzip -9 > "$WORK/postgres.sql.gz"
kubectl exec -n store-main mysql-0 -- sh -c \
  'exec mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction --routines --events wordpress' \
  | gzip -9 > "$WORK/mysql.sql.gz"

step "secrets and release state"
cp "$SECRETS" "$WORK/prod.secrets.yaml"
for release in platform store-main; do
  namespace="$release"
  helm history "$release" -n "$namespace" -o yaml > "$WORK/$release-history.yaml"
  helm get values "$release" -n "$namespace" -a -o yaml > "$WORK/$release-values.yaml"
  helm get manifest "$release" -n "$namespace" > "$WORK/$release-manifest.yaml"
done
kubectl get nodes -o wide > "$WORK/nodes.txt"
kubectl get pods -A -o wide > "$WORK/pods.txt"
git -C "$ROOT" rev-parse HEAD > "$WORK/repository-commit.txt"

step "verification"
gzip -t "$WORK/postgres.sql.gz"
gzip -t "$WORK/mysql.sql.gz"
[[ -s "$WORK/postgres.sql.gz" ]] || die "Postgres dump is empty"
[[ -s "$WORK/mysql.sql.gz" ]] || die "MySQL dump is empty"
(
  cd "$WORK"
  sha256sum -- * > SHA256SUMS
  sha256sum -c SHA256SUMS >/dev/null
)

mv "$WORK" "$FINAL"
trap - ERR
echo "Production backup verified: $FINAL"
