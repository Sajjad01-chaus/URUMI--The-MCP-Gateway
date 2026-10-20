from contextlib import asynccontextmanager

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from test_chat import FakeLLM, ask, tool_call, use_llm  # noqa: F401
from test_gateway_e2e import audit_rows, gw, mcp, names  # noqa: F401
from test_team import logged_in


@asynccontextmanager
async def search_mcp(gw, key: str):
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {key}"}) as http:
        async with Client(streamable_http_client(f"{gw.url}/mcp/search", http_client=http)) as client:
            yield client


async def test_search_endpoint_lists_only_meta_tools_and_full_endpoint_is_unchanged(gw):
    async with search_mcp(gw, gw.admin_key) as search:
        assert await names(search) == {"gateway__search_tools", "gateway__call_tool"}
    async with mcp(gw, gw.admin_key) as full:
        assert "gateway__search_tools" not in await names(full)
    async with httpx2.AsyncClient() as http:
        response = await http.post(f"{gw.url}/mcp/search", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert response.status_code == 401


async def test_bm25_search_filters_by_permission_server_and_limit(gw):
    async with search_mcp(gw, gw.member_key) as member:
        result = await member.call_tool("gateway__search_tools", {"query": "create product search", "limit": 1})
        found = result.structured_content["tools"]
        assert len(found) == 1
        assert found[0]["name"] != "woo__create_product"

        notes = await member.call_tool(
            "gateway__search_tools", {"query": "search", "server": "notes", "limit": 8}
        )
        assert [tool["name"] for tool in notes.structured_content["tools"]] == ["notes__search"]


async def test_meta_call_routes_and_audits_the_real_tool_with_search_marker(gw):
    async with search_mcp(gw, gw.admin_key) as admin:
        result = await admin.call_tool(
            "gateway__call_tool", {"name": "woo__create_product", "arguments": {"name": "Search Mug"}}
        )
    assert not result.is_error and "created Search Mug" in result.content[0].text
    row = (await audit_rows(gw))[-1]
    assert (row.server_slug, row.tool_name, row.exposed_name, row.outcome) == (
        "woo", "create_product", "woo__create_product", "success"
    )
    assert row.args_preview == {"name": "Search Mug", "via": "search"}

    async with search_mcp(gw, gw.member_key) as member:
        denied = await member.call_tool(
            "gateway__call_tool", {"name": "woo__create_product", "arguments": {"name": "No"}}
        )
    assert denied.is_error and "not allowed" in denied.content[0].text
    row = (await audit_rows(gw))[-1]
    assert (row.server_slug, row.tool_name, row.outcome, row.args_preview["via"]) == (
        "woo", "create_product", "denied", "search"
    )

    async with search_mcp(gw, gw.admin_key) as admin:
        invalid = await admin.call_tool("gateway__call_tool", {"arguments": {}})
    assert invalid.is_error and "Invalid" in invalid.content[0].text
    row = (await audit_rows(gw))[-1]
    assert (row.server_slug, row.tool_name, row.outcome) == ("gateway", "call_tool", "error")


async def test_chat_automatically_uses_search_mode_above_threshold(gw, use_llm):
    gw.svc.settings.chat_max_direct_tools = 1
    fake = use_llm(FakeLLM(
        {"content": "", "tool_calls": [tool_call("gateway__search_tools", {"query": "orders"})]},
        {
            "content": "",
            "tool_calls": [tool_call("gateway__call_tool", {"name": "woo__list_orders", "arguments": {}})],
        },
        {"content": "The order is 15."},
    ))
    async with logged_in(gw, "alice@x.io") as alice:
        response = await ask(alice, "What orders came in?")
    body = response.json()
    assert response.status_code == 200 and body["answer"] == "The order is 15."
    assert [call["name"] for call in body["tool_calls"]] == [
        "gateway__search_tools", "gateway__call_tool"
    ]
    assert {tool["function"]["name"] for tool in fake.requests[0][1]} == {
        "gateway__search_tools", "gateway__call_tool"
    }
    rows = await audit_rows(gw)
    assert (rows[-1].server_slug, rows[-1].tool_name, rows[-1].source, rows[-1].args_preview) == (
        "woo", "list_orders", "chat", {"via": "search"}
    )


async def test_tool_rate_limit_is_per_user_and_audited(gw):
    gw.svc.tool_limiter.capacity = 2
    gw.svc.tool_limiter.refill_per_second = 0.001
    async with mcp(gw, gw.member_key) as client:
        first = await client.call_tool("woo__list_orders", {})
        second = await client.call_tool("woo__list_orders", {})
        limited = await client.call_tool("woo__list_orders", {})
    assert not first.is_error and not second.is_error
    assert limited.is_error and "Rate limit reached" in limited.content[0].text
    row = (await audit_rows(gw))[-1]
    assert (row.user_email, row.server_slug, row.tool_name, row.outcome) == (
        "bob@x.io", "woo", "list_orders", "rate_limited"
    )


async def test_http_rate_limit_returns_429_and_retry_after(gw):
    gw.svc.http_limiter.capacity = 2
    gw.svc.http_limiter.refill_per_second = 0.001
    request = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {gw.admin_key}"}) as http:
        responses = [await http.post(f"{gw.url}/mcp", json=request) for _ in range(3)]
    assert all(response.status_code != 429 for response in responses[:2])
    assert responses[2].status_code == 429 and int(responses[2].headers["retry-after"]) >= 1
