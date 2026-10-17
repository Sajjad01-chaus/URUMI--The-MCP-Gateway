# System design and trade-offs

A condensed version of the [full v1 design](design/v1-system-design.md), plus the decisions that came out of building and running it.

## 1. What the system does

One gateway in front of many MCP servers, for a team:

1. An admin registers upstream MCP servers (URL, optional static credential) in the dashboard.
2. Each member gets personal API keys and points any MCP client at one URL: `https://mcp.<domain>/mcp`.
3. `tools/list` returns the union of all enabled, healthy upstreams, filtered by the caller's role; `tools/call` is routed to the right upstream with the gateway's credential for it, under a timeout, and audited.
4. The dashboard chat agent runs an LLM tool loop through the same `/mcp` endpoint, acting as the logged-in user.

The first upstream is `woo-mcp`, our MCP server for a WooCommerce store running in the same cluster. Two small upstreams (`notes-mcp`, `fx-mcp`) exist to prove credential styles, name collisions and failure handling.

## 2. Request paths

**Client tool call:** client → Traefik → gateway `/mcp` → API key verified (SHA-256 lookup, revocation checked on every request) → `woo__list_orders` split into slug `woo` + tool `list_orders` → server enabled? caller's role allowed? → per-upstream semaphore → new MCP session to `woo-mcp` with the gateway's bearer token, 20 s timeout → result returned → audit row (user, key, server, tool, duration, outcome).

**Chat:** browser → `POST /api/chat` (session cookie) → gateway mints a 5-minute chat token for the user → opens an MCP session to its own `/mcp` with that token → LLM loop (max 6 iterations, 10 tool calls, 90 s, results truncated to 4 KB) → answer + list of tool calls. Every call is audited with `source=chat`.

**Store:** woo-mcp → WooCommerce REST API v3 on the in-cluster Service, with a scoped API key for a dedicated `shop_manager` user (`mcp-bot`), never the admin.

## 3. Key decisions

| Decision | Why | Trade-off |
|---|---|---|
| **k3d locally, k3s in production, same version (v1.35.5)** | Same distribution, Traefik and storage on both, so the difference is values only | Not tested on a multi-node cloud cluster |
| **Two Helm charts: `woo-store` (one per store namespace) and `platform`** | A store is a self-contained unit; isolation by namespace + NetworkPolicy; ready for multi-store | Two releases to install (wrapped in `up.sh` / `deploy-prod.sh`) |
| **woo-mcp talks to WooCommerce over its REST API, not WP-CLI** | Network-native, uses WooCommerce validation and hooks, scoped key for a low-privilege user, no exec rights into the WordPress pod | In-cluster call is plain HTTP; WordPress is told the original scheme was HTTPS so Basic auth with the key works (traffic limited by NetworkPolicy) |
| **Always prefix tool names: `<slug>__<tool>`** | Collision-proof by construction, stable names, the model sees which system a tool acts on | Longer names; 64-character limit enforced |
| **Tool list served from an in-memory catalog, refreshed by a 15 s health monitor** | `tools/list` never waits on a slow or dead upstream | Status can lag by up to ~30 s |
| **One short-lived upstream MCP session per call** | No stale sessions after an upstream restarts; simple | Extra `initialize` round trip (milliseconds in-cluster) |
| **Never retry `tools/call`** | Tools aren't guaranteed idempotent; a retried `create_product` could create a duplicate | After a timeout the error says the outcome is unknown, so the user or model must check |
| **Per-upstream semaphore (bulkhead) + per-call timeout** | One slow server can't starve the others (verified: woo stays at ~40 ms while notes hangs) | Calls beyond the limit wait |
| **Chat agent calls the gateway's own `/mcp`** | One code path for auth, permissions, routing and audit; "through the gateway" is provable | One loopback hop |
| **API keys hashed with SHA-256; passwords with argon2id** | Keys are 256-bit random, so a fast hash is safe and allows an indexed lookup on every request | — |
| **Upstream credentials encrypted with Fernet in Postgres, write-only in the API** | A database dump alone doesn't leak them; they never reach a browser or client | Key management (Kubernetes Secret) |
| **Tool permissions from MCP annotations + admin overrides** | `readOnlyHint` → member, everything else → admin; enforced at list **and** call | Annotations come from upstreams and are untrusted, hence overrides and admin-only registration |
| **SSRF guard + gateway egress NetworkPolicy** | Admin-entered URLs are checked at save and at every connection; the network blocks metadata, the Kubernetes API and databases even if the app check fails | Internal upstreams need an explicit host allowlist |
| **Own minimal StatefulSets for MySQL and Postgres (no Bitnami)** | Explainable YAML; Bitnami images moved behind a paid catalogue | We own tuning and backups |
| **Single gateway replica** | Stateful MCP sessions (for `list_changed`) and an in-memory catalog | No high availability |
| **Non-streaming chat** | Saves build time while still showing every tool call | No progress while a turn runs |
| **nip.io locally, DuckDNS + Let's Encrypt in production** | No hosts-file edits; free public TLS | Depends on public DNS |

## 4. Decisions made while building and operating it

| Problem found | Decision |
|---|---|
| Rolling updates briefly sent new connections to a pod that was already stopping (Service routing lags behind `rollout status`) | Every Service-backed Deployment has a `preStop` sleep (5 s), so a terminating pod keeps serving until routing has moved |
| A 250m CPU limit stretched short Python tool calls to seconds (cgroup throttling) | Small CPU requests, 1-core CPU limits for the MCP servers |
| Fresh k3d nodes re-downloaded MySQL/WordPress and hit Kubernetes' 10-minute progress deadline | `up.sh` preloads third-party images from the host Docker cache, single-platform, and verifies them inside the node before Helm runs |
| Images must reach a VM that can't see the laptop | GitHub Actions builds `linux/amd64` images to GHCR, tagged by commit SHA, so a deploy or rollback names exact code |
| Let's Encrypt rate limits punish failed attempts | Staging issuer first, then production; namespaces are default-deny, so a dedicated NetworkPolicy lets Traefik reach only cert-manager's HTTP-01 solver pods |
| Azure for Students allows only a few regions and sizes | Region from the subscription's policy list (Central India), x64 size with 8 GB (B2as_v2) |

## 5. Local vs production

| | Local | Production |
|---|---|---|
| Cluster | k3d on Docker Desktop | k3s on an Azure VM (2 vCPU, 8 GB) |
| Domain | `*.127-0-0-1.nip.io` | `*.urumi-sajjad.duckdns.org` |
| TLS | none | cert-manager + Let's Encrypt |
| Images | built locally, imported into k3d | GitHub Actions → GHCR |
| Secrets | generated on the laptop, gitignored | generated on the VM, `chmod 600`, never leave it |
| Charts | identical | identical |

## 6. Limits and next steps

- **Availability:** single node and single gateway replica; the VM is a single point of failure. Next: shared session store and catalog so the gateway can scale out.
- **Backups:** database data lives on the VM disk; backups are documented, not automated.
- **notes-mcp** keeps notes in an `emptyDir` (demo upstream, data resets on restart).
- **Upstream response size** is not yet capped (design §9.4 planned it).
- **Backlog:** chat streaming, persisted conversations, Prometheus metrics, multi-store provisioning (`helm install woo-store -n store-<id>` + auto-registration), default-deny egress for every workload.
