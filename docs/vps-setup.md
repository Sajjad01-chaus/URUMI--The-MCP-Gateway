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
| Region | one close to you that your student subscription allows (e.g. Central India) | latency; student subscriptions block some regions |
| Image | **Ubuntu Server 24.04 LTS – x64 Gen2** | design §1; matches our `linux/amd64` images |
| Size | **Standard_B2ms** (2 vCPU, 8 GB) | the stack needs ~4 GB limits; 4 GB VMs are too tight. If "not available": try another region, or `Standard_B2as_v2` (also 2 vCPU / 8 GB) |
| Authentication | **SSH public key**, user `azureuser`, "Use existing public key" → paste the content of `urumi_azure.pub` | no password login at all |
| Inbound ports | SSH (22) for now | 80/443 are added in 3.3 |
| OS disk | 64 GB **Standard SSD** | images + MySQL/WordPress/Postgres volumes; HDD makes MySQL slow |
| Networking → Public IP | create new, **SKU Standard, assignment Static** | a dynamic IP changes after a stop, which would break DNS and HTTPS |
| Management → Auto-shutdown | optional, e.g. 23:30 your time | saves credit; the static IP survives |

**Review + create** → wait for "deployment complete" → note the **public IP**.

### 3.3 Firewall (Network security group)
VM → **Networking → Add inbound port rule**:
| Port | Source | Why |
|---|---|---|
| 22 | **My IP address** (not Any) | only you can SSH in |
| 80 | Any | Let's Encrypt HTTP-01 challenge + redirect |
| 443 | Any | the real HTTPS traffic |

**Do not** open 6443 (Kubernetes API): we run `kubectl`/`helm` on the VM over SSH.

### 3.4 Log in
```bash
ssh -i ~/.ssh/urumi_azure azureuser@<public-ip>
```
First time it asks to trust the host key: type `yes`. **Check:** prompt `azureuser@urumi-prod:~$`.

Then update the OS:
```bash
sudo apt update && sudo apt -y upgrade && sudo reboot
```

---

## Part 4 — DNS (DuckDNS)

1. duckdns.org → sign in → add domain `urumi-sajjad` (or your choice) → set **current ip** to the VM's public IP → **update ip**.
2. DuckDNS answers for every subdomain too, so `store.`, `app.` and `mcp.urumi-sajjad.duckdns.org` all point to the VM.
3. **Check** from the laptop:
   ```bash
   nslookup app.urumi-sajjad.duckdns.org
   ```
   must show the VM's IP.

---

## Part 5 — k3s on the VM

```bash
curl -sfL https://get.k3s.io | sh -
```
What it does: installs k3s as a systemd service, with Traefik (ingress on host ports 80/443), CoreDNS, local-path storage, metrics-server and the built-in NetworkPolicy controller: the same components as k3d locally.

Let your user run `kubectl` without sudo (copying the file is safer than making it world-readable):
```bash
mkdir -p ~/.kube && sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config && sudo chown $USER: ~/.kube/config && chmod 600 ~/.kube/config
```
Check:
```bash
kubectl get nodes
kubectl -n kube-system get pods
```
Good: node `Ready`; coredns, traefik, local-path-provisioner, metrics-server `Running`; `helm-install-traefik*` `Completed`.

Install Helm:
```bash
curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash && helm version
```

Also install the tools for secrets and the acceptance run:
```bash
sudo apt -y install git python3 && curl -LsSf https://astral.sh/uv/install.sh | sh
```

---

## Part 6 — HTTPS with cert-manager

```bash
helm repo add jetstack https://charts.jetstack.io && helm repo update
helm search repo jetstack/cert-manager
helm upgrade --install cert-manager jetstack/cert-manager -n cert-manager --create-namespace \
  --version <version shown above> --set crds.enabled=true --wait
```
**[to add]** `deploy/prod/cluster-issuers.yaml`: two ClusterIssuers, `letsencrypt-staging` and `letsencrypt-prod`, HTTP-01 solver with ingress class `traefik`. Apply with `kubectl apply -f`.

How HTTP-01 works: cert-manager sees our Ingress annotation, asks Let's Encrypt for a certificate, and Let's Encrypt fetches a token at `http://<host>/.well-known/acme-challenge/...`. cert-manager answers through a short-lived **solver pod** in the Ingress's namespace.

**Gotcha:** our namespaces are default-deny ingress (design §9.3), so Traefik could not reach the solver pod and the certificate would never be issued. **[to add]** a NetworkPolicy in both charts that lets Traefik reach pods labelled `acme.cert-manager.io/http01-solver: "true"` on :8089, and nothing else. `netpol-test.sh` gets the matching rows.

Use **staging first**: Let's Encrypt production allows only a few duplicate certificates per week. Repeated failed attempts can lock us out for days, staging cannot.

---

## Part 7 — Deploy the charts

```bash
git clone https://github.com/<github-user>/urumi-mcp-gateway.git && cd urumi-mcp-gateway
python3 scripts/gen_secrets.py prod
chmod 600 deploy/secrets/prod.secrets.yaml
nano deploy/secrets/prod.secrets.yaml   # replace REPLACE_WITH_YOUR_GROQ_KEY in groqApiKey, save
```
Secrets are generated **on the VM** and never leave it (design §10.3). Never regenerate them for a running cluster: MySQL and Postgres keep the old passwords.

**[to add]**
- `deploy/values/*/values-prod.yaml`: your DuckDNS domain, GHCR image names.
- `scripts/deploy-prod.sh <tag>`: both `helm upgrade --install` commands with `values-prod.yaml`, the prod secrets and the image tag, then waits and prints URLs. Same logic as `up.sh` minus k3d and image import.

First deploy with the staging issuer:
```bash
bash scripts/deploy-prod.sh <git-sha> --issuer letsencrypt-staging
kubectl get certificate -A
```
Good: 2 certificates `READY True` within ~2 min (the browser still warns: staging certs are intentionally untrusted). Then the real one:
```bash
bash scripts/deploy-prod.sh <git-sha>
```
**Check:** `https://store.<domain>`, `https://app.<domain>` open with a padlock; `https://mcp.<domain>/mcp` without a key → 401.

---

## Part 8 — Verify (the Phase 8 gate)

**[to add]** the acceptance script takes `--scheme https` and `--secrets deploy/secrets/prod.secrets.yaml` (it currently assumes `http://` and the local secrets file).

On the VM:
```bash
bash scripts/acceptance.sh urumi-sajjad.duckdns.org --scheme https
```
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
