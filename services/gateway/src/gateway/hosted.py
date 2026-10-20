import json
import re
import uuid
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .auth import Principal
from .catalog import SLUG_RE
from .db import advisory_xact_lock
from .deps import require_admin, services
from .models import ManagedRelease, UpstreamServer, utcnow
from .registry import FORBIDDEN_HEADERS, HEADER_NAME_RE, RegistryError

router = APIRouter(prefix="/api/hosted-servers", tags=["hosted-servers"])
ACTIVE_STATUSES = ("provisioning", "ready", "failed", "deleting")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})$")


class HostedCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    slug: str = Field(min_length=1, max_length=20)
    image: str = Field(min_length=3, max_length=500)
    port: int = Field(default=8000, ge=1024, le=65535)
    path: str = Field(default="/mcp", min_length=1, max_length=200)
    env: dict[str, str] = Field(default_factory=dict)
    secret_env: dict[str, str] = Field(default_factory=dict)
    auth_type: Literal["none", "bearer", "header"] = "none"
    auth_header_name: str | None = Field(default=None, max_length=100)
    credential_env: str | None = Field(default=None, max_length=100)
    internet_egress: bool = False
    size: Literal["small", "medium"] = "small"

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("name must not be blank")
        return cleaned

    @field_validator("slug")
    @classmethod
    def valid_slug(cls, value: str) -> str:
        slug = value.strip().lower()
        if not SLUG_RE.fullmatch(slug) or slug in {"gateway", "woo", "notes", "fx"}:
            raise ValueError("slug must be unreserved lowercase letters, numbers and single hyphens")
        return slug

    @field_validator("image")
    @classmethod
    def valid_image(cls, value: str) -> str:
        image = value.strip()
        if not IMAGE_RE.fullmatch(image) or image.rsplit(":", 1)[1].lower() == "latest":
            raise ValueError("image must be lowercase, include an explicit non-latest tag, and contain no digest")
        return image

    @field_validator("path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        path = value.strip()
        if not path.startswith("/") or ".." in path or any(char.isspace() for char in path) or "?" in path or "#" in path:
            raise ValueError("path must be an absolute URL path without whitespace, query or fragment")
        return path

    @field_validator("env", "secret_env")
    @classmethod
    def valid_env(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 20:
            raise ValueError("at most 20 environment variables are allowed")
        for key, item in value.items():
            if not ENV_NAME_RE.fullmatch(key) or len(item) > 4096:
                raise ValueError("environment names must be shell identifiers and values at most 4096 characters")
        return value

    @model_validator(mode="after")
    def valid_auth(self):
        overlap = set(self.env) & set(self.secret_env)
        if overlap:
            raise ValueError(f"environment variables cannot be both plain and secret: {', '.join(sorted(overlap))}")
        if self.auth_type == "none":
            if self.credential_env or self.auth_header_name:
                raise ValueError("gateway auth fields must be empty when authentication is none")
            return self
        if not self.credential_env or self.credential_env not in self.secret_env:
            raise ValueError("credential_env must name a secret environment variable")
        if self.auth_type == "header" and (
            not self.auth_header_name
            or not HEADER_NAME_RE.fullmatch(self.auth_header_name)
            or self.auth_header_name.lower() in FORBIDDEN_HEADERS
        ):
            raise ValueError("auth_header_name must be a safe custom HTTP header")
        if self.auth_type == "bearer" and self.auth_header_name:
            raise ValueError("auth_header_name is only valid for header authentication")
        return self


class HostedOut(BaseModel):
    id: str
    name: str
    slug: str
    image: str
    port: int
    path: str
    env: dict[str, str]
    secret_env_names: list[str]
    auth_type: str
    auth_header_name: str | None
    credential_env: str | None
    internet_egress: bool
    size: str
    status: str
    reason: str
    created_at: str | None
    ready_at: str | None


def release_out(row: ManagedRelease, box) -> HostedOut:
    spec = row.spec
    secret_names = sorted(json.loads(box.decrypt(row.secrets_encrypted)))
    return HostedOut(
        id=str(row.id),
        name=row.display_name,
        slug=row.slug,
        image=spec["image"],
        port=spec["port"],
        path=spec["path"],
        env=spec.get("env", {}),
        secret_env_names=secret_names,
        auth_type=spec["auth_type"],
        auth_header_name=spec.get("auth_header_name"),
        credential_env=spec.get("credential_env"),
        internet_egress=spec.get("internet_egress", False),
        size=spec.get("size", "small"),
        status=row.status,
        reason=row.status_reason,
        created_at=row.created_at.isoformat() if row.created_at else None,
        ready_at=row.ready_at.isoformat() if row.ready_at else None,
    )


async def _release(session, release_id: uuid.UUID) -> ManagedRelease:
    row = await session.scalar(
        select(ManagedRelease).where(ManagedRelease.id == release_id, ManagedRelease.kind == "hosted_mcp")
    )
    if row is None:
        raise HTTPException(404, "hosted MCP deployment not found")
    return row


def _allowed_image(image: str, prefixes: list[str]) -> bool:
    return any(image.startswith(f"{prefix.rstrip('/')}/") for prefix in prefixes)


@router.get("", response_model=list[HostedOut])
async def list_hosted(
    request: Request,
    include: Literal["active", "deleted"] = "active",
    _admin: Principal = Depends(require_admin),
):
    svc = services(request)
    query = select(ManagedRelease).where(ManagedRelease.kind == "hosted_mcp")
    query = query.where(ManagedRelease.status == "deleted") if include == "deleted" else query.where(
        ManagedRelease.status != "deleted"
    )
    async with svc.db.session() as session:
        rows = list((await session.scalars(query.order_by(ManagedRelease.created_at.desc()))).all())
    return [release_out(row, svc.box) for row in rows]


@router.post("", response_model=HostedOut, status_code=201)
async def create_hosted(
    data: HostedCreate,
    request: Request,
    response: Response,
    admin: Principal = Depends(require_admin),
):
    svc = services(request)
    if not _allowed_image(data.image, svc.settings.hosted_mcp_allowed_registry_list):
        raise HTTPException(422, "image registry or namespace is not allowed")
    async with svc.db.session() as session:
        await advisory_xact_lock(session, f"upstream-slug:{data.slug}")
        existing = await session.scalar(
            select(ManagedRelease).where(ManagedRelease.slug == data.slug, ManagedRelease.status.in_(ACTIVE_STATUSES))
        )
        if existing:
            if existing.kind != "hosted_mcp":
                raise HTTPException(409, f"slug '{data.slug}' is already in use")
            response.status_code = 200
            return release_out(existing, svc.box)
        if await session.scalar(select(UpstreamServer.id).where(UpstreamServer.slug == data.slug)):
            raise HTTPException(409, f"slug '{data.slug}' is already used by an MCP server")

        spec = data.model_dump(exclude={"name", "slug", "secret_env"})
        row = ManagedRelease(
            kind="hosted_mcp",
            slug=data.slug,
            display_name=data.name,
            namespace=f"mcp-{data.slug}",
            release=f"mcp-{data.slug}",
            owner_id=admin.user_id,
            desired="present",
            status="provisioning",
            status_reason="queued",
            spec=spec,
            secrets_encrypted=svc.box.encrypt(json.dumps(data.secret_env, sort_keys=True)),
            deadline_at=utcnow() + timedelta(minutes=svc.settings.provisioner_timeout_minutes),
        )
        session.add(row)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(409, f"slug '{data.slug}' was claimed by another request") from exc
        await session.refresh(row)
    return release_out(row, svc.box)


@router.post("/{release_id}/retry", response_model=HostedOut)
async def retry_hosted(
    release_id: uuid.UUID,
    request: Request,
    _admin: Principal = Depends(require_admin),
):
    svc = services(request)
    async with svc.db.session() as session:
        row = await _release(session, release_id)
        if row.status != "failed" or row.desired != "present":
            raise HTTPException(409, "only a failed hosted MCP deployment can be retried")
        row.status = "provisioning"
        row.status_reason = "queued for retry"
        row.attempts = 0
        row.lease_owner = None
        row.lease_until = None
        row.deadline_at = utcnow() + timedelta(minutes=svc.settings.provisioner_timeout_minutes)
        await session.commit()
        await session.refresh(row)
    return release_out(row, svc.box)


@router.delete("/{release_id}", response_model=HostedOut, status_code=202)
async def delete_hosted(
    release_id: uuid.UUID,
    request: Request,
    _admin: Principal = Depends(require_admin),
):
    svc = services(request)
    async with svc.db.session() as session:
        row = await _release(session, release_id)
        if row.status == "deleted":
            return release_out(row, svc.box)
    before = svc.catalog.visibility_signature()
    try:
        await svc.registry.queue_managed_deletion(
            release_id,
            utcnow() + timedelta(minutes=svc.settings.provisioner_teardown_timeout_minutes),
        )
    except RegistryError as exc:
        raise HTTPException(409, str(exc)) from exc
    await svc.monitor.notify_if_changed(before)
    async with svc.db.session() as session:
        row = await _release(session, release_id)
    return release_out(row, svc.box)
