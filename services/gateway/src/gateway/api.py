import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text

from .auth import Principal, bearer_token
from .catalog import UpstreamState, exposed_name
from .registry import RegistryError, ServerIn, ServerPatch

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


def services(request: Request):
    return request.app.state.services


async def current_principal(request: Request) -> Principal:
    token = bearer_token(request.headers)
    principal = await services(request).resolve_api_key(token) if token else None
    if principal is None:
        raise HTTPException(401, "authentication required")
    return principal


async def require_admin(principal: Principal = Depends(current_principal)) -> Principal:
    if principal.role != "admin":
        raise HTTPException(403, "admin role required")
    return principal


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
        raise HTTPException(503, "database unavailable") from exc
    return {"status": "ready"}


@router.get("/api/servers", response_model=list[ServerOut])
async def list_servers(request: Request, _: Principal = Depends(current_principal)):
    svc = services(request)
    return [server_out(svc, s) for s in sorted(svc.catalog.servers.values(), key=lambda s: s.slug)]


@router.post("/api/servers", response_model=ServerOut, status_code=201)
async def create_server(data: ServerIn, request: Request, _: Principal = Depends(require_admin)):
    svc = services(request)
    try:
        state = await svc.registry.create(data)
    except RegistryError as exc:
        raise HTTPException(422, str(exc)) from exc
    await svc.monitor.refresh(state.slug)
    return server_out(svc, state)


@router.patch("/api/servers/{server_id}", response_model=ServerOut)
async def update_server(server_id: uuid.UUID, patch: ServerPatch, request: Request, _: Principal = Depends(require_admin)):
    svc = services(request)
    find(svc, server_id)
    before = svc.catalog.visibility_signature()
    try:
        state = await svc.registry.update(server_id, patch)
    except RegistryError as exc:
        raise HTTPException(422, str(exc)) from exc
    await svc.monitor.refresh(state.slug)
    await svc.monitor.notify_if_changed(before)
    return server_out(svc, state)


@router.delete("/api/servers/{server_id}", status_code=204)
async def delete_server(server_id: uuid.UUID, request: Request, _: Principal = Depends(require_admin)):
    svc = services(request)
    find(svc, server_id)
    before = svc.catalog.visibility_signature()
    await svc.registry.delete(server_id)
    await svc.monitor.notify_if_changed(before)


@router.post("/api/servers/{server_id}/test", response_model=ServerOut)
async def test_server(server_id: uuid.UUID, request: Request, _: Principal = Depends(require_admin)):
    svc = services(request)
    state = find(svc, server_id)
    await svc.monitor.refresh(state.slug)
    return server_out(svc, state)


@router.get("/api/tools")
async def list_tools(request: Request, principal: Principal = Depends(current_principal)):
    tools = services(request).catalog.visible_tools(principal.role)
    return [{"name": t.name, "description": t.description} for t in tools]
