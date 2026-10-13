import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

from fastapi import FastAPI
from mcp.server.transport_security import TransportSecuritySettings

from .api import router
from .audit import Auditor
from .auth import McpAuthMiddleware, Resolver, api_key_resolver
from .catalog import Catalog
from .config import Settings, get_settings
from .db import Database
from .health import HealthMonitor
from .hub import SessionHub
from .mcp_gateway import build_mcp_server
from .registry import Registry
from .security import CredentialBox
from .upstream import Connector, http_connector

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@dataclass
class Services:
    settings: Settings
    db: Database
    box: CredentialBox
    catalog: Catalog
    hub: SessionHub
    registry: Registry
    monitor: HealthMonitor
    resolve_api_key: Resolver


def create_app(settings: Settings | None = None, connector: Connector | None = None, run_monitor: bool = True) -> FastAPI:
    settings = settings or get_settings()
    db = Database(settings.database_url)
    box = CredentialBox(settings.credential_encryption_key)
    catalog = Catalog(settings.max_concurrent_calls_per_upstream)
    hub = SessionHub()
    registry = Registry(db, box, settings, catalog)
    connector = connector or http_connector(settings.allowed_internal_host_patterns)
    monitor = HealthMonitor(
        catalog, registry, connector, hub,
        settings.health_interval_seconds, settings.health_timeout_seconds, settings.unreachable_after_failures,
    )
    resolve_api_key = api_key_resolver(db)
    svc = Services(settings, db, box, catalog, hub, registry, monitor, resolve_api_key)

    mcp_server = build_mcp_server(catalog, hub, Auditor(db), box, connector)
    mcp_app = mcp_server.streamable_http_app(
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.allowed_mcp_host_list,
            allowed_origins=[],
        ),
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await registry.bootstrap_admin()
        await registry.seed()
        await registry.load()
        async with mcp_server.session_manager.run():
            task = asyncio.create_task(monitor.run()) if run_monitor else None
            try:
                yield
            finally:
                if task:
                    task.cancel()
        await db.dispose()

    app = FastAPI(title="Urumi MCP Gateway", lifespan=lifespan)
    app.state.services = svc
    app.include_router(router)
    app.mount("/", McpAuthMiddleware(mcp_app, [resolve_api_key], hub))
    return app
