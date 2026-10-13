import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, timedelta

from sqlalchemy import select
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .audit import Auditor, args_preview
from .catalog import SEPARATOR
from .db import Database
from .hub import SessionHub
from .models import ApiKey, User, utcnow
from .security import API_KEY_PREFIX, hash_token
from .sessions import read_token

log = logging.getLogger(__name__)


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


def chat_token_resolver(db: Database, secret: str) -> Resolver:
    """Short-lived token minted by /api/chat: the agent calls tools as the logged-in user (source=chat)."""

    async def resolve(token: str) -> Principal | None:
        if token.startswith(API_KEY_PREFIX):
            return None
        user_id = read_token(token, secret, "chat")
        if user_id is None:
            return None
        async with db.session() as session:
            user = await session.get(User, user_id)
        if user is None or user.status != "active":
            return None
        return Principal(user.id, user.email, user.role, None, "chat")

    return resolve


RejectedCall = Callable[[str, dict], Awaitable[None]]


def rejected_call_auditor(db: Database, auditor: Auditor) -> RejectedCall:
    """Audits a tools/call made with a key that exists but no longer works (revoked, or user disabled).
    Unknown tokens are not audited, so random requests can't flood the audit log."""

    async def record(token: str, message: dict) -> None:
        if not token.startswith(API_KEY_PREFIX) or message.get("method") != "tools/call":
            return
        async with db.session() as session:
            row = (
                await session.execute(
                    select(ApiKey, User).join(User, ApiKey.user_id == User.id).where(ApiKey.key_hash == hash_token(token))
                )
            ).first()
        if row is None:
            return
        key, user = row
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        name = str(params.get("name", ""))[:130]
        slug, _, tool = name.partition(SEPARATOR)
        await auditor.record(
            user_id=user.id, user_email=user.email, api_key_id=key.id, source="mcp", server_slug=slug[:20] or "-",
            tool_name=tool[:100] or name[:100], exposed_name=name, duration_ms=0, outcome="denied",
            error_message="API key revoked" if key.revoked_at else "user is disabled",
            request_id=str(message.get("id", ""))[:64],
            args_preview=args_preview(params.get("arguments") if isinstance(params.get("arguments"), dict) else None),
        )

    return record


async def read_body(receive: Receive, limit: int = 1_000_000) -> bytes:
    body = b""
    while len(body) <= limit:
        message = await receive()
        body += message.get("body", b"")
        if not message.get("more_body"):
            break
    return body


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

    def __init__(self, app: ASGIApp, resolvers: list[Resolver], hub: SessionHub, on_rejected: RejectedCall | None = None):
        self.app = app
        self.resolvers = resolvers
        self.hub = hub
        self.on_rejected = on_rejected

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
            if token and self.on_rejected and scope["method"] == "POST":
                await self._audit_rejection(token, receive)
            await unauthorized("Send your gateway API key as 'Authorization: Bearer mcpgw_...'")(scope, receive, send)
            return
        if scope["method"] == "DELETE":
            self.hub.forget(headers.get("mcp-session-id"))
        scope.setdefault("state", {})["principal"] = principal
        await self.app(scope, receive, send)

    async def _audit_rejection(self, token: str, receive: Receive) -> None:
        """Best effort: the caller gets its 401 whether or not the audit row could be written."""
        try:
            message = json.loads(await read_body(receive))
        except ValueError:
            log.debug("rejected /mcp request body is not JSON; nothing to audit")
            return
        if not isinstance(message, dict):
            return
        try:
            await self.on_rejected(token, message)
        except Exception:
            log.exception("could not audit a rejected /mcp call")
