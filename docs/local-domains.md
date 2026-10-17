# Local domains

Locally the three hosts are subdomains of **`127-0-0-1.nip.io`**:

| Host | Backend |
|---|---|
| `store.127-0-0-1.nip.io` | WordPress (storefront + WP admin) |
| `app.127-0-0-1.nip.io` | dashboard (`/`) and gateway REST API (`/api`) |
| `mcp.127-0-0-1.nip.io` | gateway MCP endpoint (`/mcp`) |

## Why nip.io

[nip.io](https://nip.io) is public wildcard DNS: any name ending in `127-0-0-1.nip.io` resolves to `127.0.0.1`. k3d maps host ports 80/443 to Traefik (`deploy/k3d/cluster.yaml`), and Traefik routes by host name. So:

- no hosts-file edits, no admin rights;
- the same names work in browsers, Claude Code, Cursor and MCP Inspector;
- the charts only need `global.domain`, exactly like production (`urumi-sajjad.duckdns.org`).

## Fallbacks

**Offline, or a network whose DNS blocks nip.io:** add the names to the hosts file (`C:\Windows\System32\drivers\etc\hosts` on Windows, `/etc/hosts` elsewhere), as administrator:

```text
127.0.0.1 store.127-0-0-1.nip.io app.127-0-0-1.nip.io mcp.127-0-0-1.nip.io
```

**Slow first connection from Python on a DNS64 network** (some mobile hotspots): the resolver returns a synthesized IPv6 address that routes to the internet instead of localhost; curl and browsers fall back to IPv4 quickly, Python's HTTP clients wait for a timeout. The same hosts-file line fixes it, because the hosts file answers with IPv4 only.
