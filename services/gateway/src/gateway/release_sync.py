import asyncio
import json
import logging
import uuid

from sqlalchemy import select

from .catalog import Catalog
from .db import Database
from .health import HealthMonitor
from .models import ManagedRelease, utcnow
from .registry import Registry
from .security import CredentialBox

log = logging.getLogger(__name__)


class ManagedReleaseSync:
    """Keeps ready managed releases registered in the gateway catalog."""

    def __init__(
        self,
        db: Database,
        box: CredentialBox,
        catalog: Catalog,
        registry: Registry,
        monitor: HealthMonitor,
        interval: float,
    ):
        self.db = db
        self.box = box
        self.catalog = catalog
        self.registry = registry
        self.monitor = monitor
        self.interval = interval

    async def run(self) -> None:
        while True:
            try:
                await self.reconcile()
            except Exception:
                log.exception("managed release reconciliation failed")
            await asyncio.sleep(self.interval)

    async def reconcile(self) -> None:
        async with self.db.session() as session:
            rows = list((await session.scalars(select(ManagedRelease))).all())
        for row in rows:
            try:
                if row.desired == "present" and row.status == "ready":
                    if row.server_id is None or row.slug not in self.catalog.servers:
                        await self._register(row)
                elif row.server_id is not None:
                    await self._unregister(row.id)
            except Exception as exc:
                log.exception("could not reconcile managed release %s", row.slug)
                await self._record_reason(row.id, f"MCP registration retrying: {type(exc).__name__}")

    async def _register(self, row: ManagedRelease) -> None:
        secrets = json.loads(self.box.decrypt(row.secrets_encrypted))
        if row.kind == "store":
            credential = secrets.get("wooMcpToken")
            if not isinstance(credential, str) or not credential:
                raise ValueError("managed store has no Woo MCP token")
            name = f"WooCommerce ({row.display_name})"
            url = f"http://woo-mcp.{row.namespace}.svc.cluster.local:8000/mcp"
            auth_type = "bearer"
            auth_header_name = None
        elif row.kind == "hosted_mcp":
            spec = row.spec
            credential_env = spec.get("credential_env")
            credential = secrets.get(credential_env) if credential_env else None
            if credential_env and (not isinstance(credential, str) or not credential):
                raise ValueError("hosted MCP gateway credential is missing")
            name = f"Hosted MCP ({row.display_name})"
            url = f"http://mcp.{row.namespace}.svc.cluster.local:{spec['port']}{spec['path']}"
            auth_type = spec["auth_type"]
            auth_header_name = spec.get("auth_header_name")
        else:
            raise ValueError(f"unsupported managed release kind: {row.kind}")

        state = await self.registry.ensure_managed(
            row.id,
            name,
            url,
            auth_type,
            credential,
            auth_header_name,
        )
        await self.monitor.refresh(state.slug)
        if state.status == "connected":
            await self._record_reason(row.id, "resources and MCP tools are ready")
            log.info("managed release %s tools are available", row.slug)
        else:
            await self._record_reason(row.id, "resources are ready; MCP health check will retry")

    async def _unregister(self, release_id: uuid.UUID) -> None:
        before = self.catalog.visibility_signature()
        await self.registry.unregister_managed(release_id)
        await self.monitor.notify_if_changed(before)

    async def _record_reason(self, release_id: uuid.UUID, reason: str) -> None:
        async with self.db.session() as session:
            row = await session.get(ManagedRelease, release_id)
            if row is None or row.desired != "present" or row.status != "ready":
                return
            row.status_reason = reason[:1000]
            row.updated_at = utcnow()
            await session.commit()
