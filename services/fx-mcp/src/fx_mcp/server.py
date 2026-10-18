import hmac
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
RATES_AS_OF = "2026-10-01 (static demo snapshot, not live market data)"
# Units of each currency per 1 USD.
USD_RATES = {
    "USD": Decimal("1"), "EUR": Decimal("0.92"), "GBP": Decimal("0.79"), "INR": Decimal("83.20"),
    "JPY": Decimal("149.50"), "AUD": Decimal("1.52"), "CAD": Decimal("1.36"), "SGD": Decimal("1.35"),
}
NAMES = {
    "USD": "US dollar", "EUR": "Euro", "GBP": "British pound", "INR": "Indian rupee",
    "JPY": "Japanese yen", "AUD": "Australian dollar", "CAD": "Canadian dollar", "SGD": "Singapore dollar",
}
Currency = Annotated[str, Field(min_length=3, max_length=3, description="ISO 4217 code, e.g. USD, EUR, INR")]


class Settings(BaseSettings):
    fx_api_key: str
    allowed_hosts: str = "localhost:*,127.0.0.1:*"
    port: int = 8000


class Conversion(BaseModel):
    amount: str
    from_currency: str
    to_currency: str
    rate: str
    converted: str
    rates_as_of: str


class CurrencyInfo(BaseModel):
    code: str
    name: str
    per_usd: str


def rate(from_code: str, to_code: str) -> Decimal:
    try:
        return USD_RATES[to_code.upper()] / USD_RATES[from_code.upper()]
    except KeyError as exc:
        raise ToolError(f"unsupported currency {exc.args[0]}; supported: {', '.join(USD_RATES)}") from exc


def create_server() -> MCPServer:
    mcp = MCPServer(name="fx-mcp", instructions="Currency conversion with a fixed demo rate table.")

    @mcp.tool(annotations=READ_ONLY)
    def convert_currency(amount: Annotated[Decimal, Field(ge=0)], from_currency: Currency, to_currency: Currency) -> Conversion:
        """Convert an amount between currencies (static demo rates)."""
        r = rate(from_currency, to_currency)
        converted = (amount * r).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return Conversion(
            amount=str(amount), from_currency=from_currency.upper(), to_currency=to_currency.upper(),
            rate=str(r.quantize(Decimal("0.000001"))), converted=str(converted), rates_as_of=RATES_AS_OF,
        )

    @mcp.tool(annotations=READ_ONLY)
    def list_rates(base: Currency = "USD") -> list[CurrencyInfo]:
        """List supported currencies and their rate against the base currency."""
        return [CurrencyInfo(code=c, name=NAMES[c], per_usd=str(rate(base, c).quantize(Decimal("0.0001")))) for c in USD_RATES]

    @mcp.tool(annotations=READ_ONLY)
    def search(query: Annotated[str, Field(min_length=1, max_length=50)]) -> list[CurrencyInfo]:
        """Find supported currencies by code or name."""
        q = query.lower()
        return [
            CurrencyInfo(code=c, name=NAMES[c], per_usd=str(USD_RATES[c]))
            for c in USD_RATES if q in c.lower() or q in NAMES[c].lower()
        ]

    return mcp


class ApiKeyMiddleware:
    """Header-style credential: callers must send `X-API-Key` (the gateway injects it); /healthz stays open."""

    def __init__(self, app: ASGIApp, api_key: str):
        self.app = app
        self.expected = api_key.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        presented = dict(scope["headers"]).get(b"x-api-key", b"") if scope["type"] == "http" else b""
        if scope["type"] == "http" and scope["path"] != "/healthz" and not hmac.compare_digest(presented, self.expected):
            await JSONResponse({"error": "invalid or missing X-API-Key"}, status_code=401)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def build_app(settings: Settings) -> ASGIApp:
    server = create_server()

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[h.strip() for h in settings.allowed_hosts.split(",") if h.strip()],
            allowed_origins=[],
        ),
    )
    return ApiKeyMiddleware(app, settings.fx_api_key)


def main() -> None:
    settings = Settings()
    uvicorn.run(build_app(settings), host="0.0.0.0", port=settings.port)
