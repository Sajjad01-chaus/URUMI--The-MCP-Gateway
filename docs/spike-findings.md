# Phase 0.5 — MCP SDK spike findings

Date: 2026-10-13. SDK: `mcp` **2.3.0** (Python). Files: `spikes/upstream_server.py`, `client.py`, `gateway.py`, `gateway_client.py`.

## What was proven

| Test | Result |
|---|---|
| Upstream server (`MCPServer`, Streamable HTTP, stateless) | `initialize`, `tools/list`, `tools/call` work; bad input and unknown tools return `isError: true` |
| Python client (`mcp.Client`) | lists and calls tools |
| Mini gateway (low-level `Server` + `Client` inside) | re-exposes `add`/`echo` as `demo__add`/`demo__echo`, strips the prefix and forwards calls |
| API-key middleware | wrong or missing key → `401`, request never reaches MCP |
| MCP Inspector CLI (third-party client) → gateway | `tools/list` and `tools/call demo__add` work with an `Authorization` header |
| "Tools changed" push, 2026-07-28 protocol | client using `client.listen(tools_list_changed=True)` receives `ToolsListChanged` |
| "Tools changed" push, 2025-11-25 session protocol | client receives `notifications/tools/list_changed` (after the fix in build-log #8) |

## SDK v2 facts the real code relies on

1. **FastMCP is now `MCPServer`**: `from mcp.server.mcpserver import MCPServer`. Host/port go to `run(transport="streamable-http", host=..., port=...)`, not the constructor. v1 tutorials do not work.
2. **Two protocol generations are live at once.**
   - Session style (`2024-11-05` … `2025-11-25`): `initialize` handshake, `Mcp-Session-Id` header, standing `GET /mcp` stream for notifications.
   - Stateless style (`2026-07-28`): `server/discover`, protocol version + client info in each request's `_meta`, notifications only via a `subscriptions/listen` request.
   - The Python `Client` uses the new style by default (`mode="auto"`); `mode="legacy"` forces the old one. Third-party clients may use either. The SDK server answers both.
3. **Low-level `Server` takes handlers in the constructor**: `Server(name, on_list_tools=..., on_call_tool=..., on_subscriptions_listen=...)`. Handlers are `async def handler(ctx, params)`.
4. **Advertising `listChanged`**: the HTTP runner calls `create_initialization_options()` with no arguments, so subclass `Server` and default `NotificationOptions(tools_changed=True)`.
5. **Pushing changes needs both paths**:
   - new protocol: `ListenHandler(InMemorySubscriptionBus())` as `on_subscriptions_listen`, then `await bus.publish(ToolsListChanged())`;
   - old protocol: keep the sessions seen in handlers (`ctx.session`) with **strong** references and `await session.send_tool_list_changed()`.
6. **`GET /mcp` is a long-lived stream** — never use it as a health check. MCP servers get a separate `/healthz`.
7. **Custom HTTP headers on the client**: `Client(streamable_http_client(url, http_client=httpx2.AsyncClient(headers=...)))`. The SDK ships its own `httpx2` package.
8. **DNS-rebinding protection** is auto-enabled when `host` is `127.0.0.1`/`localhost` and only allows `Host: 127.0.0.1:*`/`localhost:*`. Behind Traefik (`Host: mcp.<domain>`) the real gateway must pass explicit `transport_security` allowed hosts.
9. Result fields are snake_case in Python (`result.is_error`, `result.structured_content`) and camelCase on the wire.

## Design changes for the real gateway

| Finding | Change |
|---|---|
| Inspector reacts to `401` by starting an **OAuth** login | Gateway 401 response must clearly say an API key header is required (`WWW-Authenticate: Bearer` with an error description); document the client config with the header. |
| Forwarded results carry the upstream's `_meta.serverInfo` (`"demo-upstream"`) | Router strips/replaces `_meta` from upstream results so clients only see the gateway. |
| Session set grows forever | Track sessions by `Mcp-Session-Id`; remove on `DELETE`, idle timeout or failed send. |
| Two notification paths | Gateway's session hub publishes to the subscription bus **and** to tracked legacy sessions. |

## Still to test (with the real gateway, Phase 3)

- **Claude Code** as a client (the `claude` CLI isn't installed on this laptop yet) — confirm it connects with a header key and refreshes on `list_changed`.
- Cursor (optional).
