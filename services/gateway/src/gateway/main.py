import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from fastapi import FastAPI
from mcp.server.transport_security import TransportSecuritySettings

from .api import router
from .audit import Auditor
from .auth import McpAuthMiddleware, Resolver, api_key_resolver, chat_token_resolver, rejected_call_auditor
from .catalog import Catalog
from .chat import LLM, ChatAgent, OpenAICompatibleLLM
from .chat import router as chat_router
from .config import Settings, get_settings
from .db import Database
from .health import HealthMonitor
from .hub import SessionHub
from .managed import router as managed_router
from .mcp_gateway import build_mcp_server
from .registry import Registry
from .security import CredentialBox
from .sessions import LoginThrottle
from .store_sync import StoreSync
from .team import router as team_router
from .upstream import Connector, http_connector

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


@dataclass
class Services:
    settings: Settings
    db: Database
    box: CredentialBox
    catalog: Catalog
    hub: SessionHub
    registry: Registry
    monitor: HealthMonitor
    store_sync: StoreSync
    resolve_api_key: Resolver
    throttle: LoginThrottle
    chat: ChatAgent
    chat_busy: set[uuid.UUID] = field(default_factory=set)


def create_app(
    settings: Settings | None = None, connector: Connector | None = None, run_monitor: bool = True, llm: LLM | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    if settings.uses_dev_secrets:
        log.warning("using built-in development signing secrets; set SESSION_SECRET and CHAT_TOKEN_SECRET")
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
    store_sync = StoreSync(db, box, catalog, registry, monitor, settings.provisioner_poll_seconds)
    resolve_api_key = api_key_resolver(db)
    owned_llm = None if llm else OpenAICompatibleLLM(
        settings.llm_base_url, settings.llm_api_key, settings.llm_model, settings.llm_timeout_seconds
    )
    chat_agent = ChatAgent(settings, llm or owned_llm)
    svc = Services(
        settings, db, box, catalog, hub, registry, monitor, store_sync,
        resolve_api_key, LoginThrottle(), chat_agent,
    )

    auditor = Auditor(db)
    mcp_server = build_mcp_server(catalog, hub, auditor, box, connector, settings.max_result_bytes)
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
            tasks = (
                [asyncio.create_task(monitor.run()), asyncio.create_task(store_sync.run())]
                if run_monitor else []
            )
            try:
                yield
            finally:
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
        if owned_llm:
            await owned_llm.aclose()
        await db.dispose()

    app = FastAPI(title="Urumi MCP Gateway", lifespan=lifespan)
    app.state.services = svc
    app.include_router(router)
    app.include_router(team_router)
    app.include_router(chat_router)
    app.include_router(managed_router)
    app.mount("/", McpAuthMiddleware(mcp_app, [resolve_api_key, chat_token_resolver(db, settings.chat_token_secret)], hub, rejected_call_auditor(db, auditor)))
    return app
