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


class StoreSync:
    """Keeps ready managed stores registered in the gateway catalog."""

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
                log.exception("managed store reconciliation failed")
            await asyncio.sleep(self.interval)

    async def reconcile(self) -> None:
        async with self.db.session() as session:
            rows = list(
                (
                    await session.scalars(
                        select(ManagedRelease).where(ManagedRelease.kind == "store")
                    )
                ).all()
            )
        for row in rows:
            try:
                if row.desired == "present" and row.status == "ready":
                    if row.server_id is None or row.slug not in self.catalog.servers:
                        await self._register(row)
                elif row.server_id is not None:
                    await self._unregister(row.id)
            except Exception as exc:
                log.exception("could not reconcile managed store %s", row.slug)
                await self._record_reason(row.id, f"MCP registration retrying: {type(exc).__name__}")

    async def _register(self, row: ManagedRelease) -> None:
        secrets = json.loads(self.box.decrypt(row.secrets_encrypted))
        token = secrets.get("wooMcpToken")
        if not isinstance(token, str) or not token:
            raise ValueError("managed store has no Woo MCP token")
        state = await self.registry.ensure_managed(
            row.id,
            f"WooCommerce ({row.display_name})",
            f"http://woo-mcp.{row.namespace}.svc.cluster.local:8000/mcp",
            token,
        )
        await self.monitor.refresh(state.slug)
        if state.status == "connected":
            await self._record_reason(row.id, "store resources and MCP tools are ready")
            log.info("managed store %s tools are available", row.slug)
        else:
            await self._record_reason(row.id, "store is ready; MCP health check will retry")

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
