# Production setup — k3s on an Azure VM (Phase 8)

This guide takes the system that runs locally on k3d and runs it on a public Azure VM with HTTPS, using the **same Helm charts**. Only `values-prod.yaml`, the secrets file and the image location change (design §12).

**Done when:** the acceptance script (design §15) passes against `https://app.<domain>` / `https://mcp.<domain>/mcp`.

---

## 0. Big picture

```text
 your laptop                       GitHub                             Azure VM (Ubuntu 24.04, k3s)
 ───────────                       ──────                             ──────────────────────────────
 git push ───────────────────────► repo ──► GitHub Actions             Traefik :80/:443  ◄── internet
                                            builds 5 images ──► GHCR ──► k3s pulls images
 ssh ────────────────────────────────────────────────────────────────► helm upgrade --install (2 charts)
                                                                       cert-manager ──► Let's Encrypt
 DuckDNS: urumi-sajjad.duckdns.org (+ every subdomain) ──► VM public IP
```

| | Local | Production |
|---|---|---|
| Kubernetes | k3d (k3s inside Docker) | k3s installed directly on the VM |
| Images | built on the laptop, `k3d image import` | built by GitHub Actions, pushed to GHCR, pulled by k3s |
| Domain | `*.127-0-0-1.nip.io` → 127.0.0.1 | `*.<name>.duckdns.org` → VM public IP |
| HTTPS | none | cert-manager + Let's Encrypt (HTTP-01) |
| Secrets | `deploy/secrets/local.secrets.yaml` | `deploy/secrets/prod.secrets.yaml`, generated **on the VM**, `chmod 600` |
| Charts | same | same |

Items marked **[to add]** are repo files that are created in this phase.

Rough time: 3–4 hours, most of it waiting for installs and image builds.

---

## Part 1 — Code on GitHub

### 1.1 Commit the Phase 7 work
See the chat steps: delete `AGENTS.md`, untrack the personal docs, `git add` + `git commit`.

### 1.2 Create the GitHub repo and push
1. github.com → **New repository** → name `urumi-mcp-gateway`, **Public** (reviewers need to read it; GHCR images from a public repo are easiest to pull), **no** README/.gitignore (we already have them).
2. On the laptop:
   ```bash
   git remote add origin https://github.com/<github-user>/urumi-mcp-gateway.git
   git push -u origin main
   ```
3. **Check:** the repo page shows your files and **no** `deploy/secrets/*.yaml` except `secrets.example.yaml`.

Why public is safe: secrets are gitignored and generated per environment; the repo only has `secrets.example.yaml` with placeholders.

---

## Part 2 — Images on GHCR

### 2.1 [to add] `.github/workflows/images.yml`
On every push to `main` it builds the 5 images (`gateway`, `dashboard`, `woo-mcp`, `notes-mcp`, `fx-mcp`) for `linux/amd64` and pushes them as
`ghcr.io/<github-user>/urumi-<service>:<git-sha>` and `:latest`, using the built-in `GITHUB_TOKEN` (no personal token needed).

### 2.2 Run it
1. Push the workflow → GitHub → **Actions** tab → watch "images" go green (first run ~10 min).
2. GitHub → your profile → **Packages**: 5 packages appear, **private** by default.
3. For each package: **Package settings → Change visibility → Public**. Then k3s can pull without a password.
4. **Check** from the laptop:
   ```bash
   docker pull ghcr.io/<github-user>/urumi-gateway:latest
   ```

Why the git SHA as tag: every deploy names an exact, immutable build, so `helm rollback` returns to exactly the previous code. `latest` is only a convenience.

---

## Part 3 — The Azure VM

### 3.1 SSH key (laptop, once)
```bash
ssh-keygen -t ed25519 -C "urumi-azure" -f ~/.ssh/urumi_azure
```
Press Enter for an empty passphrase, or set one. This creates `~/.ssh/urumi_azure` (private: never share) and `~/.ssh/urumi_azure.pub` (public: goes to Azure).

### 3.2 Create the VM (portal.azure.com → Virtual machines → Create)
| Field | Value | Why |
|---|---|---|
| Subscription | Azure for Students | your credit |
| Resource group | new: `urumi-rg` | delete this one group later to remove everything |
| VM name | `urumi-prod` | |
| Region | **Central India** (must be on your subscription's allowed list, see note below) | latency; Azure for Students only allows a few regions |
| Image | **Ubuntu Server 24.04 LTS – x64 Gen2** | design §1; matches our `linux/amd64` images |
| Availability options | **No infrastructure redundancy required** | one VM; a pinned zone can run out of capacity |
| Size | **Standard_B2as_v2** (2 vCPU, 8 GiB, x64, ~$0.049/h) | the stack needs ~4 GB limits; 4 GB VMs are too tight. Alternatives: B2s_v2, B2ms. **Never a `p` size (B2ps_v2 = Arm)**: our images are amd64 |
| Authentication | **SSH public key**, user `azureuser`, "Use existing public key" → paste the content of `urumi_azure.pub` | no password login at all |
| Inbound ports | SSH (22), HTTP (80), HTTPS (443) | 22 is narrowed to your IP in 3.3 |
| OS disk | 64 GB **Standard SSD** | images + MySQL/WordPress/Postgres volumes; HDD makes MySQL slow |
| Networking → Public IP | new (default: Standard SKU, static) | a dynamic IP changes after a stop, which would break DNS and HTTPS |
| Management → Auto-shutdown | optional, e.g. 23:30 your time | saves credit; the static IP survives |

**Review + create** → wait for "deployment complete" → note the **public IP**.

**Student subscription traps (seen 2026-10-16):** `NotAvailableForSubscription` on a size means that size is blocked *in the selected region*; select the region first, then the size. `RequestDisallowedByAzure … best available regions` means the region is not on your subscription's list: portal → **Policy → Assignments → "Allowed resource deployment regions" → Parameters** (ours: uaenorth, eastasia, koreacentral, malaysiawest, centralindia). The size picker does not check this policy. A "Deployment recommendations" pop-up suggesting another region is only a capacity hint; Central India deployed fine at half the price of the suggestion.

### 3.3 Firewall (Network security group)
VM → **Networking → Network settings** → edit the rules:
| Port | Source | Why |
|---|---|---|
| 22 | **My IP address** (not Any) | only you can SSH in |
| 80 | Any | Let's Encrypt HTTP-01 challenge + redirect |
| 443 | Any | the real HTTPS traffic |

**Do not** open 6443 (Kubernetes API): we run `kubectl`/`helm` on the VM over SSH.

Home/mobile IPs change (ours changed overnight): if `ssh` says **Connection timed out**, open the SSH rule, switch Source to Any and back to **My IP address** (refreshes the IP), Save.

### 3.4 Log in
```bash
ssh -i $HOME\.ssh\urumi_azure azureuser@<public-ip>   # PowerShell on the laptop
```
First time it asks to trust the host key: type `yes`. **Check:** prompt `azureuser@urumi-prod:~$`.

Then update the OS:
```bash
sudo apt update && sudo NEEDRESTART_MODE=a apt-get -y full-upgrade && sudo reboot
```

---

## Part 4 — DNS (DuckDNS)

1. duckdns.org → sign in → add domain `urumi-sajjad` (or your choice) → set **current ip** to the VM's public IP → **update ip**.
2. DuckDNS answers for every subdomain too, so `store.`, `app.` and `mcp.urumi-sajjad.duckdns.org` all point to the VM.
3. **Check** from the laptop:
   ```bash
   Resolve-DnsName app.urumi-sajjad.duckdns.org -Type A   # PowerShell
   ```
   must show the VM's IP.

---

## Part 5 — k3s on the VM

```bash
curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION="v1.35.5+k3s1" sh -
```
Pinned to the same version as the local k3d node (`deploy/k3d/cluster.yaml`). What it does: verifies the download (sha256), installs k3s as a systemd service, with Traefik (ingress on host ports 80/443), CoreDNS, local-path storage, metrics-server and the built-in NetworkPolicy controller: the same components as k3d locally.

Let your user run `kubectl` without sudo (copying the file is safer than making it world-readable):
```bash
mkdir -p ~/.kube && sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config && sudo chown $USER: ~/.kube/config && chmod 600 ~/.kube/config
echo 'export KUBECONFIG=$HOME/.kube/config' >> ~/.bashrc && source ~/.bashrc
```
The `KUBECONFIG` line is required: k3s's built-in `kubectl` otherwise reads the root-only `/etc/rancher/k3s/k3s.yaml` ("permission denied").
Check:
```bash
kubectl get nodes -o wide
kubectl -n kube-system get pods
```
Good: node `Ready`, `v1.35.5+k3s1`; coredns, traefik, local-path-provisioner, metrics-server `Running`; `helm-install-traefik*` `Completed`.

Install Helm, same version as the laptop (v4.3.0), checksum-verified:
```bash
cd /tmp && curl -fsSLO https://get.helm.sh/helm-v4.3.0-linux-amd64.tar.gz && curl -fsSLO https://get.helm.sh/helm-v4.3.0-linux-amd64.tar.gz.sha256sum && sha256sum -c helm-v4.3.0-linux-amd64.tar.gz.sha256sum && tar -xzf helm-v4.3.0-linux-amd64.tar.gz && sudo install -m 0755 linux-amd64/helm /usr/local/bin/helm && cd ~ && helm version
```
`helm list -A` already shows `traefik` and `traefik-crd`: k3s installs Traefik as a Helm chart itself.

Also install the tools for secrets and the acceptance run:
```bash
sudo apt -y install git python3 && curl -LsSf https://astral.sh/uv/install.sh | sh
```

---

## Part 6 — HTTPS with cert-manager

```bash
helm repo add jetstack https://charts.jetstack.io --force-update && helm repo update
helm upgrade --install cert-manager jetstack/cert-manager -n cert-manager --create-namespace \
  --version v1.21.2 --set crds.enabled=true --wait --timeout 5m
kubectl -n cert-manager get pods   # cert-manager, -cainjector, -webhook Running
```
**[to add]** `deploy/prod/cluster-issuers.yaml`: two ClusterIssuers, `letsencrypt-staging` and `letsencrypt-prod`, HTTP-01 solver with ingress class `traefik`. Apply with `kubectl apply -f`.

How HTTP-01 works: cert-manager sees our Ingress annotation, asks Let's Encrypt for a certificate, and Let's Encrypt fetches a token at `http://<host>/.well-known/acme-challenge/...`. cert-manager answers through a short-lived **solver pod** in the Ingress's namespace.

**Gotcha:** our namespaces are default-deny ingress (design §9.3), so Traefik could not reach the solver pod and the certificate would never be issued. **[to add]** a NetworkPolicy in both charts that lets Traefik reach pods labelled `acme.cert-manager.io/http01-solver: "true"` on :8089, and nothing else. `netpol-test.sh` gets the matching rows.

Use **staging first**: Let's Encrypt production allows only a few duplicate certificates per week. Repeated failed attempts can lock us out for days, staging cannot.

---

## Part 7 — Deploy the charts

```bash
git clone https://github.com/Sajjad01-chaus/URUMI--The-MCP-Gateway.git ~/urumi && cd ~/urumi
python3 scripts/gen_secrets.py prod && chmod 600 deploy/secrets/prod.secrets.yaml
```
Secrets are generated **on the VM** and never leave it (design §10.3). **Never regenerate them (`--force`) once deployed**: MySQL and Postgres keep the first passwords.

Insert the Groq key without showing any secret on screen (do not open the file in an editor and never screenshot it). The prompt is silent and rejects anything that isn't a Groq key:
```bash
python3 - <<'EOF'
import getpass, re, pathlib
p = pathlib.Path("deploy/secrets/prod.secrets.yaml")
key = re.sub(r"\x1b\[[0-9;]*~", "", getpass.getpass("Paste Groq key, then Enter: ")).strip()
if not re.fullmatch(r"gsk_[A-Za-z0-9]{20,}", key):
    raise SystemExit(f"not a Groq key (length {len(key)}); file unchanged")
text, n = re.subn(r'(?m)^  groqApiKey: ".*"$', f'  groqApiKey: "{key}"', p.read_text())
if n != 1:
    raise SystemExit("groqApiKey line not found; file unchanged")
p.write_text(text)
print(f"groqApiKey set (length {len(key)})")
EOF
```

`scripts/deploy-prod.sh <image-sha> [--issuer …]` checks the prerequisites (secrets file `600` with the key set, domain set, cert-manager present, all 5 GHCR images pullable), applies the two ClusterIssuers (`deploy/prod/cluster-issuers.yaml`), then runs both `helm upgrade --install` with `values-prod.yaml`, the prod secrets and the GHCR images. `<image-sha>` is the commit the **images** workflow built (Actions → images → run), not necessarily the latest commit.

First deploy with the staging issuer:
```bash
bash scripts/deploy-prod.sh <image-sha> --issuer letsencrypt-staging
kubectl get certificate -A
```
Good: 2 certificates `READY True` within ~2 min (the browser still warns: staging certs are intentionally untrusted). Then the real one (same command, default issuer `letsencrypt-prod`); cert-manager re-issues by itself because the issuer changed:
```bash
bash scripts/deploy-prod.sh <image-sha>
kubectl get secret -n platform platform-tls -o jsonpath='{.metadata.annotations.cert-manager\.io/issuer-name}{"\n"}'   # letsencrypt-prod
```
Don't delete the TLS secrets without a reason: each fresh certificate counts toward Let's Encrypt's 5-duplicates-per-week limit (renewals don't).

**Check:** `https://store.<domain>`, `https://app.<domain>` open with a padlock; `https://mcp.<domain>/mcp` without a key → 401.

---

## Part 8 — Verify (the Phase 8 gate)

On the VM (needs `uv`: `curl -LsSf https://astral.sh/uv/install.sh | sh && source ~/.local/bin/env`):
```bash
cd ~/urumi && bash scripts/acceptance.sh urumi-sajjad.duckdns.org --prod
```
`--prod` = HTTPS + `deploy/secrets/prod.*`. It runs the §15 demo script (59 checks), then `netpol-test.sh` (28 paths). On a flaky connection run the isolation part detached so an SSH drop can't kill it:
```bash
URUMI_ENV=prod nohup bash scripts/netpol-test.sh urumi-sajjad.duckdns.org > ~/netpol.log 2>&1 &
tail -f ~/netpol.log
```
Result on 2026-10-17: **59/59** and **28/28**.

Plus by hand, from the laptop: storefront purchase in the browser, dashboard login, and Claude Code as bob:
```bash
claude mcp add --transport http urumi-prod https://mcp.urumi-sajjad.duckdns.org/mcp --header "Authorization: Bearer mcpgw_..."
```

---

## Day-2 operations

| Task | How |
|---|---|
| Deploy new code | push → Actions builds `:<sha>` → on the VM: `git pull && bash scripts/deploy-prod.sh <sha>` |
| Roll back | `helm history platform -n platform` → `helm rollback platform <rev> -n platform` (PVC data untouched) |
| Save credit | Azure portal → VM → **Stop** (deallocates; static IP + disk kept, small disk cost). Start again → k3s and all pods come back by themselves |
| Logs | `kubectl -n platform logs deploy/gateway -c gateway --tail=100` |
| Backup | `kubectl -n store-main exec mysql-0 -- mysqldump …` and `pg_dump` from `postgres-0` to a file, `scp` it home (single-node `local-path` storage = the VM disk is the only copy) |
| Remove everything | Azure → delete resource group `urumi-rg` |

## Troubleshooting

| Symptom | Likely cause | Check |
|---|---|---|
| `ssh: connection timed out` | NSG port 22 not allowed from your current IP (it changes with Wi-Fi) | update the rule source to "My IP address" |
| Pod `ImagePullBackOff` | GHCR package still private, or wrong image name/tag | `kubectl describe pod …` → Events |
| Certificate stays `READY False` | DNS not pointing at the VM, port 80 closed, or solver pod blocked | `kubectl describe challenge -A` |
| WordPress redirects to `http://` or the wrong host | `WP_HOME` from old values | `helm upgrade` again; the bootstrap Job rewrites home/siteurl |
| Pods `Pending` | out of memory/disk | `kubectl describe pod`, `kubectl top nodes`, `df -h` |
