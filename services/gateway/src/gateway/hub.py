import logging

from mcp.server.subscriptions import InMemorySubscriptionBus, ToolsListChanged

log = logging.getLogger(__name__)


class SessionHub:
    """Pushes "tools changed" to connected MCP clients, for both protocol generations.

    - 2026-07-28 clients: events on the subscription bus (`subscriptions/listen` streams).
    - 2025-xx session clients: `notifications/tools/list_changed` on each known session,
      keyed by Mcp-Session-Id so closed sessions can be forgotten.
    """

    def __init__(self):
        self.bus = InMemorySubscriptionBus()
        self._sessions: dict[str, object] = {}

    def remember(self, session_id: str | None, session: object | None) -> None:
        if session_id and session is not None:
            self._sessions[session_id] = session

    def forget(self, session_id: str | None) -> None:
        if session_id:
            self._sessions.pop(session_id, None)

    @property
    def session_count(self) -> int:
        return len(self._sessions)

    async def notify_tools_changed(self) -> None:
        await self.bus.publish(ToolsListChanged())
        for session_id, session in list(self._sessions.items()):
            try:
                await session.send_tool_list_changed()
            except Exception:
                log.info("dropping closed MCP session %s", session_id)
                self._sessions.pop(session_id, None)
