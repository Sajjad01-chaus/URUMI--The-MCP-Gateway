from datetime import UTC, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
import re
from collections.abc import Callable
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .models import (
    LineItem,
    OrderDetail,
    OrderPage,
    OrderSummary,
    Product,
    ProductPage,
    StatusChange,
    StoreInfo,
)
from .woo_client import WooClient, WooError

OrderStatus = Literal["pending", "processing", "on-hold", "completed", "cancelled", "refunded", "failed"]
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


def create_server(woo: WooClient, public_url: str = "") -> MCPServer:
    """Build the MCP server; every tool talks to the store only through `woo`.

    `public_url` replaces the scheme+host WordPress puts in links: woo-mcp calls WordPress with
    X-Forwarded-Proto https, so WordPress would otherwise always answer with https:// links.
    """
    mcp = MCPServer(
        name="woo-mcp",
        instructions="Tools for one WooCommerce store: products and orders. Dates use the store's timezone.",
    )
    cache: dict[str, Any] = {}

    def public(url: str) -> str:
        if not public_url or not url:
            return url
        return re.sub(r"^https?://[^/]+", public_url.rstrip("/"), url)

    async def call(method: str, path: str, **kwargs) -> Any:
        try:
            return await woo.request(method, path, **kwargs)
        except WooError as exc:
            raise ToolError(str(exc)) from exc

    async def fetch_page(path: str, params: dict) -> Any:
        try:
            return await woo.get_page(path, params)
        except WooError as exc:
            raise ToolError(str(exc)) from exc

    async def store_settings() -> dict[str, str]:
        # Name, timezone and currency rarely change: fetch once per process.
        if "settings" not in cache:
            root = await call("GET", "/")
            currency = await call("GET", "/wc/v3/settings/general/woocommerce_currency")
            cache["settings"] = {
                "name": root.get("name", ""),
                "url": root.get("home") or root.get("url", ""),
                "timezone": root.get("timezone_string") or "",
                "gmt_offset": str(root.get("gmt_offset", 0)),
                "currency": currency.get("value", ""),
            }
        return cache["settings"]

    async def store_tz() -> tzinfo:
        settings = await store_settings()
        if settings["timezone"]:
            return ZoneInfo(settings["timezone"])
        return timezone(timedelta(hours=float(settings["gmt_offset"])))

    @mcp.tool(annotations=READ_ONLY)
    async def get_store_info() -> StoreInfo:
        """Get the store's name, URL, currency, timezone, and the current date/time in the store's timezone."""
        settings = await store_settings()
        now = datetime.now(await store_tz())
        return StoreInfo(
            name=settings["name"],
            url=public(settings["url"]),
            currency=settings["currency"],
            timezone=settings["timezone"] or f"UTC{float(settings['gmt_offset']):+g}",
            now=now.isoformat(timespec="seconds"),
            today=now.date().isoformat(),
        )

    @mcp.tool(annotations=READ_ONLY)
    async def list_products(
        search: Annotated[str | None, Field(description="Text to search for in product names")] = None,
        status: Literal["publish", "draft", "any"] = "any",
        page: Annotated[int, Field(ge=1)] = 1,
        per_page: Annotated[int, Field(ge=1, le=50)] = 20,
    ) -> ProductPage:
        """List products in the store, newest first, with price and stock status."""
        params: dict[str, Any] = {"page": page, "per_page": per_page, "status": status}
        if search:
            params["search"] = search
        result = await fetch_page("/wc/v3/products", params)
        return ProductPage(
            products=[_product(p, public) for p in result.items], page=page, total=result.total, total_pages=result.total_pages
        )

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
    async def create_product(
        name: Annotated[str, Field(min_length=1, max_length=200)],
        regular_price: Annotated[Decimal, Field(ge=0, max_digits=10, decimal_places=2, description="Price, e.g. 12.50")],
        description: str = "",
        sku: str = "",
        stock_quantity: Annotated[int | None, Field(ge=0)] = None,
        status: Literal["publish", "draft"] = "publish",
    ) -> Product:
        """Create a simple product. With status 'publish' it appears on the storefront immediately."""
        body: dict[str, Any] = {"name": name, "type": "simple", "regular_price": f"{regular_price:.2f}", "status": status}
        if description:
            body["description"] = description
        if sku:
            body["sku"] = sku
        if stock_quantity is not None:
            body.update(manage_stock=True, stock_quantity=stock_quantity)
        return _product(await call("POST", "/wc/v3/products", json=body), public)

    @mcp.tool(annotations=READ_ONLY)
    async def list_orders(
        period: Annotated[
            Literal["today", "yesterday", "last_7_days"] | None,
            Field(description="Date window in the store's timezone. Takes precedence over created_after/created_before."),
        ] = None,
        created_after: Annotated[str | None, Field(description="ISO 8601 date/time; no timezone means store time")] = None,
        created_before: Annotated[str | None, Field(description="ISO 8601 date/time; no timezone means store time")] = None,
        status: OrderStatus | None = None,
        page: Annotated[int, Field(ge=1)] = 1,
        per_page: Annotated[int, Field(ge=1, le=50)] = 20,
    ) -> OrderPage:
        """List orders, newest first. Use period='today' for "orders that came in today" (store timezone)."""
        tz = await store_tz()
        start, end = _window(period, created_after, created_before, tz)
        params: dict[str, Any] = {"page": page, "per_page": per_page, "dates_are_gmt": "true"}
        if start:
            params["after"] = start.astimezone(UTC).isoformat()
        if end:
            params["before"] = end.astimezone(UTC).isoformat()
        if status:
            params["status"] = status
        result = await fetch_page("/wc/v3/orders", params)
        window = f"{start.isoformat() if start else 'beginning'} to {end.isoformat() if end else 'now'}"
        return OrderPage(
            orders=[_order_summary(o) for o in result.items],
            page=page,
            total=result.total,
            total_pages=result.total_pages,
            window=window,
        )

    @mcp.tool(annotations=READ_ONLY)
    async def get_order(order_id: Annotated[int, Field(ge=1)]) -> OrderDetail:
        """Get one order with its line items, totals, payment method and status."""
        return _order_detail(await call("GET", f"/wc/v3/orders/{order_id}"))

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True))
    async def update_order_status(
        order_id: Annotated[int, Field(ge=1)],
        status: OrderStatus,
        note: Annotated[str, Field(max_length=500, description="Optional private note added to the order")] = "",
    ) -> StatusChange:
        """Change an order's status. Setting 'refunded' only changes the status; it does not move any money."""
        current = await call("GET", f"/wc/v3/orders/{order_id}")
        updated = await call("PUT", f"/wc/v3/orders/{order_id}", json={"status": status})
        if note:
            await call("POST", f"/wc/v3/orders/{order_id}/notes", json={"note": note, "customer_note": False})
        return StatusChange(
            id=order_id, previous_status=current["status"], new_status=updated["status"], note_added=bool(note)
        )

    return mcp


def _window(
    period: str | None, after: str | None, before: str | None, tz: tzinfo
) -> tuple[datetime | None, datetime | None]:
    """Turn a named period or ISO strings into an aware [start, end) window in the store timezone."""
    if period:
        today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        return {
            "today": (today, today + timedelta(days=1)),
            "yesterday": (today - timedelta(days=1), today),
            "last_7_days": (today - timedelta(days=6), today + timedelta(days=1)),
        }[period]
    return _parse(after, tz), _parse(before, tz)


def _parse(value: str | None, tz: tzinfo) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ToolError(f"Invalid date '{value}': use ISO 8601, e.g. 2026-10-13 or 2026-10-13T09:00") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)


def _product(p: dict[str, Any], public: Callable[[str], str]) -> Product:
    return Product(
        id=p["id"],
        name=p.get("name", ""),
        sku=p.get("sku", ""),
        status=p.get("status", ""),
        price=p.get("price", ""),
        regular_price=p.get("regular_price", ""),
        stock_status=p.get("stock_status", ""),
        permalink=public(p.get("permalink", "")),
    )


def _customer(o: dict[str, Any]) -> str:
    billing = o.get("billing") or {}
    return f"{billing.get('first_name', '')} {billing.get('last_name', '')}".strip() or "Guest"


def _order_summary(o: dict[str, Any]) -> OrderSummary:
    return OrderSummary(
        id=o["id"],
        number=str(o.get("number", o["id"])),
        status=o.get("status", ""),
        total=o.get("total", ""),
        currency=o.get("currency", ""),
        created_at=o.get("date_created", ""),
        customer=_customer(o),
        item_count=sum(i.get("quantity", 0) for i in o.get("line_items", [])),
    )


def _order_detail(o: dict[str, Any]) -> OrderDetail:
    billing = o.get("billing") or {}
    return OrderDetail(
        id=o["id"],
        number=str(o.get("number", o["id"])),
        status=o.get("status", ""),
        currency=o.get("currency", ""),
        total=o.get("total", ""),
        shipping_total=o.get("shipping_total", ""),
        payment_method=o.get("payment_method_title", ""),
        created_at=o.get("date_created", ""),
        customer=_customer(o),
        billing_city=billing.get("city", ""),
        billing_country=billing.get("country", ""),
        customer_note=o.get("customer_note", ""),
        line_items=[
            LineItem(product_id=i.get("product_id", 0), name=i.get("name", ""), quantity=i.get("quantity", 0), total=i.get("total", ""))
            for i in o.get("line_items", [])
        ],
    )
