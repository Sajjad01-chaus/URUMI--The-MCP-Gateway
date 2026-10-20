import httpx2
from test_gateway_e2e import gw, mcp  # noqa: F401  (gw is a fixture)

from gateway.ssrf import HostResolutionError, UnsafeUrlError
from gateway.upstream import classify_error, describe_error


async def test_gateway_metrics_and_admin_stats(gw):
    async with mcp(gw, gw.admin_key) as client:
        await client.call_tool("woo__list_orders", {})
    async with mcp(gw, gw.member_key) as client:
        await client.call_tool("woo__create_product", {"name": "blocked"})

    async with httpx2.AsyncClient(base_url=gw.url) as http:
        assert (await http.get("/healthz")).json()["version"] == "dev"
        metrics = await http.get("/metrics")
        assert metrics.status_code == 200
        assert 'urumi_tool_calls_total{outcome="success",server="woo",source="mcp",tool="list_orders"} 1.0' in metrics.text
        assert 'urumi_upstream_up{server="woo"} 1.0' in metrics.text

    async with httpx2.AsyncClient(base_url=gw.url, headers={"Authorization": f"Bearer {gw.admin_key}"}) as admin:
        servers = (await admin.get("/api/servers")).json()
        assert {row["slug"]: row["version"] for row in servers} == {"notes": "test-notes", "woo": "test-woo"}
        stats = (await admin.get("/api/stats", params={"window": "15m"})).json()
        assert (stats["window"], stats["total_calls"], stats["total_errors"]) == ("15m", 2, 1)
        woo = next(server for server in stats["servers"] if server["slug"] == "woo")
        assert woo["calls"] == 2 and woo["error_rate"] == 50.0 and woo["p95_ms"] >= woo["p50_ms"]
        assert stats["top_tools"][0]["name"] in {"woo__list_orders", "woo__create_product"}
        assert stats["top_error_tools"] == [{"name": "woo__create_product", "calls": 1, "errors": 1}]
        assert {row["email"] for row in stats["users"]} == {"alice@x.io", "bob@x.io"}

    async with httpx2.AsyncClient(base_url=gw.url, headers={"Authorization": f"Bearer {gw.member_key}"}) as member:
        assert (await member.get("/api/stats")).status_code == 403


def test_upstream_failures_have_bounded_classes_and_clear_messages():
    assert classify_error(HostResolutionError("host 'missing' does not resolve")) == "dns"
    assert classify_error(httpx2.ConnectError("connection refused")) == "refused"
    assert classify_error(TimeoutError()) == "timeout"
    assert classify_error(UnsafeUrlError("metadata address")) == "blocked"
    assert "Service has no ready endpoints" in describe_error(httpx2.ConnectError("connection refused"))
    assert describe_error(UnsafeUrlError("metadata address")).startswith("URL blocked by SSRF guard")
