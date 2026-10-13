import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, timedelta

from sqlalchemy import select
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .db import Database
from .hub import SessionHub
from .models import ApiKey, User, utcnow
from .security import API_KEY_PREFIX, hash_token


@dataclass(frozen=True)
class Principal:
    """Who is calling: resolved fresh on every request, so revocation takes effect immediately."""

    user_id: uuid.UUID
    email: str
    role: str
    api_key_id: uuid.UUID | None
    source: str


Resolver = Callable[[str], Awaitable[Principal | None]]


def bearer_token(headers: Headers) -> str | None:
    scheme, _, token = headers.get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


def api_key_resolver(db: Database) -> Resolver:
    async def resolve(token: str) -> Principal | None:
        if not token.startswith(API_KEY_PREFIX):
            return None
        async with db.session() as session:
            row = (
                await session.execute(
                    select(ApiKey, User)
                    .join(User, ApiKey.user_id == User.id)
                    .where(ApiKey.key_hash == hash_token(token), ApiKey.revoked_at.is_(None), User.status == "active")
                )
            ).first()
            if row is None:
                return None
            key, user = row
            now = utcnow()
            last = key.last_used_at.replace(tzinfo=UTC) if key.last_used_at and key.last_used_at.tzinfo is None else key.last_used_at
            if last is None or now - last > timedelta(minutes=1):
                key.last_used_at = now
                await session.commit()
            return Principal(user.id, user.email, user.role, key.id, "mcp")

    return resolve


def unauthorized(description: str) -> JSONResponse:
    return JSONResponse(
        {"error": "invalid_token", "error_description": description},
        status_code=401,
        headers={"WWW-Authenticate": f'Bearer error="invalid_token", error_description="{description}"'},
    )


class McpAuthMiddleware:
    """Authenticates every /mcp HTTP request and attaches the Principal to request.state.

    MCP handlers read it from ctx.request.state.principal (verified for both protocol generations).
    """

    def __init__(self, app: ASGIApp, resolvers: list[Resolver], hub: SessionHub):
        self.app = app
        self.resolvers = resolvers
        self.hub = hub

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/mcp"):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        token = bearer_token(headers)
        principal = None
        if token:
            for resolve in self.resolvers:
                principal = await resolve(token)
                if principal:
                    break
        if principal is None:
            await unauthorized("Send your gateway API key as 'Authorization: Bearer mcpgw_...'")(scope, receive, send)
            return
        if scope["method"] == "DELETE":
            self.hub.forget(headers.get("mcp-session-id"))
        scope.setdefault("state", {})["principal"] = principal
        await self.app(scope, receive, send)
