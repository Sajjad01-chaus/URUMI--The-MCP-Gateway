import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx2
import pytest
import uvicorn
from cryptography.fernet import Fernet
from mcp import Client, types
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError
from sqlalchemy import select

from gateway.config import Settings
from gateway.main import create_app
from gateway.models import ApiKey, AuditLog, Base, User, utcnow
from gateway.security import generate_api_key

READ = types.ToolAnnotations(read_only_hint=True)
WRITE = types.ToolAnnotations(read_only_hint=False)


def make_woo() -> MCPServer:
    server = MCPServer("woo")

    @server.tool(annotations=READ)
    def list_orders() -> list[int]:
        """List orders."""
        return [15]

    @server.tool(annotations=WRITE)
    def create_product(name: str) -> str:
        """Create a product."""
        return f"created {name}"

    @server.tool(annotations=READ)
    def search(q: str) -> str:
        """Search products."""
        return f"woo:{q}"

    return server


def make_notes() -> MCPServer:
    server = MCPServer("notes")

    @server.tool(annotations=READ)
    def search(q: str) -> str:
        """Search notes."""
        return f"notes:{q}"

    @server.tool(annotations=READ)
    async def slow() -> str:
        """Takes too long."""
        await asyncio.sleep(5)
        return "late"

    @server.tool(annotations=READ)
    def big() -> str:
        """Returns more than the gateway's size cap."""
        return "x" * 300_000

    return server


@dataclass
class Gateway:
    url: str
    svc: object
    admin_key: str
    member_key: str
    dead: set
    upstreams: dict[str, MCPServer]


@pytest.fixture
async def gw(tmp_path):
    upstreams = {"woo": make_woo(), "notes": make_notes()}
    dead: set[str] = set()

    @asynccontextmanager
    async def connector(target):
        if target.slug in dead:
            raise httpx2.ConnectError("connection refused")
        async with Client(upstreams[target.slug]) as client:
            yield client

    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'gw.db'}",
        credential_encryption_key=Fernet.generate_key().decode(),
        seed_servers=json.dumps([
            {"slug": "woo", "name": "Woo", "url": "http://woo.test/mcp"},
            {"slug": "notes", "name": "Notes", "url": "http://notes.test/mcp", "timeout_seconds": 1},
        ]),
        allowed_mcp_hosts="127.0.0.1:*",
        health_timeout_seconds=2,
    )
    app = create_app(settings, connector=connector, run_monitor=False)
    svc = app.state.services
    async with svc.db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    keys = {}
    async with svc.db.session() as session:
        for email, role in (("alice@x.io", "admin"), ("bob@x.io", "member")):
            user = User(email=email, role=role)
            session.add(user)
            await session.flush()
            key, prefix, key_hash = generate_api_key()
            session.add(ApiKey(user_id=user.id, prefix=prefix, key_hash=key_hash))
            keys[role] = key
        await session.commit()

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        if task.done():
            await task
        await asyncio.sleep(0.05)
    else:
        task.cancel()
        raise TimeoutError("test gateway did not start within 10 seconds")
    port = server.servers[0].sockets[0].getsockname()[1]
    await svc.monitor.check_all()
    yield Gateway(f"http://127.0.0.1:{port}", svc, keys["admin"], keys["member"], dead, upstreams)
    server.should_exit = True
    await task


@asynccontextmanager
async def mcp(gw: Gateway, key: str, mode: str = "auto", handler=None):
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {key}"}) as http:
        async with Client(streamable_http_client(f"{gw.url}/mcp", http_client=http), mode=mode, message_handler=handler) as client:
            yield client


async def names(client) -> set[str]:
    return {t.name for t in (await client.list_tools()).tools}


async def audit_rows(gw: Gateway) -> list[AuditLog]:
    async with gw.svc.db.session() as session:
        return list((await session.scalars(select(AuditLog).order_by(AuditLog.id))).all())


async def test_aggregates_both_servers_with_prefixes_and_role_filtering(gw):
    async with mcp(gw, gw.admin_key) as admin:
        assert await names(admin) == {"woo__list_orders", "woo__create_product", "woo__search", "notes__search", "notes__slow", "notes__big"}
    async with mcp(gw, gw.member_key, mode="legacy") as member:
        assert "woo__create_product" not in await names(member)
        assert {"woo__search", "notes__search"} <= await names(member)


async def test_same_tool_name_routes_to_the_right_server(gw):
    async with mcp(gw, gw.member_key) as client:
        woo = await client.call_tool("woo__search", {"q": "mug"})
        notes = await client.call_tool("notes__search", {"q": "mug"})
    assert woo.structured_content == {"result": "woo:mug"}
    assert notes.structured_content == {"result": "notes:mug"}
    assert woo.meta["io.modelcontextprotocol/serverInfo"]["name"] == "urumi-mcp-gateway"


async def test_member_cannot_call_hidden_write_tool_directly(gw):
    async with mcp(gw, gw.member_key) as client:
        result = await client.call_tool("woo__create_product", {"name": "x"})
    assert result.is_error and "not allowed" in result.content[0].text
    row = (await audit_rows(gw))[-1]
    assert (row.user_email, row.outcome, row.server_slug) == ("bob@x.io", "denied", "woo")


async def test_missing_bad_and_revoked_keys_are_rejected(gw):
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    async with httpx2.AsyncClient() as http:
        assert (await http.post(f"{gw.url}/mcp", json=body)).status_code == 401
        assert (await http.post(f"{gw.url}/mcp", json=body, headers={"Authorization": "Bearer mcpgw_nope"})).status_code == 401

    async with mcp(gw, gw.member_key) as client:
        assert (await client.call_tool("woo__list_orders", {})).is_error is False
        async with gw.svc.db.session() as session:
            for key in (await session.scalars(select(ApiKey))).all():
                key.revoked_at = utcnow()
            await session.commit()
        with pytest.raises(MCPError):
            await client.call_tool("woo__list_orders", {})


async def test_slow_upstream_times_out_without_blocking_others(gw):
    async with mcp(gw, gw.admin_key) as client:
        slow = asyncio.create_task(client.call_tool("notes__slow", {}))
        await asyncio.sleep(0.1)
        started = asyncio.get_running_loop().time()
        fast = await client.call_tool("woo__list_orders", {})
        fast_seconds = asyncio.get_running_loop().time() - started
        slow_result = await slow
    assert not fast.is_error and fast_seconds < 1
    assert slow_result.is_error and "did not answer within 1s" in slow_result.content[0].text
    outcomes = {(r.exposed_name, r.outcome) for r in await audit_rows(gw)}
    assert ("notes__slow", "timeout") in outcomes and ("woo__list_orders", "success") in outcomes


async def test_oversized_result_is_replaced_by_an_error(gw):
    async with mcp(gw, gw.admin_key) as client:
        result = await client.call_tool("notes__big", {})
        assert not (await client.call_tool("woo__list_orders", {})).is_error
    assert result.is_error and "result too large" in result.content[0].text
    rows = [r for r in await audit_rows(gw) if r.exposed_name == "notes__big"]
    assert rows and rows[-1].outcome == "error" and "too large" in rows[-1].error_message


async def test_dead_upstream_is_marked_unreachable_and_others_keep_working(gw):
    gw.dead.add("notes")
    await gw.svc.monitor.check_all()
    assert gw.svc.catalog.servers["notes"].status == "connected"
    await gw.svc.monitor.check_all()
    notes = gw.svc.catalog.servers["notes"]
    assert notes.status == "unreachable" and "connection refused" in notes.last_error
    async with mcp(gw, gw.admin_key) as client:
        assert not any(n.startswith("notes__") for n in await names(client))
        assert not (await client.call_tool("woo__list_orders", {})).is_error
        result = await client.call_tool("notes__search", {"q": "x"})
    assert result.is_error and "unreachable" in result.content[0].text
    gw.dead.clear()
    await gw.svc.monitor.check_all()
    assert gw.svc.catalog.servers["notes"].status == "connected"


async def test_disabling_a_server_notifies_both_protocol_generations(gw):
    notes_id = gw.svc.catalog.servers["notes"].id
    legacy_event = asyncio.Event()

    async def on_message(message):
        if isinstance(message, types.ToolListChangedNotification):
            legacy_event.set()

    async with mcp(gw, gw.admin_key, mode="legacy", handler=on_message) as legacy, mcp(gw, gw.admin_key) as modern:
        assert "notes__search" in await names(legacy)
        await asyncio.sleep(0.3)
        async with modern.listen(tools_list_changed=True) as subscription:
            async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {gw.admin_key}"}) as http:
                response = await http.patch(f"{gw.url}/api/servers/{notes_id}", json={"enabled": False})
            assert response.status_code == 200 and response.json()["status"] == "disabled"
            await asyncio.wait_for(anext(aiter(subscription)), timeout=5)
        await asyncio.wait_for(legacy_event.wait(), timeout=5)
        assert not any(n.startswith("notes__") for n in await names(legacy))
        assert not any(n.startswith("notes__") for n in await names(modern))
        blocked = await legacy.call_tool("notes__search", {"q": "x"})
    assert blocked.is_error and "disabled" in blocked.content[0].text


async def test_rest_api_never_returns_credentials_and_members_cannot_edit(gw):
    async with httpx2.AsyncClient() as http:
        admin = {"Authorization": f"Bearer {gw.admin_key}"}
        member = {"Authorization": f"Bearer {gw.member_key}"}
        created = await http.post(
            f"{gw.url}/api/servers",
            json={"name": "Ext", "slug": "ext", "url": "https://93.184.216.34/mcp", "auth_type": "bearer", "credential": "TOPSECRET"},
            headers=admin,
        )
        assert created.status_code == 201, created.text
        listing = await http.get(f"{gw.url}/api/servers", headers=member)
        assert "TOPSECRET" not in listing.text and listing.json()[0]["has_credential"] in (True, False)
        assert (await http.post(f"{gw.url}/api/servers", json={"name": "x", "slug": "x", "url": "https://93.184.216.34/"}, headers=member)).status_code == 403
        ssrf = await http.post(f"{gw.url}/api/servers", json={"name": "m", "slug": "meta", "url": "http://169.254.169.254/"}, headers=admin)
        assert ssrf.status_code == 422 and "metadata" in ssrf.text
