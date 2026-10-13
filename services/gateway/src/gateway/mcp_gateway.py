import logging
import time
from typing import Any

import anyio
from mcp import types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.subscriptions import ListenHandler

from .audit import Auditor, args_preview
from .auth import Principal
from .catalog import Catalog, ToolUnavailable
from .hub import SessionHub
from .registry import target_for
from .security import CredentialBox
from .upstream import Connector, describe_error

log = logging.getLogger(__name__)


class GatewayServer(Server):
    """Low-level MCP server: its tool list is decided per request, not fixed at import time."""

    def create_initialization_options(self, notification_options=None, *args, **kwargs):
        return super().create_initialization_options(
            notification_options or NotificationOptions(tools_changed=True), *args, **kwargs
        )


def error_result(message: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=message)], is_error=True)


def principal_of(ctx: Any) -> Principal:
    principal = getattr(getattr(ctx.request, "state", None), "principal", None)
    if principal is None:
        raise RuntimeError("request reached the MCP handler without authentication")
    return principal


def build_mcp_server(catalog: Catalog, hub: SessionHub, auditor: Auditor, box: CredentialBox, connector: Connector) -> GatewayServer:
    def track(ctx: Any) -> None:
        request = ctx.request
        session_id = request.headers.get("mcp-session-id") if request is not None else None
        hub.remember(session_id, ctx.session)

    async def list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        track(ctx)
        return types.ListToolsResult(tools=catalog.visible_tools(principal_of(ctx).role))

    async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        track(ctx)
        principal = principal_of(ctx)
        started = time.monotonic()
        slug, tool_name, outcome, error = "", params.name, "success", None
        server_id = None
        try:
            state, tool = catalog.resolve(params.name, principal.role)
            slug, tool_name, server_id = state.slug, tool.name, state.id
            target = target_for(state, box)
            # Bulkhead: each upstream gets a bounded number of in-flight calls; the timeout covers waiting too.
            # No retries: a call that timed out may already have run upstream (e.g. created a product).
            with anyio.fail_after(target.timeout_seconds):
                async with state.semaphore:
                    async with connector(target) as client:
                        result = await client.call_tool(tool.name, params.arguments or {})
            result = result.model_copy(update={"meta": None})
            if result.is_error:
                outcome = "error"
                error = _text(result)
            return result
        except ToolUnavailable as exc:
            slug, tool_name, outcome, error = exc.slug or slug, exc.tool or tool_name, exc.outcome, str(exc)
            return error_result(error)
        except TimeoutError:
            outcome = "timeout"
            error = (
                f"Upstream '{slug}' did not answer within {target.timeout_seconds:g}s. "
                "The call may or may not have run; check before trying again."
            )
            return error_result(error)
        except Exception as exc:
            outcome = "unavailable"
            error = f"Upstream '{slug}' is unreachable: {describe_error(exc)}"
            log.warning("call %s failed: %s", params.name, error)
            return error_result(error)
        finally:
            await auditor.record(
                user_id=principal.user_id, user_email=principal.email, api_key_id=principal.api_key_id,
                source=principal.source, server_id=server_id, server_slug=slug or "-", tool_name=tool_name,
                exposed_name=params.name, duration_ms=int((time.monotonic() - started) * 1000),
                outcome=outcome, error_message=error, request_id=str(ctx.request_id),
                client_name=(ctx.request.headers.get("user-agent", "")[:100] if ctx.request is not None else None),
                args_preview=args_preview(params.arguments),
            )

    return GatewayServer(
        "urumi-mcp-gateway",
        instructions="Team MCP gateway. Tool names are '<server>__<tool>'; the prefix says which system a tool acts on.",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        on_subscriptions_listen=ListenHandler(hub.bus),
    )


def _text(result: types.CallToolResult) -> str:
    return " ".join(c.text for c in result.content if isinstance(c, types.TextContent))[:500]
