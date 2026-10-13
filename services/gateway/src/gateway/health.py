import asyncio
import logging

import anyio
from mcp import Client, types

from .catalog import Catalog, UpstreamState
from .hub import SessionHub
from .models import utcnow
from .registry import Registry, target_for
from .upstream import Connector, describe_error

log = logging.getLogger(__name__)


async def list_all_tools(client: Client, max_pages: int = 20) -> list[types.Tool]:
    tools: list[types.Tool] = []
    cursor = None
    for _ in range(max_pages):
        page = await client.list_tools(cursor=cursor)
        tools.extend(page.tools)
        cursor = page.next_cursor
        if not cursor:
            break
    return tools


class HealthMonitor:
    """Probes every enabled upstream (connect + tools/list) on an interval and refreshes the catalog.

    One success marks a server Connected; `failures_to_unreachable` consecutive failures mark it
    Unreachable (one blip doesn't flap). Clients are notified whenever the routable tool set changes.
    """

    def __init__(
        self, catalog: Catalog, registry: Registry, connector: Connector, hub: SessionHub,
        interval: float, timeout: float, failures_to_unreachable: int,
    ):
        self.catalog, self.registry, self.connector, self.hub = catalog, registry, connector, hub
        self.interval, self.timeout, self.failures_to_unreachable = interval, timeout, failures_to_unreachable

    async def run(self) -> None:
        while True:
            try:
                await self.check_all()
            except Exception:
                log.exception("health sweep failed")
            await asyncio.sleep(self.interval)

    async def check_all(self) -> None:
        servers = [s for s in self.catalog.servers.values() if s.enabled]
        await self._sweep(servers)

    async def refresh(self, slug: str) -> None:
        """Check one server now (after an admin change) and notify clients if tools changed."""
        state = self.catalog.servers.get(slug)
        await self._sweep([state] if state and state.enabled else [])

    async def notify_if_changed(self, before: frozenset) -> None:
        if self.catalog.visibility_signature() != before:
            await self.hub.notify_tools_changed()

    async def _sweep(self, servers: list[UpstreamState]) -> None:
        before = self.catalog.visibility_signature()
        await asyncio.gather(*(self.check(s) for s in servers))
        await self.notify_if_changed(before)

    async def check(self, state: UpstreamState) -> None:
        try:
            with anyio.fail_after(self.timeout):
                async with self.connector(target_for(state, self.registry.box)) as client:
                    tools = await list_all_tools(client)
            state.tools, state.status, state.last_error, state.consecutive_failures = tools, "connected", None, 0
        except Exception as exc:
            state.consecutive_failures += 1
            state.last_error = describe_error(exc)
            if state.status != "connected" or state.consecutive_failures >= self.failures_to_unreachable:
                if state.status != "unreachable":
                    log.warning("upstream %s unreachable: %s", state.slug, state.last_error)
                state.status = "unreachable"
        state.last_checked_at = utcnow()
        try:
            await self.registry.persist_health(state)
        except Exception:
            log.exception("could not persist health of %s", state.slug)
