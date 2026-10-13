import hmac

from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import Settings
from .tools import create_server
from .woo_client import WooClient


class BearerTokenMiddleware:
    """Rejects any request without the shared gateway token; /healthz stays open for kubelet probes."""

    def __init__(self, app: ASGIApp, token: str, open_paths: frozenset[str] = frozenset({"/healthz"})):
        self.app = app
        self.expected = f"Bearer {token}".encode()
        self.open_paths = open_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] not in self.open_paths:
            supplied = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(supplied, self.expected):
                response = JSONResponse(
                    {"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


async def healthz(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def build_app(settings: Settings, woo: WooClient | None = None) -> ASGIApp:
    woo = woo or WooClient(
        base_url=settings.woo_base_url,
        store_host=settings.woo_store_host,
        consumer_key=settings.woo_consumer_key,
        consumer_secret=settings.woo_consumer_secret,
        timeout=settings.woo_timeout_seconds,
    )
    server = create_server(woo, settings.woo_public_url)
    server.custom_route("/healthz", methods=["GET"])(healthz)
    # Stateless + JSON: every request stands alone, so any replica can answer and restarts lose nothing.
    # Host allow-list (DNS-rebinding protection) accepts only the in-cluster service names we expect.
    mcp_app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.allowed_host_list,
            allowed_origins=[],
        ),
    )
    return BearerTokenMiddleware(mcp_app, settings.mcp_auth_token)
