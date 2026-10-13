# woo-mcp — WooCommerce MCP server

An MCP server (Streamable HTTP, MCP Python SDK 2.3.0) that exposes one WooCommerce store's products and orders as tools. It runs inside the store's namespace and is **only reachable by the gateway**.

## Tools

| Tool | What it does | Annotation |
|---|---|---|
| `get_store_info` | Store name, URL, currency, timezone, current date/time in store time | read-only |
| `list_products` | Products (search, status, pagination) | read-only |
| `create_product` | Creates a simple product; `publish` shows it on the storefront | write |
| `list_orders` | Orders; `period=today/yesterday/last_7_days` computed in the **store's timezone** | read-only |
| `get_order` | One order with line items, totals, payment method | read-only |
| `update_order_status` | Changes status (+ optional private note); returns previous and new status | write, destructive |

Outputs are typed (Pydantic models → MCP `outputSchema`) and compact: no emails, phones or street addresses are returned.

## How it talks to the store, and why

woo-mcp calls the **WooCommerce REST API v3** (`/wp-json/wc/v3/...`) on the in-cluster Service `http://wordpress`.

**Why REST rather than WP-CLI:** WP-CLI would need either `pods/exec` permission into the WordPress pod or a shared filesystem with PHP, which widens privileges and ties the MCP server to WordPress internals. REST goes through WooCommerce's own validation, hooks, stock handling and permission checks, uses a **scoped API key** for a dedicated `mcp-bot` user (`shop_manager`, not admin), and works the same way for any number of stores.

**Authentication over in-cluster HTTP:** WooCommerce accepts Basic-auth API keys only when it believes the request is HTTPS. `wp-config.php` trusts `X-Forwarded-Proto: https` (needed behind TLS anyway), so woo-mcp sends that header plus `Host: <store host>`. The key travels unencrypted only on the cluster network, where NetworkPolicy allows just `woo-mcp → wordpress`. Because WordPress then builds `https://` links, woo-mcp rewrites links to `WOO_PUBLIC_URL`.

The API key itself lives in the K8s Secret `woo-api`; the store bootstrap Job registers it in WooCommerce (hashed with `wc_api_hash`), so Kubernetes is the source of truth.

## Security boundaries

| Layer | Enforcement |
|---|---|
| Network | ClusterIP only, no Ingress; NetworkPolicy allows only pods `app.kubernetes.io/name=gateway` in namespace `platform` on :8000 |
| Caller auth | Every `/mcp` request needs `Authorization: Bearer <woo-mcp-auth token>` (constant-time compare); only `/healthz` is open (kubelet probes) |
| Host header | DNS-rebinding protection: only the in-cluster service names (and localhost for port-forward) are accepted |
| Store access | Scoped `read_write` key of `mcp-bot` (`shop_manager`), never the WordPress admin |
| Container | non-root uid 10001, read-only root filesystem, all capabilities dropped, no service-account token |

## Configuration (env)

| Variable | Example | Source |
|---|---|---|
| `WOO_BASE_URL` | `http://wordpress` | Helm |
| `WOO_STORE_HOST` | `store.127-0-0-1.nip.io` | Helm (`global.domain`) |
| `WOO_PUBLIC_URL` | `http://store.127-0-0-1.nip.io` | Helm |
| `WOO_CONSUMER_KEY` / `WOO_CONSUMER_SECRET` | `ck_…` / `cs_…` | Secret `woo-api` |
| `MCP_AUTH_TOKEN` | random | Secret `woo-mcp-auth` |
| `ALLOWED_HOSTS` | `woo-mcp.store-main.svc.cluster.local:*,…` | Helm |

## Develop and test

```bash
uv sync
uv run pytest -q
docker build -t urumi/woo-mcp:dev .
k3d image import urumi/woo-mcp:dev -c urumi
```

Manual test through a tunnel (the service has no public URL):

```bash
kubectl -n store-main port-forward svc/woo-mcp 18000:8000
npx @modelcontextprotocol/inspector --cli http://127.0.0.1:18000/mcp --transport http --header "Authorization: Bearer <token>" --method tools/list
```
