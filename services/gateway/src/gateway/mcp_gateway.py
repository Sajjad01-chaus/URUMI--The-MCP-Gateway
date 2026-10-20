import json
import logging
import math
import time
from typing import Any

import anyio
from mcp import types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.subscriptions import ListenHandler
from pydantic import BaseModel, Field, ValidationError

from .audit import Auditor, args_preview
from .auth import Principal
from .catalog import Catalog, ToolUnavailable
from .hub import SessionHub
from .metrics import GatewayMetrics
from .rate_limit import TokenBucketLimiter
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


class SearchToolsInput(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    server: str | None = Field(default=None, min_length=1, max_length=20)
    limit: int = Field(default=8, ge=1, le=20)


class CallToolInput(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    arguments: dict[str, Any] = Field(default_factory=dict)


SEARCH_TOOL = types.Tool(
    name="gateway__search_tools",
    description="Search the tools available to you by capability, optionally within one server.",
    input_schema=SearchToolsInput.model_json_schema(),
    annotations=types.ToolAnnotations(read_only_hint=True),
)
CALL_TOOL = types.Tool(
    name="gateway__call_tool",
    description="Call one tool returned by gateway__search_tools using its exact name and arguments.",
    input_schema=CallToolInput.model_json_schema(),
    annotations=types.ToolAnnotations(read_only_hint=False),
)
META_TOOLS = [SEARCH_TOOL, CALL_TOOL]


def error_result(message: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=message)], is_error=True)


def principal_of(ctx: Any) -> Principal:
    principal = getattr(getattr(ctx.request, "state", None), "principal", None)
    if principal is None:
        raise RuntimeError("request reached the MCP handler without authentication")
    return principal


def result_size(result: types.CallToolResult) -> int:
    return len(result.model_dump_json(by_alias=True, exclude_none=True).encode())


def build_mcp_servers(
    catalog: Catalog, hub: SessionHub, auditor: Auditor, box: CredentialBox, connector: Connector,
    metrics: GatewayMetrics, tool_limiter: TokenBucketLimiter, calls_per_minute: int,
    max_result_bytes: int, app_version: str,
) -> tuple[GatewayServer, GatewayServer]:
    def track(ctx: Any) -> None:
        request = ctx.request
        session_id = request.headers.get("mcp-session-id") if request is not None else None
        hub.remember(session_id, ctx.session)

    async def list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        track(ctx)
        return types.ListToolsResult(tools=catalog.visible_tools(principal_of(ctx).role))

    async def execute(ctx: Any, name: str, arguments: dict[str, Any], via_search: bool = False) -> types.CallToolResult:
        principal = principal_of(ctx)
        started = time.monotonic()
        slug, tool_name, outcome, error = "", name, "success", None
        server_id = None
        try:
            state, tool = catalog.resolve(name, principal.role)
            slug, tool_name, server_id = state.slug, tool.name, state.id
            target = target_for(state, box)
            retry_after = tool_limiter.consume(principal.user_id)
            if retry_after:
                outcome = "rate_limited"
                error = f"Rate limit reached: {calls_per_minute} calls/min; retry in {math.ceil(retry_after)} s"
                metrics.rate_limited.labels("tool_call").inc()
                return error_result(error)
            # Bulkhead: each upstream gets a bounded number of in-flight calls; the timeout covers waiting too.
            # No retries: a call that timed out may already have run upstream (e.g. created a product).
            with anyio.fail_after(target.timeout_seconds):
                async with state.semaphore:
                    async with connector(target) as client:
                        result = await client.call_tool(tool.name, arguments)
            result = result.model_copy(update={"meta": None})
            # Size cap: an oversized result never reaches the client, the model or the audit preview.
            size = result_size(result)
            if size > max_result_bytes:
                outcome = "error"
                error = f"Upstream '{slug}' result too large ({size // 1024} KiB > {max_result_bytes // 1024} KiB)"
                log.warning("call %s: %s", name, error)
                return error_result(error)
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
            log.warning("call %s failed: %s", name, error)
            return error_result(error)
        finally:
            duration = time.monotonic() - started
            metric_server = slug if slug in catalog.servers else "unknown"
            metric_tool = tool_name if metric_server != "unknown" else "unknown"
            metrics.tool_calls.labels(metric_server, metric_tool, outcome, principal.source).inc()
            metrics.tool_duration.labels(metric_server, outcome).observe(duration)
            await auditor.record(
                user_id=principal.user_id, user_email=principal.email, api_key_id=principal.api_key_id,
                source=principal.source, server_id=server_id, server_slug=slug or "-", tool_name=tool_name,
                exposed_name=name, duration_ms=int(duration * 1000),
                outcome=outcome, error_message=error, request_id=str(ctx.request_id),
                client_name=(ctx.request.headers.get("user-agent", "")[:100] if ctx.request is not None else None),
                args_preview=args_preview({**arguments, "via": "search"} if via_search else arguments),
            )

    async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        track(ctx)
        return await execute(ctx, params.name, params.arguments or {})

    async def list_meta_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        principal_of(ctx)
        return types.ListToolsResult(tools=META_TOOLS)

    async def audit_meta(
        ctx: Any, params: types.CallToolRequestParams, principal: Principal,
        tool_name: str, started: float, outcome: str, error: str | None,
    ) -> None:
        duration = time.monotonic() - started
        metrics.tool_calls.labels("gateway", tool_name, outcome, principal.source).inc()
        metrics.tool_duration.labels("gateway", outcome).observe(duration)
        await auditor.record(
            user_id=principal.user_id, user_email=principal.email, api_key_id=principal.api_key_id,
            source=principal.source, server_slug="gateway", tool_name=tool_name,
            exposed_name=params.name, duration_ms=int(duration * 1000), outcome=outcome,
            error_message=error, request_id=str(ctx.request_id),
            client_name=(ctx.request.headers.get("user-agent", "")[:100] if ctx.request is not None else None),
            args_preview=args_preview(params.arguments),
        )

    async def call_meta_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        principal = principal_of(ctx)
        if params.name == CALL_TOOL.name:
            try:
                data = CallToolInput.model_validate(params.arguments or {})
            except ValidationError as exc:
                error = f"Invalid gateway__call_tool arguments: {exc.errors()[0]['msg']}"
                await audit_meta(ctx, params, principal, "call_tool", time.monotonic(), "error", error)
                return error_result(error)
            return await execute(ctx, data.name, data.arguments, via_search=True)

        started = time.monotonic()
        outcome, error = "success", None
        try:
            if params.name != SEARCH_TOOL.name:
                outcome, error = "denied", f"Unknown tool '{params.name}'"
                return error_result(error)
            try:
                data = SearchToolsInput.model_validate(params.arguments or {})
            except ValidationError as exc:
                outcome, error = "error", f"Invalid gateway__search_tools arguments: {exc.errors()[0]['msg']}"
                return error_result(error)
            retry_after = tool_limiter.consume(principal.user_id)
            if retry_after:
                outcome = "rate_limited"
                error = f"Rate limit reached: {calls_per_minute} calls/min; retry in {math.ceil(retry_after)} s"
                metrics.rate_limited.labels("tool_call").inc()
                return error_result(error)
            found = catalog.search_tools(principal.role, data.query, data.server, data.limit)
            result = types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(found, separators=(",", ":")))],
                structured_content={"tools": found},
            )
            if result_size(result) > max_result_bytes:
                outcome, error = "error", "Tool search result exceeded the gateway response-size limit"
                return error_result(error)
            return result
        finally:
            await audit_meta(ctx, params, principal, "search_tools", started, outcome, error)

    full_server = GatewayServer(
        "urumi-mcp-gateway",
        version=app_version,
        instructions="Team MCP gateway. Tool names are '<server>__<tool>'; the prefix says which system a tool acts on.",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        on_subscriptions_listen=ListenHandler(hub.bus),
    )
    search_server = GatewayServer(
        "urumi-mcp-gateway-search",
        version=app_version,
        instructions="Search for an allowed tool, then call its exact name through the gateway.",
        on_list_tools=list_meta_tools,
        on_call_tool=call_meta_tool,
    )
    return full_server, search_server


def _text(result: types.CallToolResult) -> str:
    return " ".join(c.text for c in result.content if isinstance(c, types.TextContent))[:500]
