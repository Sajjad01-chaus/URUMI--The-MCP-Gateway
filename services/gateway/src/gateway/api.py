import logging
import math
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from prometheus_client import CONTENT_TYPE_LATEST
from pydantic import BaseModel
from sqlalchemy import select, text
from starlette.responses import Response

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
    is_managed: bool
    managed_kind: str | None
    last_error: str | None
    last_checked_at: datetime | None
    tools: list[ToolOut]


def server_out(svc, state: UpstreamState) -> ServerOut:
    return ServerOut(
        id=state.id, slug=state.slug, name=state.name, url=state.url, enabled=state.enabled,
        status=state.display_status, auth_type=state.auth_type, auth_header_name=state.auth_header_name,
        has_credential=state.credential_encrypted is not None, timeout_seconds=state.timeout_seconds,
        is_managed=state.is_managed, managed_kind=state.managed_kind,
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


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request):
    svc = services(request)
    for state in svc.catalog.servers.values():
        svc.metrics.upstream_up.labels(state.slug).set(state.is_routable)
    return Response(svc.metrics.render(svc.hub.session_count), media_type=CONTENT_TYPE_LATEST)


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
    current = find(svc, server_id)
    if current.is_managed and patch.model_fields_set - {"enabled"}:
        raise HTTPException(409, "managed servers can only be enabled or disabled here; edit the store from Stores")
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
    current = find(svc, server_id)
    if current.is_managed:
        raise HTTPException(409, "delete managed servers from Stores")
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


class ServerStats(BaseModel):
    slug: str
    name: str
    status: str
    status_since: datetime | None
    calls: int
    error_rate: float
    p50_ms: int
    p95_ms: int
    last_error: str | None


class ToolStats(BaseModel):
    name: str
    calls: int
    errors: int


class UserStats(BaseModel):
    email: str
    total: int
    mcp: int
    chat: int


class StatsOut(BaseModel):
    window: Literal["15m", "1h", "24h"]
    generated_at: datetime
    total_calls: int
    total_errors: int
    servers: list[ServerStats]
    top_tools: list[ToolStats]
    top_error_tools: list[ToolStats]
    users: list[UserStats]


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


def _percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


@router.get("/api/stats", response_model=StatsOut)
async def stats(
    request: Request,
    window: Literal["15m", "1h", "24h"] = "1h",
    _: Principal = Depends(require_admin),
):
    """Aggregate the durable audit log; Prometheus is not required for the dashboard."""
    duration = {"15m": timedelta(minutes=15), "1h": timedelta(hours=1), "24h": timedelta(hours=24)}[window]
    cutoff = datetime.now(UTC) - duration
    async with services(request).db.session() as session:
        rows = list(
            (await session.scalars(select(AuditLog).where(AuditLog.ts >= cutoff).order_by(AuditLog.id.desc()))).all()
        )

    by_server: dict[str, list[AuditLog]] = defaultdict(list)
    tool_calls: Counter[str] = Counter()
    tool_errors: Counter[str] = Counter()
    user_sources: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        by_server[row.server_slug].append(row)
        tool_calls[row.exposed_name] += 1
        user_sources[row.user_email][row.source] += 1
        if row.outcome != "success":
            tool_errors[row.exposed_name] += 1

    svc = services(request)
    server_rows = []
    for state in sorted(svc.catalog.servers.values(), key=lambda item: item.slug):
        calls = by_server.get(state.slug, [])
        errors = [row for row in calls if row.outcome != "success"]
        last_error = next((row.error_message for row in calls if row.outcome != "success" and row.error_message), None)
        server_rows.append(
            ServerStats(
                slug=state.slug,
                name=state.name,
                status=state.display_status,
                status_since=state.status_since or state.last_checked_at,
                calls=len(calls),
                error_rate=round((len(errors) / len(calls) * 100) if calls else 0, 1),
                p50_ms=_percentile([row.duration_ms for row in calls], 0.50),
                p95_ms=_percentile([row.duration_ms for row in calls], 0.95),
                last_error=last_error or state.last_error,
            )
        )

    def tool_row(name: str) -> ToolStats:
        return ToolStats(name=name, calls=tool_calls[name], errors=tool_errors[name])

    return StatsOut(
        window=window,
        generated_at=datetime.now(UTC),
        total_calls=len(rows),
        total_errors=sum(row.outcome != "success" for row in rows),
        servers=server_rows,
        top_tools=[tool_row(name) for name, _ in tool_calls.most_common(10)],
        top_error_tools=[tool_row(name) for name, _ in tool_errors.most_common(5)],
        users=[
            UserStats(email=email, total=sum(counts.values()), mcp=counts["mcp"], chat=counts["chat"])
            for email, counts in sorted(user_sources.items(), key=lambda item: (-sum(item[1].values()), item[0]))
        ],
    )
