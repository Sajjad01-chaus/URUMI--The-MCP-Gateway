# Demo script

The definition of done (design §15), as a recording plan for the demo video. Target length: 8–10 minutes. Run it against production (`urumi-sajjad.duckdns.org`); every step is also automated in `scripts/acceptance.sh`.

**Users:** alice@urumi.test (admin, "User A"), bob@urumi.test (member, "User B"). For a clean take, invite fresh users (e.g. `alice2@`, `bob2@`) or reuse these.

**Windows to have open:** browser (store, dashboard as alice, dashboard as bob in a private window), a terminal with Claude Code, an SSH session to the VM.

| # | Show | Say (one line) |
|---|---|---|
| 0 | Architecture diagram (README) | One gateway in front of many MCP servers; nothing but the gateway talks to upstreams. |
| 1 | `https://store.<d>`: add a product to the cart, checkout with **Cash on Delivery**; the order in WP admin | WooCommerce runs in the cluster with persistent MySQL and WordPress volumes. |
| 2 | Dashboard as alice → **Servers**: woo, notes, fx **Connected** with tool counts | The admin registered servers once; credentials are write-only and encrypted. |
| 3 | **Chat** (alice): start a conversation, "Create a product called Urumi Mug priced 12.50" → watch the completed-tool chip appear and the answer stream → refresh and resume the same conversation | Chat history is persisted in Postgres; the agent calls the gateway's own MCP endpoint as alice. |
| 4 | Buy Urumi Mug on the storefront | — |
| 5 | **Chat**: "What orders came in today?" → `woo__list_orders {period: today}` | "Today" is computed in the store's timezone by the server, not guessed by the LLM. |
| 6 | **Members**: invite bob (copy link) → private window: accept → **API keys**: create a key (shown once) | Keys are stored only as SHA-256 hashes. |
| 7 | Terminal: `claude mcp add --transport http urumi https://mcp.<d>/mcp --header "Authorization: Bearer mcpgw_…"` → `/mcp` shows `woo__*`, `notes__*`, `fx__*` → "What orders came in today?" | One URL + personal key in any MCP client; bob sees 11 tools, not the write tools. |
| 8 | Claude Code (bob): "Summarise today's orders, convert revenue to EUR and save it as a note" | woo → fx → notes, routed by the `<slug>__<tool>` prefix. |
| 9 | Dashboard (alice) → **API keys**: revoke bob's key → bob's next call fails (401) | Revocation is checked on every request. |
| 10 | **Servers**: disable fx → fx tools disappear in chat and in Claude Code (after re-list); a direct call is rejected | The gateway pushes `tools/list_changed`; enforcement is server-side. |
| 11 | SSH: `kubectl -n platform scale deploy notes-mcp --replicas=0` → Servers shows notes **Unreachable** with the reason; woo still works → scale back to 1 → Connected | One dead upstream doesn't affect the others. |
| 12 | SSH: `kubectl -n platform set env deploy/notes-mcp FAULT_DELAY_MS=30000` → a notes call fails with a clear timeout after 20 s while woo stays fast → `kubectl -n platform set env deploy/notes-mcp FAULT_DELAY_MS-` | Timeouts and bulkheads; no retries because tools aren't idempotent. |
| 13 | Bob: `woo__update_order_status` is not listed; calling it by name → denied | Tool permissions apply at list and call time. |
| 14 | SSH: `URUMI_ENV=prod bash scripts/netpol-test.sh urumi-sajjad.duckdns.org` → 28 passed | NetworkPolicies are tested, not just declared. |
| 15 | **Audit log**: every call above with user, server, tool, duration, outcome, source | Every call is attributed to a person. |

**Before recording:** VM started, `kubectl get pods -A` all Running, servers Connected, a fresh browser profile without saved logins, notifications off.
