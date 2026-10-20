import json
import uuid

import httpx2
from sqlalchemy import select
from test_gateway_e2e import gw  # noqa: F401

from gateway.models import ManagedRelease, UpstreamServer


def payload(**overrides):
    body = {
        "name": "Clock",
        "slug": "clock",
        "image": "ghcr.io/sajjad01-chaus/urumi-fx-mcp:abc123",
        "port": 8000,
        "path": "/mcp",
        "env": {"LOG_LEVEL": "info"},
        "secret_env": {"MCP_TOKEN": "write-only-secret"},
        "auth_type": "bearer",
        "credential_env": "MCP_TOKEN",
        "internet_egress": False,
        "size": "small",
    }
    body.update(overrides)
    return body


async def test_hosted_mcp_is_admin_only_validated_encrypted_registered_and_deleted(gw):
    admin = {"Authorization": f"Bearer {gw.admin_key}"}
    member = {"Authorization": f"Bearer {gw.member_key}"}
    async with httpx2.AsyncClient(base_url=gw.url) as http:
        assert (await http.post("/api/hosted-servers", headers=member, json=payload())).status_code == 403
        assert (await http.post(
            "/api/hosted-servers", headers=admin, json=payload(image="ghcr.io/other/server:v1")
        )).status_code == 422
        assert (await http.post(
            "/api/hosted-servers", headers=admin, json=payload(image="ghcr.io/sajjad01-chaus/server:latest")
        )).status_code == 422

        created_response = await http.post("/api/hosted-servers", headers=admin, json=payload())
        assert created_response.status_code == 201
        created = created_response.json()
        assert created["secret_env_names"] == ["MCP_TOKEN"] and "write-only-secret" not in json.dumps(created)
        release_id = uuid.UUID(created["id"])
        async with gw.svc.db.session() as session:
            row = await session.get(ManagedRelease, release_id)
            assert b"write-only-secret" not in row.secrets_encrypted
            row.status = "ready"
            await session.commit()

        gw.upstreams["clock"] = gw.upstreams["notes"]
        await gw.svc.release_sync.reconcile()
        state = gw.svc.catalog.servers["clock"]
        assert state.status == "connected" and state.managed_kind == "hosted_mcp"
        async with gw.svc.db.session() as session:
            row = await session.get(ManagedRelease, release_id)
            server = await session.scalar(select(UpstreamServer).where(UpstreamServer.slug == "clock"))
            assert server and server.managed_kind == "hosted_mcp" and row.server_id == server.id

        duplicate = await http.post("/api/hosted-servers", headers=admin, json=payload())
        assert duplicate.status_code == 200 and duplicate.json()["id"] == created["id"]
        deleted = await http.delete(f"/api/hosted-servers/{release_id}", headers=admin)
        assert deleted.status_code == 202 and deleted.json()["status"] == "deleting"
        assert "clock" not in gw.svc.catalog.servers
        async with gw.svc.db.session() as session:
            row = await session.get(ManagedRelease, release_id)
            server = await session.scalar(select(UpstreamServer).where(UpstreamServer.slug == "clock"))
            assert row.server_id is None and row.desired == "absent" and server is None
