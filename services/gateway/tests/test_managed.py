import json
import uuid

import httpx2
from sqlalchemy import select
from test_gateway_e2e import gw  # noqa: F401

from gateway.managed import generate_store_secrets
from gateway.models import ManagedRelease


async def test_store_lifecycle_is_idempotent_encrypted_and_owner_scoped(gw):
    admin_headers = {"Authorization": f"Bearer {gw.admin_key}"}
    member_headers = {"Authorization": f"Bearer {gw.member_key}"}
    async with httpx2.AsyncClient(base_url=gw.url) as http:
        created = await http.post("/api/stores", json={"name": "London Shop", "slug": "london"}, headers=admin_headers)
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["status"] == "provisioning" and body["url"].endswith("store-london.127-0-0-1.nip.io")

        repeated = await http.post("/api/stores", json={"name": "Ignored", "slug": "london"}, headers=admin_headers)
        assert repeated.status_code == 200 and repeated.json()["id"] == body["id"]
        assert (await http.post("/api/stores", json={"name": "Mine", "slug": "london"}, headers=member_headers)).status_code == 409

        member_list = (await http.get("/api/stores", headers=member_headers)).json()
        assert [store["slug"] for store in member_list] == ["main"]
        admin_list = (await http.get("/api/stores", headers=admin_headers)).json()
        assert {store["slug"] for store in admin_list} == {"main", "london"}
        server_collision = await http.post(
            "/api/servers",
            json={"name": "Collision", "slug": "london", "url": "http://notes-mcp:8000/mcp"},
            headers=admin_headers,
        )
        assert server_collision.status_code == 422 and "managed release" in server_collision.text

        assert (await http.delete(f"/api/stores/{body['id']}", headers=member_headers)).status_code == 404
        deleting = await http.delete(f"/api/stores/{body['id']}", headers=admin_headers)
        assert deleting.status_code == 202 and deleting.json()["status"] == "deleting"

    async with gw.svc.db.session() as session:
        row = await session.scalar(select(ManagedRelease).where(ManagedRelease.slug == "london"))
        assert row and row.desired == "absent" and b"mysqlRootPassword" not in row.secrets_encrypted
        stored = json.loads(gw.svc.box.decrypt(row.secrets_encrypted))
    assert stored["wooConsumerKey"].startswith("ck_") and len(stored["wooConsumerKey"]) == 43
    assert stored["wooConsumerSecret"].startswith("cs_") and len(stored["wooConsumerSecret"]) == 43


async def test_retry_reuses_secrets_and_limits_are_enforced(gw):
    headers = {"Authorization": f"Bearer {gw.admin_key}"}
    async with httpx2.AsyncClient(base_url=gw.url, headers=headers) as http:
        london = (await http.post("/api/stores", json={"name": "London", "slug": "london"})).json()
        assert (await http.post("/api/stores", json={"name": "Paris", "slug": "paris"})).status_code == 201
        limited = await http.post("/api/stores", json={"name": "Rome", "slug": "rome"})
        assert limited.status_code == 409 and "per user" in limited.text

        async with gw.svc.db.session() as session:
            row = await session.get(ManagedRelease, uuid.UUID(london["id"]))
            before = row.secrets_encrypted
            row.status = "failed"
            row.status_reason = "test failure"
            row.attempts = 3
            await session.commit()

        retried = await http.post(f"/api/stores/{london['id']}/retry")
        assert retried.status_code == 200 and retried.json()["status"] == "provisioning"
        async with gw.svc.db.session() as session:
            row = await session.get(ManagedRelease, uuid.UUID(london["id"]))
            assert row.secrets_encrypted == before and row.attempts == 0


async def test_store_validation_and_server_slug_collision(gw):
    headers = {"Authorization": f"Bearer {gw.admin_key}"}
    async with httpx2.AsyncClient(base_url=gw.url, headers=headers) as http:
        for slug in ("Bad_Slug", "two--hyphens", "gateway", "woo"):
            assert (await http.post("/api/stores", json={"name": "X", "slug": slug})).status_code in (409, 422)


def test_generated_store_secrets_are_unique_and_complete():
    first, second = generate_store_secrets(), generate_store_secrets()
    assert set(first) == {
        "mysqlRootPassword",
        "mysqlPassword",
        "wpAdminUser",
        "wpAdminPassword",
        "wpAdminEmail",
        "wooConsumerKey",
        "wooConsumerSecret",
        "wooMcpToken",
    }
    assert first["mysqlPassword"] != second["mysqlPassword"]
