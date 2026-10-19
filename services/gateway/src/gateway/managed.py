import json
import logging
import re
import secrets
import uuid
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from .auth import Principal
from .db import advisory_xact_lock
from .deps import current_principal, services
from .models import ManagedRelease, UpstreamServer, User, utcnow

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/stores", tags=["stores"])
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ACTIVE_STATUSES = ("provisioning", "ready", "failed", "deleting")


class StoreCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    slug: str = Field(min_length=1, max_length=20)

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
        if "__" in slug or not SLUG_RE.fullmatch(slug):
            raise ValueError("slug must contain lowercase letters, numbers and single hyphens only")
        if slug in {"gateway", "woo", "notes", "fx"}:
            raise ValueError(f"slug '{slug}' is reserved")
        return slug


class StoreOut(BaseModel):
    id: str
    name: str
    slug: str
    owner_email: str | None
    status: str
    reason: str
    url: str
    created_at: str | None
    ready_at: str | None
    built_in: bool = False


def generate_store_secrets() -> dict[str, str]:
    return {
        "mysqlRootPassword": secrets.token_urlsafe(32),
        "mysqlPassword": secrets.token_urlsafe(32),
        "wpAdminUser": "admin",
        "wpAdminPassword": secrets.token_urlsafe(32),
        "wpAdminEmail": "admin@urumi.local",
        "wooConsumerKey": f"ck_{secrets.token_hex(20)}",
        "wooConsumerSecret": f"cs_{secrets.token_hex(20)}",
        "wooMcpToken": secrets.token_urlsafe(32),
    }


def store_url(settings, slug: str, built_in: bool = False) -> str:
    subdomain = "store" if built_in else f"store-{slug}"
    return f"{settings.public_scheme}://{subdomain}.{settings.public_domain}"


def release_out(row: ManagedRelease, owner_email: str | None, settings) -> StoreOut:
    return StoreOut(
        id=str(row.id),
        name=row.display_name,
        slug=row.slug,
        owner_email=owner_email,
        status=row.status,
        reason=row.status_reason,
        url=store_url(settings, row.slug),
        created_at=row.created_at.isoformat() if row.created_at else None,
        ready_at=row.ready_at.isoformat() if row.ready_at else None,
    )


def built_in_store(settings) -> StoreOut:
    return StoreOut(
        id="store-main",
        name="Urumi Demo Store",
        slug="main",
        owner_email=None,
        status="ready",
        reason="built-in v1 store",
        url=store_url(settings, "main", built_in=True),
        created_at=None,
        ready_at=None,
        built_in=True,
    )


async def _owned_release(session, release_id: uuid.UUID, principal: Principal) -> tuple[ManagedRelease, str | None]:
    result = await session.execute(
        select(ManagedRelease, User.email)
        .outerjoin(User, ManagedRelease.owner_id == User.id)
        .where(ManagedRelease.id == release_id, ManagedRelease.kind == "store")
    )
    found = result.one_or_none()
    if found is None:
        raise HTTPException(404, "store not found")
    row, owner_email = found
    if principal.role != "admin" and row.owner_id != principal.user_id:
        raise HTTPException(404, "store not found")
    return row, owner_email


@router.get("", response_model=list[StoreOut])
async def list_stores(
    request: Request,
    include: Literal["active", "deleted"] = "active",
    principal: Principal = Depends(current_principal),
):
    svc = services(request)
    query = (
        select(ManagedRelease, User.email)
        .outerjoin(User, ManagedRelease.owner_id == User.id)
        .where(ManagedRelease.kind == "store")
        .order_by(ManagedRelease.created_at.desc())
    )
    query = query.where(ManagedRelease.status == "deleted") if include == "deleted" else query.where(
        ManagedRelease.status != "deleted"
    )
    if principal.role != "admin":
        query = query.where(ManagedRelease.owner_id == principal.user_id)
    async with svc.db.session() as session:
        rows = (await session.execute(query)).all()
    stores = [release_out(row, owner, svc.settings) for row, owner in rows]
    return stores if include == "deleted" else [built_in_store(svc.settings), *stores]


@router.post("", response_model=StoreOut, status_code=201)
async def create_store(
    data: StoreCreate,
    request: Request,
    response: Response,
    principal: Principal = Depends(current_principal),
):
    svc = services(request)
    now = utcnow()
    async with svc.db.session() as session:
        await advisory_xact_lock(session, "managed-release:store-quota")
        await advisory_xact_lock(session, f"upstream-slug:{data.slug}")
        existing = await session.scalar(
            select(ManagedRelease).where(
                ManagedRelease.slug == data.slug,
                ManagedRelease.status.in_(ACTIVE_STATUSES),
            )
        )
        if existing:
            if existing.owner_id != principal.user_id:
                raise HTTPException(409, f"slug '{data.slug}' is already in use")
            owner = await session.scalar(select(User.email).where(User.id == existing.owner_id))
            response.status_code = 200
            return release_out(existing, owner, svc.settings)
        if await session.scalar(select(UpstreamServer.id).where(UpstreamServer.slug == data.slug)):
            raise HTTPException(409, f"slug '{data.slug}' is already used by an MCP server")

        per_user = await session.scalar(
            select(func.count()).select_from(ManagedRelease).where(
                ManagedRelease.kind == "store",
                ManagedRelease.owner_id == principal.user_id,
                ManagedRelease.status.in_(ACTIVE_STATUSES),
            )
        )
        total = await session.scalar(
            select(func.count()).select_from(ManagedRelease).where(
                ManagedRelease.kind == "store", ManagedRelease.status.in_(ACTIVE_STATUSES)
            )
        )
        if (per_user or 0) >= svc.settings.stores_max_per_user:
            raise HTTPException(409, f"store limit reached ({svc.settings.stores_max_per_user} per user)")
        if (total or 0) >= svc.settings.stores_max_total:
            raise HTTPException(409, f"platform store limit reached ({svc.settings.stores_max_total} total)")

        encrypted = svc.box.encrypt(json.dumps(generate_store_secrets(), sort_keys=True))
        row = ManagedRelease(
            kind="store",
            slug=data.slug,
            display_name=data.name,
            namespace=f"store-{data.slug}",
            release=f"store-{data.slug}",
            owner_id=principal.user_id,
            desired="present",
            status="provisioning",
            status_reason="queued",
            spec={"profile": "tenant"},
            secrets_encrypted=encrypted,
            deadline_at=now + timedelta(minutes=svc.settings.provisioner_timeout_minutes),
        )
        session.add(row)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(409, f"slug '{data.slug}' was claimed by another request") from exc
        await session.refresh(row)
    log.info("%s requested store %s", principal.email, row.slug)
    return release_out(row, principal.email, svc.settings)


@router.post("/{release_id}/retry", response_model=StoreOut)
async def retry_store(
    release_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(current_principal),
):
    svc = services(request)
    async with svc.db.session() as session:
        row, owner = await _owned_release(session, release_id, principal)
        if row.status != "failed" or row.desired != "present":
            raise HTTPException(409, "only a failed store can be retried")
        row.status = "provisioning"
        row.status_reason = "queued for retry"
        row.attempts = 0
        row.lease_owner = None
        row.lease_until = None
        row.deadline_at = utcnow() + timedelta(minutes=svc.settings.provisioner_timeout_minutes)
        await session.commit()
        await session.refresh(row)
    log.info("%s retried store %s", principal.email, row.slug)
    return release_out(row, owner, svc.settings)


@router.delete("/{release_id}", response_model=StoreOut, status_code=202)
async def delete_store(
    release_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(current_principal),
):
    svc = services(request)
    async with svc.db.session() as session:
        row, owner = await _owned_release(session, release_id, principal)
        if row.status == "deleted":
            return release_out(row, owner, svc.settings)
        row.desired = "absent"
        row.status = "deleting"
        row.status_reason = "queued for deletion"
        row.deadline_at = utcnow() + timedelta(minutes=svc.settings.provisioner_teardown_timeout_minutes)
        row.lease_owner = None
        row.lease_until = None
        await session.commit()
        await session.refresh(row)
    log.info("%s requested deletion of store %s", principal.email, row.slug)
    return release_out(row, owner, svc.settings)
