# Threat model

Scope: the gateway, its dashboard, the upstream MCP servers and the WooCommerce store as deployed by the two Helm charts (local k3d and production k3s on Azure).

## 1. What we protect

| Asset | Where it lives |
|---|---|
| Upstream credentials (woo-mcp token, notes token, fx API key, WooCommerce consumer key/secret) | Postgres (Fernet-encrypted) and Kubernetes Secrets |
| Member API keys and passwords | Postgres, as SHA-256 hashes (keys) and argon2id hashes (passwords) |
| Store data: orders, customers, products | MySQL in `store-main` |
| The ability to act on the store (create products, change order status) | woo-mcp tools, reachable only through the gateway |
| Audit trail | Postgres `audit_log` |
| LLM API key | Kubernetes Secret, gateway only |

## 2. Actors and trust boundaries

| Actor | Trust |
|---|---|
| Admin | Trusted to register servers and manage members; can do everything a member can |
| Member | Trusted for read-only tools by default; must not see credentials, other members' keys or audit rows, or reach upstreams directly |
| MCP client on a member's machine | Holds one revocable API key; untrusted beyond what that key allows |
| Upstream MCP server | **Untrusted output**: tool descriptions and results may be malicious |
| LLM provider | Receives prompts and tool results; untrusted output |
| Internet | Untrusted; reaches only Traefik on 80/443 (and SSH from one IP in production) |

Boundaries: internet → Traefik; Traefik → gateway/dashboard/WordPress; gateway → upstreams; gateway → LLM; woo-mcp → WordPress → MySQL.

## 3. Threats and mitigations

| # | Threat | Mitigation in v1 | Verified by |
|---|---|---|---|
| T1 | **Stolen or leaked API key** | Keys are shown once and stored only as SHA-256; revocation is checked on **every** `/mcp` request; audit shows each call with key id and client name | Acceptance step 9 (revoke → next call 401) |
| T2 | **Token passthrough / confused deputy**: client tokens forwarded to upstreams | Client keys stop at the gateway; upstreams only ever see the gateway's own credential for them | Design rule; upstream `_meta` is stripped so upstream identity doesn't leak back either |
| T3 | **Member reaches an upstream directly** | Upstreams have no Ingress (ClusterIP only) and NetworkPolicies admit only the gateway; each upstream also checks its own bearer/header credential | netpol-test: random pod → woo-mcp, notes-mcp, fx-mcp **denied** |
| T4 | **Member uses write tools** | Per-tool minimum role from annotations (write tools admin-only) + admin overrides; enforced in `tools/list` (hidden) and `tools/call` (rejected, audited as `denied`) | Acceptance step 13 |
| T5 | **SSRF via "add MCP server"**: an admin URL pointing at cloud metadata, the Kubernetes API or internal databases | URL validation at save **and** at every connection (scheme, no userinfo, every resolved IP checked, private ranges only for allowlisted hostnames, no redirects); gateway egress NetworkPolicy blocks metadata, the API server and the databases as a network backstop | Phase 3 tests (6 URLs → 422); netpol-test: gateway → 169.254.169.254, Kubernetes API, MySQL **denied** |
| T6 | **Tool poisoning / prompt injection** via upstream tool descriptions or results | Only admins add servers; every exposed description is prefixed with its server name; the chat system prompt treats tool text as data; the chat UI shows every tool call with arguments; write tools need admin; tool results are truncated to 4 KB | Manual; residual risk below |
| T7 | **Upstream credentials leaked from the database or API** | Fernet-encrypted at rest with a key from a Kubernetes Secret; write-only in the REST API (`has_credential: true`, never the value); decrypted only in memory for a call | Phase 3 test: no credential in `/api/servers` |
| T8 | **Dashboard session theft / CSRF** | Signed session cookie, `HttpOnly`, `SameSite=Lax`, `Secure` in production; mutating requests need JSON plus an `X-Requested-With` header | Acceptance: cookie write without the header → 403 |
| T9 | **Password guessing** | argon2id hashes; per-email login throttle (429 after 5 failures) | Re-verification 2026-10-15 |
| T10 | **Slow or dead upstream used to exhaust the gateway** | Per-call timeout (20 s), per-upstream concurrency limit, health monitor hides unreachable servers, no retries | Acceptance steps 11–12 |
| T11 | **Lateral movement inside the cluster** | Default-deny ingress in both namespaces; explicit allow rules only (Traefik → apps, gateway → upstreams and Postgres, WordPress → MySQL, woo-mcp → WordPress); containers we build run as non-root with a read-only root filesystem and no capabilities | netpol-test 28/28 |
| T12 | **Secrets in git, logs or screenshots** | Secrets generated per environment and gitignored; production secrets generated on the VM with `chmod 600`; logs never contain passwords, tokens or full keys | Log scan for all secret values (none found); git history scanned before publishing |
| T13 | **Exposed VM** | Only 80/443 public; SSH key-only and restricted to one source IP; Kubernetes API (6443) not exposed | Azure network security group |

## 4. Residual risks (accepted in v1)

1. **Shared upstream identity.** Every member acts on the store through the same WooCommerce user (`mcp-bot`). A member with write access could cancel an order someone else created. Mitigations: write tools admin-only by default, scoped key, audit attributes every call to a person.
2. **Malicious upstreams.** Annotations come from the upstream and can lie (a destructive tool marked read-only). Admin overrides exist, but a careless admin can still register a hostile server. v2: description length limits and scanning, human approval for destructive tools.
3. **Admin compromise** gives full control. Mitigate operationally: few admins, strong passwords. v2: audit of admin actions, SSO/MFA.
4. **No per-user rate limits** on tool calls (only the login throttle and per-upstream concurrency).
5. **Upstream response size is capped after decoding**: results over `gateway.maxResultBytes` (256 KiB) are replaced by a tool error before they reach a client, the model or the audit log. The gateway still reads the whole response into memory first, bounded only by the call timeout and the pod's memory limit.
6. **Plain HTTP inside the cluster** (gateway → upstreams, woo-mcp → WordPress). Limited by NetworkPolicies; v2: mTLS or a service mesh.
7. **WordPress and MySQL official images start as root** (they drop privileges internally); documented exception.
8. **Single node, no automated backups**: availability and data loss risk, not confidentiality.
