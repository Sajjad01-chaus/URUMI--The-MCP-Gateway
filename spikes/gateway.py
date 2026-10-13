import uvicorn
from mcp import Client, types
from mcp.server.lowlevel import Server
from starlette.responses import JSONResponse

UPSTREAM_URL = "http://127.0.0.1:8001/mcp"
PREFIX = "demo__"
API_KEY = "spike-secret-key"


async def list_tools(ctx, params) -> types.ListToolsResult:
    async with Client(UPSTREAM_URL) as upstream:
        result = await upstream.list_tools()
    tools = [
        tool.model_copy(update={"name": PREFIX + tool.name, "description": f"[demo] {tool.description}"})
        for tool in result.tools
    ]
    return types.ListToolsResult(tools=tools)


async def call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
    if not params.name.startswith(PREFIX):
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"Unknown tool: {params.name}")], is_error=True
        )
    async with Client(UPSTREAM_URL) as upstream:
        return await upstream.call_tool(params.name.removeprefix(PREFIX), params.arguments or {})


class BearerAuth:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            if headers.get(b"authorization") != f"Bearer {API_KEY}".encode():
                response = JSONResponse({"error": "invalid or missing API key"}, status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


server = Server("mini-gateway", on_list_tools=list_tools, on_call_tool=call_tool)
app = BearerAuth(server.streamable_http_app())

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8002)