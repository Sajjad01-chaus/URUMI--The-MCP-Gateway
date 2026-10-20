import time

from mcp import Client
from starlette.testclient import TestClient

from notes_mcp.server import NoteStore, Settings, build_app, create_server


async def test_create_list_search_get(tmp_path):
    async with Client(create_server(NoteStore(str(tmp_path / "n.db")), "test-v1")) as client:
        assert client.server_info.version == "test-v1"
        created = (await client.call_tool("create_note", {"title": "Daily", "body": "2 orders today"})).structured_content
        assert created["id"] == 1
        assert len((await client.call_tool("list_notes", {})).structured_content["result"]) == 1
        assert (await client.call_tool("search", {"query": "orders"})).structured_content["result"][0]["title"] == "Daily"
        missing = await client.call_tool("get_note", {"note_id": 99})
        assert missing.is_error and "not found" in missing.content[0].text


async def test_fault_delay_slows_every_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("FAULT_DELAY_MS", "300")
    async with Client(create_server(NoteStore(str(tmp_path / "n.db")))) as client:
        started = time.monotonic()
        await client.call_tool("list_notes", {})
    assert time.monotonic() - started >= 0.3


def test_token_required_except_healthz(tmp_path):
    app = build_app(Settings(mcp_auth_token="t", notes_db_path=str(tmp_path / "n.db"), allowed_hosts="testserver", app_version="test-v1"))
    with TestClient(app) as http:
        assert http.get("/healthz").json() == {"status": "ok", "version": "test-v1"}
        assert http.post("/mcp", json={}).status_code == 401
