import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from mcp import Client
from starlette.testclient import TestClient

from woo_mcp.app import build_app
from woo_mcp.config import Settings
from woo_mcp.tools import create_server
from woo_mcp.woo_client import WooClient

ORDER = {
    "id": 15, "number": "15", "status": "processing", "total": "25.00", "currency": "USD",
    "date_created": "2026-10-13T10:58:03", "payment_method_title": "Cash on delivery", "shipping_total": "0.00",
    "customer_note": "", "billing": {"first_name": "Test", "last_name": "Shopper", "city": "SF", "country": "US"},
    "line_items": [{"product_id": 11, "name": "Kubernetes Mug", "quantity": 2, "total": "25.00"}],
}


class FakeWoo:
    """Records requests and answers like the WooCommerce REST API."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.order_status = "processing"

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, method = request.url.path, request.method
        if path == "/wp-json/":
            return httpx.Response(200, json={"name": "Test Store", "home": "https://store.test", "timezone_string": "Asia/Kolkata", "gmt_offset": 5.5})
        if path.endswith("/settings/general/woocommerce_currency"):
            return httpx.Response(200, json={"value": "USD"})
        if path == "/wp-json/wc/v3/orders" and method == "GET":
            return httpx.Response(200, json=[ORDER], headers={"X-WP-Total": "1", "X-WP-TotalPages": "1"})
        if path == "/wp-json/wc/v3/products" and method == "POST":
            body = json.loads(request.content)
            return httpx.Response(201, json={"id": 99, "price": body["regular_price"], "stock_status": "instock", "permalink": "https://store.test/p/99", **body})
        if path == "/wp-json/wc/v3/orders/15" and method == "GET":
            return httpx.Response(200, json={**ORDER, "status": self.order_status})
        if path == "/wp-json/wc/v3/orders/15" and method == "PUT":
            self.order_status = json.loads(request.content)["status"]
            return httpx.Response(200, json={**ORDER, "status": self.order_status})
        if path == "/wp-json/wc/v3/orders/15/notes" and method == "POST":
            return httpx.Response(201, json={"id": 1})
        return httpx.Response(404, json={"code": "woocommerce_rest_shop_order_invalid_id", "message": "Invalid ID."})


@pytest.fixture
def fake():
    return FakeWoo()


@pytest.fixture
def woo(fake):
    return WooClient("http://wordpress", "store.test", "ck_x", "cs_y", transport=httpx.MockTransport(fake.handler))


async def test_exposes_the_six_tools_with_correct_annotations(woo):
    async with Client(create_server(woo)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {"get_store_info", "list_products", "create_product", "list_orders", "get_order", "update_order_status"}
    assert tools["list_orders"].annotations.read_only_hint is True
    assert tools["create_product"].annotations.read_only_hint is False
    assert tools["update_order_status"].annotations.destructive_hint is True


async def test_requests_use_basic_auth_forwarded_proto_and_store_host(woo, fake):
    async with Client(create_server(woo)) as client:
        await client.call_tool("get_order", {"order_id": 15})
    request = fake.requests[-1]
    assert request.headers["x-forwarded-proto"] == "https"
    assert request.headers["host"] == "store.test"
    assert request.headers["authorization"].startswith("Basic ")


async def test_list_orders_today_uses_store_timezone_window(woo, fake):
    async with Client(create_server(woo)) as client:
        result = await client.call_tool("list_orders", {"period": "today"})
    assert not result.is_error
    assert result.structured_content["orders"][0]["id"] == 15
    params = fake.requests[-1].url.params
    after, before = datetime.fromisoformat(params["after"]), datetime.fromisoformat(params["before"])
    assert params["dates_are_gmt"] == "true"
    assert before - after == timedelta(days=1)
    local_start = after.astimezone(ZoneInfo("Asia/Kolkata"))
    assert (local_start.hour, local_start.minute) == (0, 0)
    assert local_start.date() == datetime.now(ZoneInfo("Asia/Kolkata")).date()


async def test_create_product_sends_simple_published_product(woo, fake):
    async with Client(create_server(woo)) as client:
        result = await client.call_tool("create_product", {"name": "Urumi Mug", "regular_price": "12.50"})
    assert not result.is_error
    body = json.loads(fake.requests[-1].content)
    assert body == {"name": "Urumi Mug", "type": "simple", "regular_price": "12.50", "status": "publish"}
    assert result.structured_content["id"] == 99


async def test_invalid_price_is_rejected_before_calling_woo(woo, fake):
    async with Client(create_server(woo)) as client:
        result = await client.call_tool("create_product", {"name": "Bad", "regular_price": "twelve"})
    assert result.is_error
    assert fake.requests == []


async def test_woo_errors_become_readable_tool_errors(woo):
    async with Client(create_server(woo)) as client:
        result = await client.call_tool("get_order", {"order_id": 404})
    assert result.is_error
    assert "HTTP 404" in result.content[0].text and "Invalid ID." in result.content[0].text


async def test_update_order_status_reports_previous_and_adds_note(woo, fake):
    async with Client(create_server(woo)) as client:
        result = await client.call_tool("update_order_status", {"order_id": 15, "status": "completed", "note": "Delivered"})
    assert result.structured_content == {"id": 15, "previous_status": "processing", "new_status": "completed", "note_added": True}
    assert [r.method for r in fake.requests] == ["GET", "PUT", "POST"]


def test_http_app_requires_token_except_healthz(woo):
    settings = Settings(woo_store_host="store.test", woo_consumer_key="ck", woo_consumer_secret="cs", mcp_auth_token="secret-token", allowed_hosts="testserver", app_version="test-v1")
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
    headers = {"Accept": "application/json, text/event-stream"}
    with TestClient(build_app(settings, woo)) as http:
        assert http.get("/healthz").json() == {"status": "ok", "version": "test-v1"}
        assert http.post("/mcp", json=body, headers=headers).status_code == 401
        assert http.post("/mcp", json=body, headers={**headers, "Authorization": "Bearer wrong"}).status_code == 401
        ok = http.post("/mcp", json=body, headers={**headers, "Authorization": "Bearer secret-token"})
        assert ok.status_code == 200 and ok.json()["result"]["serverInfo"] == {
            "name": "woo-mcp", "version": "test-v1"
        }
        bad_host = http.post("/mcp", json=body, headers={**headers, "Authorization": "Bearer secret-token", "Host": "evil.example"})
        assert bad_host.status_code in (400, 421)


async def test_numeric_price_is_accepted_and_sent_as_exact_string(woo, fake):
    async with Client(create_server(woo)) as client:
        result = await client.call_tool("create_product", {"name": "Mug", "regular_price": 9.99})
    assert not result.is_error
    assert json.loads(fake.requests[-1].content)["regular_price"] == "9.99"


@pytest.mark.parametrize("price", ["twelve", -1, "1.999"])
async def test_bad_prices_are_rejected(woo, fake, price):
    async with Client(create_server(woo)) as client:
        result = await client.call_tool("create_product", {"name": "Mug", "regular_price": price})
    assert result.is_error
    assert fake.requests == []


async def test_links_use_the_store_public_url(woo):
    async with Client(create_server(woo, public_url="http://store.test")) as client:
        info = await client.call_tool("get_store_info", {})
        product = await client.call_tool("create_product", {"name": "Mug", "regular_price": "5"})
    assert info.structured_content["url"] == "http://store.test"
    assert product.structured_content["permalink"] == "http://store.test/p/99"
