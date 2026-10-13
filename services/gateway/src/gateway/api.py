import logging
import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select, text

from .auth import Principal
from .catalog import UpstreamState, default_min_role, exposed_name
from .deps import current_principal, require_admin, services
from .models import AuditLog
from .registry import RegistryError, ServerIn, ServerPatch

log = logging.getLogger(__name__)
router = APIRouter()


class ToolOut(BaseModel):
    name: str
    exposed_name: str
    description: str
    min_role: str
    read_only: bool


class ServerOut(BaseModel):
    """Never contains the credential: only whether one is set."""

    id: uuid.UUID
    slug: str
    name: str
    url: str
    enabled: bool
    status: str
    auth_type: str
    auth_header_name: str | None
    has_credential: bool
    timeout_seconds: int
    last_error: str | None
    last_checked_at: datetime | None
    tools: list[ToolOut]


def server_out(svc, state: UpstreamState) -> ServerOut:
    return ServerOut(
        id=state.id, slug=state.slug, name=state.name, url=state.url, enabled=state.enabled,
        status=state.display_status, auth_type=state.auth_type, auth_header_name=state.auth_header_name,
        has_credential=state.credential_encrypted is not None, timeout_seconds=state.timeout_seconds,
        last_error=state.last_error, last_checked_at=state.last_checked_at,
        tools=[
            ToolOut(
                name=t.name, exposed_name=exposed_name(state.slug, t.name), description=t.description or "",
                min_role=svc.catalog.min_role(state.slug, t),
                read_only=bool(t.annotations and t.annotations.read_only_hint),
            )
            for t in state.tools
        ],
    )


def find(svc, server_id: uuid.UUID) -> UpstreamState:
    state = next((s for s in svc.catalog.servers.values() if s.id == server_id), None)
    if state is None:
        raise HTTPException(404, "server not found")
    return state


@router.get("/healthz")
async def healthz():
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request):
    try:
        async with services(request).db.session() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:
        log.warning("readiness check failed: %s", exc)
        raise HTTPException(503, "database unavailable") from exc
    return {"status": "ready"}


@router.get("/api/servers", response_model=list[ServerOut])
async def list_servers(request: Request, _: Principal = Depends(current_principal)):
    svc = services(request)
    return [server_out(svc, s) for s in sorted(svc.catalog.servers.values(), key=lambda s: s.slug)]


@router.post("/api/servers", response_model=ServerOut, status_code=201)
async def create_server(data: ServerIn, request: Request, admin: Principal = Depends(require_admin)):
    svc = services(request)
    try:
        state = await svc.registry.create(data)
    except RegistryError as exc:
        log.info("%s: server registration rejected: %s", admin.email, exc)
        raise HTTPException(422, str(exc)) from exc
    log.info("%s registered server %s -> %s", admin.email, state.slug, state.url)
    await svc.monitor.refresh(state.slug)
    return server_out(svc, state)


@router.patch("/api/servers/{server_id}", response_model=ServerOut)
async def update_server(server_id: uuid.UUID, patch: ServerPatch, request: Request, admin: Principal = Depends(require_admin)):
    svc = services(request)
    find(svc, server_id)
    before = svc.catalog.visibility_signature()
    try:
        state = await svc.registry.update(server_id, patch)
    except RegistryError as exc:
        raise HTTPException(422, str(exc)) from exc
    # Field names only: the credential value must never reach the logs.
    log.info("%s updated server %s (%s)", admin.email, state.slug, ", ".join(sorted(patch.model_dump(exclude_unset=True))))
    await svc.monitor.refresh(state.slug)
    await svc.monitor.notify_if_changed(before)
    return server_out(svc, state)


@router.delete("/api/servers/{server_id}", status_code=204)
async def delete_server(server_id: uuid.UUID, request: Request, admin: Principal = Depends(require_admin)):
    svc = services(request)
    find(svc, server_id)
    before = svc.catalog.visibility_signature()
    slug = await svc.registry.delete(server_id)
    log.info("%s deleted server %s", admin.email, slug)
    await svc.monitor.notify_if_changed(before)


@router.post("/api/servers/{server_id}/test", response_model=ServerOut)
async def test_server(server_id: uuid.UUID, request: Request, _: Principal = Depends(require_admin)):
    svc = services(request)
    state = find(svc, server_id)
    await svc.monitor.refresh(state.slug)
    return server_out(svc, state)


class CatalogToolOut(BaseModel):
    exposed_name: str
    server_slug: str
    server_name: str
    tool: str
    description: str
    min_role: str
    read_only: bool
    input_schema: dict[str, Any]


class MetaOut(BaseModel):
    mcp_url: str


@router.get("/api/tools", response_model=list[CatalogToolOut])
async def list_tools(request: Request, principal: Principal = Depends(current_principal)):
    """Exactly what this user's MCP client sees in tools/list (same routable + role filter)."""
    catalog = services(request).catalog
    return [
        CatalogToolOut(
            exposed_name=exposed_name(s.slug, t.name), server_slug=s.slug, server_name=s.name, tool=t.name,
            description=t.description or "", min_role=catalog.min_role(s.slug, t),
            read_only=bool(t.annotations and t.annotations.read_only_hint), input_schema=t.input_schema,
        )
        for s in sorted(catalog.servers.values(), key=lambda s: s.slug)
        if s.is_routable
        for t in s.tools
        if catalog.allowed(principal.role, s.slug, t)
    ]


@router.get("/api/meta", response_model=MetaOut)
async def meta(request: Request, _: Principal = Depends(current_principal)):
    return MetaOut(mcp_url=services(request).settings.public_mcp_url)


class PermissionOut(BaseModel):
    server_slug: str
    tool: str
    exposed_name: str
    read_only: bool
    default_role: str
    override: str | None
    min_role: str


class PermissionIn(BaseModel):
    min_role: Literal["member", "admin"] | None


class AuditOut(BaseModel):
    id: int
    ts: datetime
    user_email: str
    api_key_id: uuid.UUID | None
    source: str
    server_slug: str
    tool_name: str
    exposed_name: str
    duration_ms: int
    outcome: str
    error_message: str | None
    client_name: str | None
    args_preview: dict[str, Any] | None


class AuditPage(BaseModel):
    items: list[AuditOut]
    next_cursor: int | None


@router.get("/api/permissions", response_model=list[PermissionOut])
async def list_permissions(request: Request, _: Principal = Depends(require_admin)):
    catalog = services(request).catalog
    return [
        PermissionOut(
            server_slug=s.slug, tool=t.name, exposed_name=exposed_name(s.slug, t.name),
            read_only=bool(t.annotations and t.annotations.read_only_hint), default_role=default_min_role(t),
            override=catalog.policies.get((s.slug, t.name)), min_role=catalog.min_role(s.slug, t),
        )
        for s in sorted(catalog.servers.values(), key=lambda s: s.slug)
        for t in s.tools
    ]


@router.put("/api/permissions/{slug}/{tool}", response_model=PermissionOut)
async def set_permission(slug: str, tool: str, data: PermissionIn, request: Request, admin: Principal = Depends(require_admin)):
    """min_role=null removes the override, so the annotation default applies again."""
    svc = services(request)
    state = svc.catalog.servers.get(slug)
    match = next((t for t in state.tools if t.name == tool), None) if state else None
    if match is None:
        raise HTTPException(404, f"no tool '{tool}' on server '{slug}'")
    await svc.registry.set_policy(state, tool, data.min_role, admin.user_id)
    log.info("%s set min role of %s__%s to %s", admin.email, slug, tool, data.min_role or "default")
    # Who can see the tool changed, so connected clients should re-list.
    await svc.hub.notify_tools_changed()
    return PermissionOut(
        server_slug=slug, tool=tool, exposed_name=exposed_name(slug, tool),
        read_only=bool(match.annotations and match.annotations.read_only_hint), default_role=default_min_role(match),
        override=data.min_role, min_role=svc.catalog.min_role(slug, match),
    )


@router.get("/api/audit", response_model=AuditPage)
async def list_audit(
    request: Request,
    principal: Principal = Depends(current_principal),
    user: str | None = None,
    server: str | None = None,
    tool: str | None = None,
    outcome: str | None = None,
    source: str | None = None,
    since: datetime | None = Query(None, alias="from"),
    until: datetime | None = Query(None, alias="to"),
    cursor: int | None = None,
    limit: int = Query(50, ge=1, le=200),
):
    """Newest first. Admins see everyone; members only their own calls. `cursor` = last id of the previous page."""
    query = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit + 1)
    if principal.role != "admin":
        query = query.where(AuditLog.user_id == principal.user_id)
    elif user:
        query = query.where(AuditLog.user_email == user.strip().lower())
    filters = {AuditLog.server_slug: server, AuditLog.exposed_name: tool, AuditLog.outcome: outcome, AuditLog.source: source}
    for column, value in filters.items():
        if value:
            query = query.where(column == value)
    if since:
        query = query.where(AuditLog.ts >= since)
    if until:
        query = query.where(AuditLog.ts < until)
    if cursor:
        query = query.where(AuditLog.id < cursor)
    async with services(request).db.session() as session:
        rows = list((await session.scalars(query)).all())
    more = len(rows) > limit
    rows = rows[:limit]
    return AuditPage(
        items=[AuditOut.model_validate(r, from_attributes=True) for r in rows],
        next_cursor=rows[-1].id if more else None,
    )
