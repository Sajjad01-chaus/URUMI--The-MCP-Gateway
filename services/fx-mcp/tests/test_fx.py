from mcp import Client
from starlette.testclient import TestClient

from fx_mcp.server import Settings, build_app, create_server


async def test_convert_usd_to_eur_and_back():
    async with Client(create_server()) as client:
        eur = (await client.call_tool("convert_currency", {"amount": 25, "from_currency": "usd", "to_currency": "EUR"})).structured_content
        inr = (await client.call_tool("convert_currency", {"amount": "100", "from_currency": "EUR", "to_currency": "INR"})).structured_content
        bad = await client.call_tool("convert_currency", {"amount": 1, "from_currency": "USD", "to_currency": "XYZ"})
    assert (eur["converted"], eur["to_currency"]) == ("23.00", "EUR")
    assert inr["converted"] == "9043.48"
    assert bad.is_error and "unsupported currency" in bad.content[0].text


async def test_search_finds_by_code_or_name():
    async with Client(create_server()) as client:
        found = (await client.call_tool("search", {"query": "rupee"})).structured_content["result"]
    assert [c["code"] for c in found] == ["INR"]


def test_header_api_key_required():
    with TestClient(build_app(Settings(fx_api_key="k", allowed_hosts="testserver"))) as http:
        assert http.get("/healthz").status_code == 200
        assert http.post("/mcp", json={}).status_code == 401
        assert http.post("/mcp", json={}, headers={"X-API-Key": "wrong"}).status_code == 401
