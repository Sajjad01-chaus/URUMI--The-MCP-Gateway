# Upgrades and rollback

Urumi deploys immutable image tags with Helm. The image tag is injected as `APP_VERSION`, returned by each `/healthz`, reported by MCP `serverInfo`, and shown in the dashboard. A visible version makes it possible to prove what is serving traffic before and after a change.

## Safety rules

1. Use a commit-SHA image tag; never deploy `latest`.
2. Before a production change, run `scripts/backup-prod.sh`. It saves and verifies Postgres/MySQL dumps, `prod.secrets.yaml`, both Helm histories/values/manifests, cluster state and the repository commit in a private timestamped directory.
3. Keep database migrations expand/contract and backward compatible for one release. Rollback changes containers and Kubernetes objects; it does not reverse a destructive database migration.
4. Run `helm diff` when available, then use `--wait --rollback-on-failure --timeout 5m` for upgrades.
5. Verify `/healthz`, the dashboard version, tool calls, `helm history`, pod readiness and error logs before declaring success.
6. Never use `--force` for the generated secrets file. Persistent databases retain their original passwords.

## Production upgrade

Builds publish all images under the same Git commit SHA. On the VM, pull the new repository commit and run:

```bash
bash scripts/deploy-prod.sh <commit-sha>
```

When existing releases are present, the deploy script requires a verified backup before checking images or changing the cluster. It then checks every image and uses Helm's automatic rollback on failure for both releases. Keep a tool-call loop running during the platform rollout to prove availability.

Backups default to `~/urumi-backups/<UTC timestamp>` on the VM. To choose a different private filesystem, set `URUMI_BACKUP_ROOT` or pass a directory directly:

```bash
bash scripts/backup-prod.sh /mnt/private/urumi-backups
```

Each completed directory contains `SHA256SUMS`; a failed run retains an `.incomplete` directory and blocks deployment.

## Automatic rollback on a failed upgrade

Helm 4 uses `--rollback-on-failure` (Helm 3 called the combined behavior `--atomic`):

```bash
helm upgrade platform deploy/charts/platform -n platform \
  -f deploy/values/platform/values-prod.yaml \
  -f deploy/secrets/prod.secrets.yaml \
  --set-string gateway.image=ghcr.io/sajjad01-chaus/urumi-gateway:<commit-sha> \
  --rollback-on-failure --timeout 5m
```

If a new pod never becomes Ready, Helm restores the last successful manifest. With a one-replica RollingUpdate, Kubernetes keeps the old Ready pod until the replacement is ready.

## Manual rollback

```bash
helm history platform -n platform
helm rollback platform <known-good-revision> -n platform --wait --timeout 5m
helm history store-main -n store-main
helm rollback store-main <known-good-revision> -n store-main --wait --timeout 5m
```

After rollback, confirm the displayed versions, call one tool from every upstream, inspect the audit log and run the acceptance and NetworkPolicy suites. PVC data is not removed by rollback.

## Rotate the credential-encryption key

The chart injects both `CREDENTIAL_ENCRYPTION_KEYS` and the one-release compatibility variable `CREDENTIAL_ENCRYPTION_KEY`. The Secret value may contain a comma-separated keyring; the first key encrypts and every listed key can decrypt.

1. Back up Postgres and the current secrets file.
2. Generate a Fernet key without printing it into shell history.
3. Set `secrets.credentialEncryptionKey` to `new_key,old_key` and upgrade the platform. Verify all upstreams reconnect.
4. Run this once inside the new gateway pod:

   ```bash
   python -m gateway.cli rotate-credential-key
   ```

   The command re-encrypts every upstream credential and managed-release secret in one database transaction and reports counts only.
5. Set the Secret value to `new_key` only, upgrade again, and verify every upstream plus managed-store retry/deletion paths.
6. Store the old key with the pre-rotation backup until the release is accepted, then retire it. A Helm rollback after step 5 uses the new key; rollback does not restore old ciphertext.

If the command reports that a value cannot be rotated, the transaction rolls back completely. Restore the database/Secret pair from the same backup generation before retrying.

## Repeatable local demonstration

`scripts/phase10_5_gate.py` creates two local tags for the same tested gateway image and demonstrates:

- a rolling `phase10.5-a` → `phase10.5-b` upgrade with continuous successful calls;
- a missing Notes image rejected by readiness and automatically rolled back while Notes calls continue;
- a manual rollback to `phase10.5-a`, followed by restoration of the desired `phase10.5-b` revision;
- visible API/dashboard versions and clean revocation of its temporary key.

Run it only against the local k3d cluster:

```powershell
services\gateway\.venv\Scripts\python.exe -u scripts\phase10_5_gate.py
```
