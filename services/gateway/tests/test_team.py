import asyncio
import json
from contextlib import asynccontextmanager

import httpx2
import pytest
from cryptography.fernet import Fernet
from mcp.shared.exceptions import MCPError
from sqlalchemy import func, select, update
from test_gateway_e2e import audit_rows, gw, mcp, names  # noqa: F401  (gw is a fixture)

from gateway.catalog import Catalog
from gateway.config import Settings
from gateway.db import Database
from gateway.models import ApiKey, Base, User, utcnow
from gateway.registry import Registry
from gateway.security import CredentialBox, hash_password
from gateway.sessions import LoginThrottle

PASSWORD = "correct-horse-battery"
XRW = {"X-Requested-With": "dashboard"}


async def set_password(gw, email: str) -> None:
    async with gw.svc.db.session() as session:
        await session.execute(update(User).where(User.email == email).values(password_hash=hash_password(PASSWORD)))
        await session.commit()


@asynccontextmanager
async def logged_in(gw, email: str):
    await set_password(gw, email)
    async with httpx2.AsyncClient(base_url=gw.url) as http:
        response = await http.post("/api/auth/login", json={"email": email.upper(), "password": PASSWORD})
        assert response.status_code == 200, response.text
        yield http


async def key_id_of(gw, email: str):
    async with gw.svc.db.session() as session:
        return await session.scalar(select(ApiKey.id).join(User, ApiKey.user_id == User.id).where(User.email == email))


async def test_login_session_csrf_key_creation_and_logout(gw):
    async with httpx2.AsyncClient(base_url=gw.url) as anon:
        assert (await anon.post("/api/auth/login", json={"email": "alice@x.io", "password": "wrong"})).status_code == 401
        assert (await anon.get("/api/auth/me")).status_code == 401

    async with logged_in(gw, "alice@x.io") as http:
        cookie = http.cookies.jar
        assert any(c.name == "urumi_session" for c in cookie)
        me = (await http.get("/api/auth/me")).json()
        assert (me["email"], me["role"]) == ("alice@x.io", "admin") and me["last_login_at"]

        # Cookie-authenticated writes without the CSRF header are refused.
        assert (await http.post("/api/keys", json={"name": "laptop"})).status_code == 403
        created = await http.post("/api/keys", json={"name": "laptop"}, headers=XRW)
        assert created.status_code == 201 and created.json()["key"].startswith("mcpgw_")
        new_key = created.json()["key"]
        listed = (await http.get("/api/keys")).json()
        assert all("key" not in k for k in listed) and {k["user_email"] for k in listed} == {"alice@x.io", "bob@x.io"}

        async with mcp(gw, new_key) as client:
            assert "woo__create_product" in await names(client)

        await http.post("/api/auth/logout", headers=XRW)
        assert (await http.get("/api/auth/me")).status_code == 401


async def test_api_key_cannot_mint_new_keys(gw):
    async with httpx2.AsyncClient(base_url=gw.url, headers={"Authorization": f"Bearer {gw.member_key}"}) as http:
        assert (await http.post("/api/keys", json={"name": "x"})).status_code == 403
        assert len((await http.get("/api/keys")).json()) == 1


async def test_login_is_throttled_after_repeated_failures(gw):
    await set_password(gw, "bob@x.io")
    async with httpx2.AsyncClient(base_url=gw.url) as http:
        for _ in range(5):
            assert (await http.post("/api/auth/login", json={"email": "bob@x.io", "password": "nope"})).status_code == 401
        blocked = await http.post("/api/auth/login", json={"email": "bob@x.io", "password": PASSWORD})
    assert blocked.status_code == 429


async def test_invite_accept_flow(gw):
    async with logged_in(gw, "alice@x.io") as admin:
        assert (await admin.post("/api/invites", json={"email": "bob@x.io"}, headers=XRW)).status_code == 409
        invite = await admin.post("/api/invites", json={"email": "Carol@X.io", "role": "member"}, headers=XRW)
        assert invite.status_code == 201
        link = invite.json()["link"]
        assert link.startswith("http://app.127-0-0-1.nip.io/invite/")
        assert [i["email"] for i in (await admin.get("/api/invites")).json()] == ["carol@x.io"]
    token = link.rsplit("/", 1)[1]

    async with httpx2.AsyncClient(base_url=gw.url) as carol:
        assert (await carol.get(f"/api/invites/{token}")).json()["email"] == "carol@x.io"
        assert (await carol.get("/api/invites/not-a-token")).status_code == 404
        short = await carol.post(f"/api/invites/{token}/accept", json={"name": "Carol", "password": "short"})
        assert short.status_code == 422
        accepted = await carol.post(f"/api/invites/{token}/accept", json={"name": "Carol", "password": PASSWORD})
        assert accepted.status_code == 201
        me = (await carol.get("/api/auth/me")).json()
        assert (me["email"], me["role"]) == ("carol@x.io", "member")
        assert (await carol.post(f"/api/invites/{token}/accept", json={"name": "C", "password": PASSWORD})).status_code == 404
        assert (await carol.post("/api/invites", json={"email": "dan@x.io"}, headers=XRW)).status_code == 403
        assert (await carol.get("/api/members")).status_code == 403


async def test_revoked_key_gets_401_on_next_call_and_is_audited(gw):
    bob_key_id = await key_id_of(gw, "bob@x.io")
    alice_key_id = await key_id_of(gw, "alice@x.io")
    async with httpx2.AsyncClient(base_url=gw.url, headers={"Authorization": f"Bearer {gw.member_key}"}) as bob:
        assert (await bob.delete(f"/api/keys/{alice_key_id}")).status_code == 404

    async with mcp(gw, gw.member_key) as client:
        assert not (await client.call_tool("woo__list_orders", {})).is_error
        async with httpx2.AsyncClient(base_url=gw.url, headers={"Authorization": f"Bearer {gw.admin_key}"}) as admin:
            assert (await admin.delete(f"/api/keys/{bob_key_id}")).status_code == 204
        with pytest.raises(MCPError):
            await client.call_tool("woo__list_orders", {})

    call = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "woo__list_orders", "arguments": {}}}
    async with httpx2.AsyncClient() as http:
        response = await http.post(f"{gw.url}/mcp", json=call, headers={"Authorization": f"Bearer {gw.member_key}"})
        assert response.status_code == 401
        await http.post(f"{gw.url}/mcp", json=call, headers={"Authorization": "Bearer mcpgw_unknown"})
    denied = [r for r in await audit_rows(gw) if r.outcome == "denied"]
    assert [(r.user_email, r.exposed_name, r.error_message) for r in denied][-1] == ("bob@x.io", "woo__list_orders", "API key revoked")
    assert all(r.user_email == "bob@x.io" for r in denied)


async def test_disabled_member_is_locked_out_and_last_admin_is_protected(gw):
    async with logged_in(gw, "alice@x.io") as admin:
        members = {m["email"]: m for m in (await admin.get("/api/members")).json()}
        assert members["bob@x.io"]["active_keys"] == 1
        disabled = await admin.patch(f"/api/members/{members['bob@x.io']['id']}", json={"status": "disabled"}, headers=XRW)
        assert disabled.json()["status"] == "disabled"
        async with httpx2.AsyncClient() as http:
            ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
            assert (await http.post(f"{gw.url}/mcp", json=ping, headers={"Authorization": f"Bearer {gw.member_key}"})).status_code == 401
        demote_self = await admin.patch(f"/api/members/{members['alice@x.io']['id']}", json={"role": "member"}, headers=XRW)
        assert demote_self.status_code == 409


async def test_member_cannot_list_or_call_write_tools_until_admin_overrides(gw):
    async with mcp(gw, gw.member_key) as member:
        assert "woo__create_product" not in await names(member)
        assert (await member.call_tool("woo__create_product", {"name": "x"})).is_error

    async with logged_in(gw, "alice@x.io") as admin, httpx2.AsyncClient(base_url=gw.url, headers={"Authorization": f"Bearer {gw.member_key}"}) as bob:
        rows = {p["exposed_name"]: p for p in (await admin.get("/api/permissions")).json()}
        assert rows["woo__create_product"]["min_role"] == "admin" and rows["woo__list_orders"]["min_role"] == "member"
        assert (await bob.put("/api/permissions/woo/create_product", json={"min_role": "member"})).status_code == 403
        opened = await admin.put("/api/permissions/woo/create_product", json={"min_role": "member"}, headers=XRW)
        assert opened.json()["override"] == "member"
        async with mcp(gw, gw.member_key) as member:
            assert "woo__create_product" in await names(member)
            assert not (await member.call_tool("woo__create_product", {"name": "mug"})).is_error
        await admin.put("/api/permissions/woo/create_product", json={"min_role": None}, headers=XRW)
        async with mcp(gw, gw.member_key) as member:
            assert "woo__create_product" not in await names(member)
        assert (await admin.put("/api/permissions/woo/nope", json={"min_role": "member"}, headers=XRW)).status_code == 404


async def test_audit_api_scopes_members_and_paginates(gw):
    async with mcp(gw, gw.admin_key) as alice, mcp(gw, gw.member_key) as bob:
        await alice.call_tool("woo__list_orders", {})
        await alice.call_tool("woo__create_product", {"name": "mug"})
        await bob.call_tool("woo__search", {"q": "mug"})
        await bob.call_tool("woo__create_product", {"name": "x"})

    async with httpx2.AsyncClient(base_url=gw.url, headers={"Authorization": f"Bearer {gw.member_key}"}) as bob:
        own = (await bob.get("/api/audit", params={"user": "alice@x.io"})).json()["items"]
        assert {r["user_email"] for r in own} == {"bob@x.io"} and len(own) == 2
    async with httpx2.AsyncClient(base_url=gw.url, headers={"Authorization": f"Bearer {gw.admin_key}"}) as admin:
        everything = (await admin.get("/api/audit")).json()["items"]
        assert len(everything) == 4 and everything[0]["exposed_name"] == "woo__create_product"
        denied = (await admin.get("/api/audit", params={"outcome": "denied"})).json()["items"]
        assert [(r["user_email"], r["source"]) for r in denied] == [("bob@x.io", "mcp")]
        page1 = (await admin.get("/api/audit", params={"limit": 3})).json()
        page2 = (await admin.get("/api/audit", params={"limit": 3, "cursor": page1["next_cursor"]})).json()
        assert len(page1["items"]) == 3 and len(page2["items"]) == 1 and page2["next_cursor"] is None
        assert (await admin.get("/api/audit", params={"user": "alice@x.io", "server": "woo"})).json()["items"][0]["duration_ms"] >= 0


async def test_seeded_tool_policy_is_written_once(tmp_path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'seed.db'}",
        credential_encryption_key=Fernet.generate_key().decode(),
        seed_servers=json.dumps([{"slug": "notes", "name": "Notes", "url": "http://notes.test/mcp",
                                  "tool_policies": {"create_note": "member"}}]),
    )
    db = Database(settings.database_url)
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    catalog = Catalog()
    registry = Registry(db, CredentialBox(settings.credential_encryption_key), settings, catalog)
    await registry.seed()
    await registry.load()
    assert catalog.policies == {("notes", "create_note"): "member"}

    await registry.set_policy(catalog.servers["notes"], "create_note", None, by=None)
    await registry.seed()
    fresh = Catalog()
    await Registry(db, registry.box, settings, fresh).load()
    assert fresh.policies == {}
    await db.dispose()


def test_login_throttle_forgets_expired_entries_and_caps_memory(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("gateway.sessions.time.monotonic", lambda: clock[0])
    throttle = LoginThrottle(limit=2, window=60, max_tracked=3)
    throttle.fail("a@x.io")
    throttle.fail("a@x.io")
    assert throttle.blocked("a@x.io")
    clock[0] += 61
    assert not throttle.blocked("a@x.io") and "a@x.io" not in throttle._failures
    for i in range(3):
        throttle.fail(f"user{i}@x.io")
    clock[0] += 61
    throttle.fail("new@x.io")
    assert set(throttle._failures) == {"new@x.io"}


async def test_revoked_key_with_garbage_body_still_gets_clean_401(gw):
    async with gw.svc.db.session() as session:
        await session.execute(update(ApiKey).values(revoked_at=utcnow()))
        await session.commit()
    async with httpx2.AsyncClient() as http:
        response = await http.post(
            f"{gw.url}/mcp", content=b"\xff not json", headers={"Authorization": f"Bearer {gw.member_key}"}
        )
    assert response.status_code == 401
    assert not [r for r in await audit_rows(gw) if r.outcome == "denied"]


async def test_accepting_an_invite_for_an_existing_email_is_409_not_500(gw):
    async with logged_in(gw, "alice@x.io") as admin:
        link = (await admin.post("/api/invites", json={"email": "dave@x.io"}, headers=XRW)).json()["link"]
    async with gw.svc.db.session() as session:
        session.add(User(email="dave@x.io", role="member"))
        await session.commit()
    async with httpx2.AsyncClient(base_url=gw.url) as dave:
        response = await dave.post(f"/api/invites/{link.rsplit('/', 1)[1]}/accept", json={"name": "D", "password": PASSWORD})
    assert response.status_code == 409


async def test_two_racing_accepts_create_one_user(gw):
    async with logged_in(gw, "alice@x.io") as admin:
        link = (await admin.post("/api/invites", json={"email": "erin@x.io"}, headers=XRW)).json()["link"]
    url = f"/api/invites/{link.rsplit('/', 1)[1]}/accept"
    async with httpx2.AsyncClient(base_url=gw.url) as a, httpx2.AsyncClient(base_url=gw.url) as b:
        results = await asyncio.gather(
            a.post(url, json={"name": "E1", "password": PASSWORD}), b.post(url, json={"name": "E2", "password": PASSWORD})
        )
    assert sorted(r.status_code for r in results)[0] == 201 and all(r.status_code in (201, 404, 409) for r in results)
    assert sorted(r.status_code for r in results) != [201, 201]
    async with gw.svc.db.session() as session:
        assert await session.scalar(select(func.count()).select_from(User).where(User.email == "erin@x.io")) == 1


async def test_tools_api_matches_mcp_tools_list_per_role_and_meta_gives_mcp_url(gw):
    for key in (gw.admin_key, gw.member_key):
        async with mcp(gw, key) as client, httpx2.AsyncClient(base_url=gw.url, headers={"Authorization": f"Bearer {key}"}) as http:
            rest = (await http.get("/api/tools")).json()
            assert {t["exposed_name"] for t in rest} == await names(client)
            assert all(t["server_slug"] and "properties" in t["input_schema"] for t in rest)
            assert (await http.get("/api/meta")).json() == {"mcp_url": "http://mcp.127-0-0-1.nip.io/mcp"}
