import logging
import os
import re
import uuid
from datetime import datetime
from typing import Literal

from mcp import types
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from .catalog import SLUG_RE, Catalog, UpstreamState
from .config import Settings
from .db import Database, advisory_xact_lock
from .models import ManagedRelease, ToolPolicy, UpstreamServer, User, utcnow
from .security import CredentialBox, hash_password
from .ssrf import HostResolutionError, check_upstream_url
from .upstream import Target

log = logging.getLogger(__name__)

HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9-]{1,100}$")
FORBIDDEN_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "mcp-session-id", "mcp-protocol-version"}


class RegistryError(ValueError):
    """Invalid server registration; message is shown to the admin."""


class ServerIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    slug: str = Field(min_length=1, max_length=20)
    url: str = Field(min_length=1, max_length=500)
    auth_type: Literal["none", "bearer", "header"] = "none"
    auth_header_name: str | None = None
    credential: str | None = Field(default=None, max_length=4000)
    timeout_seconds: int = Field(default=20, ge=1, le=120)
    enabled: bool = True


class ServerPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    url: str | None = Field(default=None, min_length=1, max_length=500)
    auth_type: Literal["none", "bearer", "header"] | None = None
    auth_header_name: str | None = None
    credential: str | None = Field(default=None, max_length=4000, description="Omit to keep, empty string to clear")
    timeout_seconds: int | None = Field(default=None, ge=1, le=120)
    enabled: bool | None = None


def to_state(row: UpstreamServer) -> UpstreamState:
    state = UpstreamState(
        id=row.id, slug=row.slug, name=row.name, url=row.url, enabled=row.enabled,
        auth_type=row.auth_type, auth_header_name=row.auth_header_name,
        credential_encrypted=row.credential_encrypted, timeout_seconds=row.timeout_seconds,
        is_managed=row.is_managed,
    )
    if row.tools_cache:
        state.tools = [types.Tool.model_validate(t) for t in row.tools_cache]
    return state


def target_for(state: UpstreamState, box: CredentialBox) -> Target:
    credential = box.decrypt(state.credential_encrypted) if state.credential_encrypted else None
    return Target(state.slug, state.url, state.auth_type, state.auth_header_name, credential, state.timeout_seconds)


def validate_auth(auth_type: str, header_name: str | None, credential: str | None, has_existing: bool) -> None:
    if auth_type == "header" and (
        not header_name or not HEADER_NAME_RE.match(header_name) or header_name.lower() in FORBIDDEN_HEADERS
    ):
        raise RegistryError("auth_header_name must be a valid custom header name (e.g. X-API-Key)")
    if auth_type in ("bearer", "header") and not credential and not has_existing:
        raise RegistryError(f"auth_type '{auth_type}' needs a credential")


async def validate_url(url: str, settings: Settings) -> None:
    try:
        await check_upstream_url(url, settings.allowed_internal_host_patterns)
    except HostResolutionError:
        log.info("registering %s although it does not resolve yet; it will show as unreachable", url)
    except ValueError as exc:
        raise RegistryError(str(exc)) from exc


class Registry:
    """Source of truth for upstreams (Postgres) and its in-memory mirror (the Catalog)."""

    def __init__(self, db: Database, box: CredentialBox, settings: Settings, catalog: Catalog):
        self.db, self.box, self.settings, self.catalog = db, box, settings, catalog

    async def load(self) -> None:
        async with self.db.session() as session:
            for row in (await session.scalars(select(UpstreamServer))).all():
                self.catalog.upsert(to_state(row))
            servers = {s.id: s.slug for s in self.catalog.servers.values()}
            for policy in (await session.scalars(select(ToolPolicy))).all():
                if policy.server_id in servers:
                    self.catalog.policies[(servers[policy.server_id], policy.tool_name)] = policy.min_role

    async def bootstrap_admin(self) -> None:
        email, password = self.settings.bootstrap_admin_email, self.settings.bootstrap_admin_password
        if not email or not password:
            return
        async with self.db.session() as session:
            if await session.scalar(select(func.count()).select_from(User)):
                return
            session.add(User(email=email.lower(), name="Admin", role="admin", password_hash=hash_password(password)))
            await session.commit()
            log.info("created bootstrap admin %s", email)

    async def seed(self) -> None:
        """Register servers from trusted Helm config once; later admin edits are never overwritten."""
        async with self.db.session() as session:
            existing = set((await session.scalars(select(UpstreamServer.slug))).all())
            for seed in self.settings.seed_server_list:
                if seed.slug in existing:
                    continue
                credential = os.environ.get(seed.credential_env, "") if seed.credential_env else ""
                row = UpstreamServer(
                    slug=seed.slug, name=seed.name, url=seed.url, auth_type=seed.auth_type,
                    auth_header_name=seed.auth_header_name, timeout_seconds=seed.timeout_seconds,
                    credential_encrypted=self.box.encrypt(credential) if credential else None, is_seeded=True,
                )
                session.add(row)
                await session.flush()
                # Seeded overrides are written only with the new server, so later admin changes stick.
                for tool_name, min_role in seed.tool_policies.items():
                    session.add(ToolPolicy(server_id=row.id, tool_name=tool_name, min_role=min_role))
                log.info("seeded upstream %s -> %s", seed.slug, seed.url)
            await session.commit()

    async def create(self, data: ServerIn) -> UpstreamState:
        if not SLUG_RE.match(data.slug):
            raise RegistryError("slug must be lowercase letters, digits and single hyphens (e.g. 'woo', 'store-2')")
        validate_auth(data.auth_type, data.auth_header_name, data.credential, has_existing=False)
        await validate_url(data.url, self.settings)
        async with self.db.session() as session:
            await advisory_xact_lock(session, f"upstream-slug:{data.slug}")
            if await session.scalar(select(UpstreamServer.id).where(UpstreamServer.slug == data.slug)):
                raise RegistryError(f"slug '{data.slug}' is already used")
            if await session.scalar(
                select(ManagedRelease.id).where(
                    ManagedRelease.slug == data.slug,
                    ManagedRelease.status != "deleted",
                )
            ):
                raise RegistryError(f"slug '{data.slug}' is already used by a managed release")
            row = UpstreamServer(
                slug=data.slug, name=data.name, url=data.url, auth_type=data.auth_type,
                auth_header_name=data.auth_header_name if data.auth_type == "header" else None,
                credential_encrypted=self.box.encrypt(data.credential) if data.credential else None,
                timeout_seconds=data.timeout_seconds, enabled=data.enabled,
            )
            session.add(row)
            await session.commit()
        state = to_state(row)
        self.catalog.upsert(state)
        return self.catalog.servers[state.slug]

    async def ensure_managed(
        self, release_id: uuid.UUID, name: str, url: str, credential: str
    ) -> UpstreamState:
        """Create or recover the internal upstream for one ready managed release."""
        await validate_url(url, self.settings)
        async with self.db.session() as session:
            release = await session.get(ManagedRelease, release_id)
            if release is None:
                raise RegistryError("managed release no longer exists")
            await advisory_xact_lock(session, f"upstream-slug:{release.slug}")
            await session.refresh(release)
            if release.desired != "present" or release.status != "ready":
                raise RegistryError("managed release is not ready for registration")
            row = await session.scalar(select(UpstreamServer).where(UpstreamServer.slug == release.slug))
            if row is not None and not row.is_managed:
                raise RegistryError(f"slug '{release.slug}' is already used by a manual MCP server")
            if row is None:
                row = UpstreamServer(
                    slug=release.slug,
                    name=name,
                    url=url,
                    auth_type="bearer",
                    credential_encrypted=self.box.encrypt(credential),
                    timeout_seconds=self.settings.default_call_timeout_seconds,
                    is_managed=True,
                )
                session.add(row)
                await session.flush()
                log.info("registered managed upstream %s -> %s", release.slug, url)
            elif release.server_id not in (None, row.id):
                raise RegistryError(f"managed release '{release.slug}' points to a different MCP server")
            release.server_id = row.id
            release.status_reason = "MCP registration created; health check pending"
            release.updated_at = utcnow()
            await session.commit()
        state = to_state(row)
        self.catalog.upsert(state)
        return self.catalog.servers[state.slug]

    async def unregister_managed(self, release_id: uuid.UUID) -> str | None:
        """Remove a release's managed upstream without changing its desired state."""
        async with self.db.session() as session:
            release = await session.get(ManagedRelease, release_id)
            if release is None:
                return None
            await advisory_xact_lock(session, f"upstream-slug:{release.slug}")
            await session.refresh(release)
            row = await session.scalar(select(UpstreamServer).where(UpstreamServer.slug == release.slug))
            if row is not None and not row.is_managed:
                raise RegistryError(f"slug '{release.slug}' belongs to a manual MCP server")
            release.server_id = None
            if row is not None:
                await session.delete(row)
            await session.commit()
        if row is not None:
            self.catalog.remove(release.slug)
            log.info("unregistered managed upstream %s", release.slug)
        return release.slug if row is not None else None

    async def queue_managed_deletion(self, release_id: uuid.UUID, deadline_at: datetime) -> str:
        """Atomically unregister a managed upstream and queue its infrastructure teardown."""
        async with self.db.session() as session:
            release = await session.get(ManagedRelease, release_id)
            if release is None:
                raise KeyError(release_id)
            await advisory_xact_lock(session, f"upstream-slug:{release.slug}")
            await session.refresh(release)
            row = await session.scalar(select(UpstreamServer).where(UpstreamServer.slug == release.slug))
            if row is not None and not row.is_managed:
                raise RegistryError(f"slug '{release.slug}' belongs to a manual MCP server")
            if row is not None:
                await session.delete(row)
            release.server_id = None
            release.desired = "absent"
            release.status = "deleting"
            release.status_reason = "MCP server removed; infrastructure teardown queued"
            release.deadline_at = deadline_at
            release.lease_owner = None
            release.lease_until = None
            release.updated_at = utcnow()
            await session.commit()
        self.catalog.remove(release.slug)
        log.info("unregistered managed upstream %s and queued teardown", release.slug)
        return release.slug

    async def update(self, server_id: uuid.UUID, patch: ServerPatch) -> UpstreamState:
        async with self.db.session() as session:
            row = await session.get(UpstreamServer, server_id)
            if row is None:
                raise KeyError(server_id)
            auth_type = patch.auth_type or row.auth_type
            header = patch.auth_header_name if patch.auth_header_name is not None else row.auth_header_name
            validate_auth(auth_type, header, patch.credential, has_existing=row.credential_encrypted is not None and patch.credential != "")
            if patch.url is not None and patch.url != row.url:
                await validate_url(patch.url, self.settings)
                row.url = patch.url
            for field in ("name", "timeout_seconds", "enabled"):
                value = getattr(patch, field)
                if value is not None:
                    setattr(row, field, value)
            row.auth_type = auth_type
            row.auth_header_name = header if auth_type == "header" else None
            if patch.credential is not None:
                row.credential_encrypted = self.box.encrypt(patch.credential) if patch.credential else None
            if auth_type == "none":
                row.credential_encrypted = None
            row.updated_at = utcnow()
            await session.commit()
        self.catalog.upsert(to_state(row))
        return self.catalog.servers[row.slug]

    async def delete(self, server_id: uuid.UUID) -> str:
        async with self.db.session() as session:
            row = await session.get(UpstreamServer, server_id)
            if row is None:
                raise KeyError(server_id)
            slug = row.slug
            await session.delete(row)
            await session.commit()
        self.catalog.remove(slug)
        return slug

    async def set_policy(self, state: UpstreamState, tool_name: str, min_role: str | None, by: uuid.UUID) -> None:
        """Override a tool's minimum role, or remove the override (None) to fall back to the annotation default."""
        async with self.db.session() as session:
            row = await session.scalar(
                select(ToolPolicy).where(ToolPolicy.server_id == state.id, ToolPolicy.tool_name == tool_name)
            )
            if min_role is None:
                if row is not None:
                    await session.delete(row)
            elif row is None:
                session.add(ToolPolicy(server_id=state.id, tool_name=tool_name, min_role=min_role, updated_by=by))
            else:
                row.min_role, row.updated_by, row.updated_at = min_role, by, utcnow()
            await session.commit()
        if min_role is None:
            self.catalog.policies.pop((state.slug, tool_name), None)
        else:
            self.catalog.policies[(state.slug, tool_name)] = min_role

    async def persist_health(self, state: UpstreamState) -> None:
        async with self.db.session() as session:
            row = await session.get(UpstreamServer, state.id)
            if row is None:
                return
            row.status = state.status
            row.last_error = state.last_error
            row.last_checked_at = state.last_checked_at
            row.tools_cache = [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in state.tools]
            await session.commit()
